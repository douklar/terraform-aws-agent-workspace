import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
from botocore.exceptions import ClientError

# Anything not on a stock LogRecord came from a caller's extra={...}.
_RESERVED_LOG_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Serializes the record plus any `extra` fields as a single JSON line."""

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# Configure structured logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Add console handler if not already present
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)


ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")

# The scheduler manages every EC2 instance that carries this tag KEY. The tag
# VALUE selects the per-instance mode (read fresh on every invocation, so tag
# edits take effect in realtime). This makes the scheduler reusable: tag any
# instance with `scheduler=<mode>` and it joins the managed fleet.
SCHEDULER_MODE_TAG_KEY = os.getenv("SCHEDULER_MODE_TAG_KEY", "scheduler")

# Optional: an instance ID that is always included even if its tag has not yet
# propagated (the module's own workspace instance). Discovery is tag-based, so
# this is only a belt-and-braces guarantee and may be empty.
MANAGED_INSTANCE_ID = os.getenv("MANAGED_INSTANCE_ID", "")

MANAGER_TAG_KEY = os.getenv("MANAGER_TAG_KEY", "CreatedBy")
# No default: scopes which AMIs and snapshots cleanup is allowed to delete.
MANAGER_TAG_VALUE = os.environ["MANAGER_TAG_VALUE"]
SCHEDULER_MODE_DEFAULT = os.getenv("SCHEDULER_MODE_DEFAULT", "free-time")
SCHEDULER_MODE_ON_DEMAND = os.getenv("SCHEDULER_MODE_ON_DEMAND", "on-demand")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "UTC")
INSTANCE_ALLOWED_WINDOWS = os.getenv("INSTANCE_ALLOWED_WINDOWS", "[]")

DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

# Modes that keep the instance running continuously (never stopped on schedule).
ON_DEMAND_ALIASES = {
    "on-demand", "ondemand", "always-on", "always", "on", "keep-on", "keep-running",
}
# Modes that tell the scheduler to ignore the instance entirely: it will neither
# start nor stop it. This is the safe escape hatch for "leave my box alone".
DISABLED_ALIASES = {
    "disabled", "off", "manual", "paused", "ignore", "none", "false", "no",
}

LIVE_INSTANCE_STATES = ["pending", "running", "stopping", "stopped"]


def positive_int_env(name, default):
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


DAILY_RETENTION_DAYS = positive_int_env("DAILY_RETENTION_DAYS", 7)
WEEKLY_RETENTION_DAYS = positive_int_env("WEEKLY_RETENTION_DAYS", 30)
MONTHLY_RETENTION_DAYS = positive_int_env("MONTHLY_RETENTION_DAYS", 30)


def lambda_handler(event, _context):
    event = event or {}
    action = event.get("action")
    logger.info("Lambda handler invoked", extra={"action": action})

    if action in ("start", "stop"):
        result = manage_instances(action, event)
        return {"status": "ok", "action": action, **result}

    if action in ("enforce_schedule", "reconcile_schedule"):
        result = enforce_schedule()
        return {"status": "ok", "action": action, **result}

    if action == "create_ami":
        backup_type = event.get("backup_type", "daily")
        created = create_amis(backup_type)
        return {"status": "ok", "action": action, "created_images": created}

    if action == "cleanup_amis":
        cleanup_result = cleanup_amis()
        cleaned_snapshots = cleanup_daily_snapshots()
        return {
            "status": "ok",
            "action": action,
            "cleaned_images": cleanup_result["cleaned"],
            "cleanup_errors": cleanup_result["errors"],
            "cleaned_snapshots": cleaned_snapshots,
        }

    if action == "create_daily_snapshots":
        created = create_daily_snapshots()
        return {"status": "ok", "action": action, "created_snapshots": created}

    if action == "security_update":
        result = run_security_update()
        return {"status": "ok", "action": action, **result}

    raise ValueError(f"Unsupported action: {action}")


# ─── FLEET DISCOVERY ──────────────────────────────────────────────────────────
def managed_instances(states=None):
    """Return all instances tagged for scheduling, deduplicated.

    Discovery is by tag KEY presence, so tagging any instance with
    `<SCHEDULER_MODE_TAG_KEY>=<mode>` makes it part of the managed fleet.
    """
    wanted_states = states or LIVE_INSTANCE_STATES
    filters = [
        {"Name": "tag-key", "Values": [SCHEDULER_MODE_TAG_KEY]},
        {"Name": "instance-state-name", "Values": wanted_states},
    ]

    instances = []
    seen = set()
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=filters):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                if instance_id not in seen:
                    seen.add(instance_id)
                    instances.append(instance)

    # Belt-and-braces: always include the module's own instance if it exists and
    # is in a relevant state but its tag has not been discovered (propagation lag).
    if MANAGED_INSTANCE_ID and MANAGED_INSTANCE_ID not in seen:
        instance = describe_instance(MANAGED_INSTANCE_ID)
        if instance and instance["State"]["Name"] in wanted_states:
            instances.append(instance)

    return instances


def describe_instance(instance_id):
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "InvalidInstanceID.NotFound":
            return None
        raise
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return instance
    return None


def instance_state(instance_id):
    instance = describe_instance(instance_id)
    if instance is None:
        raise ValueError(f"Instance not found: {instance_id}")
    return instance["State"]["Name"]


def instance_tags(instance):
    return {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}


def normalize_mode(value):
    mode = (value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not mode:
        return SCHEDULER_MODE_DEFAULT
    if mode in ON_DEMAND_ALIASES:
        return SCHEDULER_MODE_ON_DEMAND
    if mode in DISABLED_ALIASES:
        return "disabled"
    return mode


def instance_mode(instance):
    return normalize_mode(instance_tags(instance).get(SCHEDULER_MODE_TAG_KEY))


def event_scheduler_mode(event):
    mode = (event or {}).get("scheduler_mode")
    if mode is None:
        return None
    return normalize_mode(mode)


# ─── START / STOP ─────────────────────────────────────────────────────────────
def manage_instances(action, event=None):
    requested_mode = event_scheduler_mode(event)
    results = [manage_one(instance, action, requested_mode) for instance in managed_instances()]
    return {"count": len(results), "instances": results}


def manage_one(instance, action, requested_mode):
    instance_id = instance["InstanceId"]
    state = instance["State"]["Name"]
    mode = instance_mode(instance)

    base = {"instance_id": instance_id, "scheduler_mode": mode}

    if mode == "disabled":
        return {**base, "previous_state": state, "result": "skipped_disabled"}

    if mode == SCHEDULER_MODE_ON_DEMAND:
        if action == "start":
            return {**base, **start_instance(instance_id, "on_demand_start", state)}
        # on-demand instances are never stopped by the scheduler
        return {**base, "previous_state": state, "result": "skipped_on_demand_stop"}

    # Scheduled (window) mode. A start/stop event may target a specific mode
    # cohort; skip instances whose mode does not match that cohort.
    if requested_mode and requested_mode != mode:
        return {
            **base,
            "previous_state": state,
            "event_scheduler_mode": requested_mode,
            "result": "skipped_mode_mismatch",
        }

    if action == "start":
        return {**base, **start_instance(instance_id, "scheduled_start", state)}
    if action == "stop":
        return {**base, **stop_instance(instance_id, "scheduled_stop", state)}
    raise ValueError(f"Unsupported instance action: {action}")


def start_instance(instance_id, reason, initial_state=None):
    state = initial_state or instance_state(instance_id)
    previous_state = state

    if state == "stopping":
        state = wait_for_instance_state(
            instance_id,
            {"pending", "running", "stopped", "shutting-down", "terminated"},
            timeout_seconds=45,
            poll_seconds=5,
        )

    if state == "stopped":
        if DRY_RUN:
            result = "dry_run_start"
        else:
            ec2.start_instances(InstanceIds=[instance_id])
            result = "start_requested"
    elif state == "stopping":
        result = "stopping_start_deferred"
    else:
        result = "no_action"

    logger.info(
        "Evaluated instance start",
        extra={
            "instance_id": instance_id,
            "reason": reason,
            "previous_state": previous_state,
            "evaluated_state": state,
            "result": result,
        },
    )

    return {
        "reason": reason,
        "previous_state": previous_state,
        "evaluated_state": state,
        "result": result,
    }


def stop_instance(instance_id, reason, initial_state=None):
    state = initial_state or instance_state(instance_id)
    previous_state = state

    if state == "pending":
        state = wait_for_instance_state(
            instance_id,
            {"running", "stopping", "stopped", "shutting-down", "terminated"},
            timeout_seconds=45,
            poll_seconds=5,
        )

    if state == "running":
        if DRY_RUN:
            result = "dry_run_stop"
        else:
            ec2.stop_instances(InstanceIds=[instance_id])
            result = "stop_requested"
    elif state == "pending":
        result = "pending_stop_deferred"
    else:
        result = "no_action"

    logger.info(
        "Evaluated instance stop",
        extra={
            "instance_id": instance_id,
            "reason": reason,
            "previous_state": previous_state,
            "evaluated_state": state,
            "result": result,
        },
    )

    return {
        "reason": reason,
        "previous_state": previous_state,
        "evaluated_state": state,
        "result": result,
    }


def wait_for_instance_state(instance_id, target_states, timeout_seconds=45, poll_seconds=5):
    deadline = time.monotonic() + timeout_seconds
    state = instance_state(instance_id)

    while state not in target_states and time.monotonic() < deadline:
        time.sleep(poll_seconds)
        state = instance_state(instance_id)

    return state


# ─── RECONCILE ────────────────────────────────────────────────────────────────
def enforce_schedule():
    utc_now = datetime.now(timezone.utc)
    results = [enforce_one(instance, utc_now) for instance in managed_instances()]
    return {
        "evaluated_at": utc_now.isoformat(),
        "count": len(results),
        "instances": results,
    }


def enforce_one(instance, utc_now):
    instance_id = instance["InstanceId"]
    state = instance["State"]["Name"]
    mode = instance_mode(instance)
    base = {"instance_id": instance_id, "scheduler_mode": mode}

    if mode == "disabled":
        return {**base, "previous_state": state, "result": "skipped_disabled"}

    if mode == SCHEDULER_MODE_ON_DEMAND:
        return {**base, **start_instance(instance_id, "on_demand_reconcile", state)}

    try:
        windows = active_allowed_windows(mode)
        allowed_now = is_allowed_time(utc_now, windows)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        # Non-destructive default: if we cannot evaluate the schedule we leave the
        # instance untouched rather than stopping a possibly-in-use machine.
        logger.exception(
            "Schedule evaluation failed; leaving instance unchanged",
            extra={"instance_id": instance_id, "scheduler_mode": mode},
        )
        return {
            **base,
            "previous_state": state,
            "result": "schedule_error_no_action",
            "schedule_error": str(exc),
        }

    if allowed_now:
        return {**base, "allowed_now": True, **start_instance(instance_id, "scheduled_reconcile", state)}

    return {**base, "allowed_now": False, **stop_instance(instance_id, "outside_allowed_window", state)}


# ─── SCHEDULE WINDOWS ─────────────────────────────────────────────────────────
def allowed_windows():
    try:
        windows = json.loads(INSTANCE_ALLOWED_WINDOWS)
    except json.JSONDecodeError as exc:
        raise ValueError("INSTANCE_ALLOWED_WINDOWS must be valid JSON") from exc

    if not isinstance(windows, list) or not windows:
        raise ValueError("INSTANCE_ALLOWED_WINDOWS must contain at least one window")

    return windows


def active_allowed_windows(mode):
    windows = [window for window in allowed_windows() if window_mode(window) == mode]
    if not windows:
        raise ValueError(f"No instance_schedule_windows configured for scheduler mode: {mode}")

    return windows


def window_mode(window):
    return normalize_mode(window.get("mode", SCHEDULER_MODE_DEFAULT))


def is_allowed_time(utc_now, windows):
    for window in windows:
        if window_allows_time(utc_now, window):
            return True
    return False


def window_allows_time(utc_now, window):
    days = {day.upper() for day in window.get("days", [])}
    if not days:
        raise ValueError(f"Allowed window is missing days: {window}")

    window_timezone = window.get("timezone") or SCHEDULE_TIMEZONE
    local_now = utc_now.astimezone(ZoneInfo(window_timezone))
    start_minutes = window_time_minutes(window, "start_time")
    stop_minutes = window_time_minutes(window, "stop_time")

    current_day = DAY_NAMES[local_now.weekday()]
    previous_day = DAY_NAMES[(local_now.weekday() - 1) % len(DAY_NAMES)]
    current_minutes = local_now.hour * 60 + local_now.minute

    if start_minutes == stop_minutes:
        return current_day in days

    if start_minutes < stop_minutes:
        return current_day in days and start_minutes <= current_minutes < stop_minutes

    return (
        (current_day in days and current_minutes >= start_minutes)
        or (previous_day in days and current_minutes < stop_minutes)
    )


def window_time_minutes(window, key):
    if key not in window:
        raise ValueError(f"Allowed window is missing {key}: {window}")
    return parse_hhmm(window[key])


def parse_hhmm(value):
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"Invalid time value, expected HH:MM: {value}") from exc

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid time value, expected HH:MM: {value}")

    return hour * 60 + minute


# ─── BACKUPS: AMIs ────────────────────────────────────────────────────────────
def create_amis(backup_type):
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    created = []

    # Create backups only from running instances to avoid backups while powered off.
    for instance in managed_instances(states=["running"]):
        instance_id = instance["InstanceId"]
        image_name = f"{instance_id}-{backup_type}-{stamp}"

        response = ec2.create_image(
            InstanceId=instance_id,
            Name=image_name,
            Description=f"Automated {backup_type} AMI by scheduler",
            NoReboot=True,
            TagSpecifications=[
                {
                    "ResourceType": "image",
                    "Tags": [
                        {"Key": MANAGER_TAG_KEY, "Value": MANAGER_TAG_VALUE},
                        {"Key": "BackupType", "Value": backup_type},
                        {"Key": "SourceInstanceId", "Value": instance_id},
                        {"Key": "CreatedAtUtc", "Value": now.isoformat()},
                    ],
                },
                {
                    "ResourceType": "snapshot",
                    "Tags": [
                        {"Key": MANAGER_TAG_KEY, "Value": MANAGER_TAG_VALUE},
                        {"Key": "BackupType", "Value": backup_type},
                        {"Key": "SourceInstanceId", "Value": instance_id},
                        {"Key": "CreatedAtUtc", "Value": now.isoformat()},
                    ],
                },
            ],
        )

        created.append(response["ImageId"])

    return created


def is_snapshot_referenced(snapshot_id, exclude_image_id):
    """Check if a snapshot is referenced by any AMI other than the one being deregistered."""
    try:
        paginator = ec2.get_paginator("describe_images")
        for page in paginator.paginate(
            # Only our own AMIs can reference our own snapshot.
            Owners=["self"],
            Filters=[
                {"Name": "block-device-mapping.snapshot-id", "Values": [snapshot_id]},
            ],
        ):
            for image in page.get("Images", []):
                if image["ImageId"] != exclude_image_id:
                    return True
        return False
    except ClientError as e:
        logger.error(
            "Failed to check snapshot usage",
            extra={"snapshot_id": snapshot_id, "error": str(e)},
        )
        # Fail safe: assume snapshot is referenced if we can't check
        return True


def retry_api_call(func, max_retries=3, base_delay=1):
    """Retry a boto3 API call with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return func()
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            # Retry on throttling and transient errors
            if error_code in ("Throttling", "RequestLimitExceeded", "InternalError"):
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "API call throttled, retrying",
                        extra={"attempt": attempt + 1, "delay": delay, "error": str(e)},
                    )
                    time.sleep(delay)
                else:
                    raise
            else:
                raise


def cleanup_amis():
    now = datetime.now(timezone.utc)
    daily_cutoff = now - timedelta(days=DAILY_RETENTION_DAYS)
    weekly_cutoff = now - timedelta(days=WEEKLY_RETENTION_DAYS)
    monthly_cutoff = now - timedelta(days=MONTHLY_RETENTION_DAYS)

    logger.info(
        "Starting AMI cleanup",
        extra={
            "daily_cutoff": daily_cutoff.isoformat(),
            "weekly_cutoff": weekly_cutoff.isoformat(),
            "monthly_cutoff": monthly_cutoff.isoformat(),
            "dry_run": DRY_RUN,
        },
    )

    # Scope cleanup to artifacts this scheduler created (across every managed
    # instance), never anything else in the account.
    filters = [
        {"Name": f"tag:{MANAGER_TAG_KEY}", "Values": [MANAGER_TAG_VALUE]},
    ]

    images = []
    paginator = ec2.get_paginator("describe_images")
    for page in paginator.paginate(Owners=["self"], Filters=filters):
        images.extend(page.get("Images", []))

    logger.info("Found images to evaluate", extra={"count": len(images)})

    cleaned = []
    errors = []

    for image in images:
        image_id = image["ImageId"]
        created_at = parse_aws_datetime(image["CreationDate"])
        tags = {t["Key"]: t["Value"] for t in image.get("Tags", [])}
        backup_type = tags.get("BackupType", "weekly")
        source_instance = tags.get("SourceInstanceId", "unknown")

        logger.info(
            "Evaluating AMI",
            extra={
                "image_id": image_id,
                "created_at": created_at.isoformat(),
                "backup_type": backup_type,
                "source_instance": source_instance,
            },
        )

        if backup_type == "monthly":
            expired = created_at < monthly_cutoff
        elif backup_type == "weekly":
            expired = created_at < weekly_cutoff
        else:
            # any legacy/unrecognized backup type uses the daily window
            expired = created_at < daily_cutoff

        if not expired:
            logger.info("AMI not expired, skipping", extra={"image_id": image_id})
            continue

        if DRY_RUN:
            logger.info(
                "DRY RUN: Would deregister AMI",
                extra={"image_id": image_id, "source_instance": source_instance},
            )
            cleaned.append(image_id)
            continue

        # Deregister AMI with error handling
        try:
            retry_api_call(lambda iid=image_id: ec2.deregister_image(ImageId=iid))
            logger.info("Deregistered AMI", extra={"image_id": image_id})
        except ClientError as e:
            error_msg = f"Failed to deregister AMI {image_id}: {e}"
            logger.error(error_msg)
            errors.append(error_msg)
            continue

        # Handle snapshot cleanup
        for mapping in image.get("BlockDeviceMappings", []):
            ebs = mapping.get("Ebs")
            if not ebs:
                continue
            snapshot_id = ebs.get("SnapshotId")
            if not snapshot_id:
                continue

            # Check if snapshot is referenced by other AMIs
            if is_snapshot_referenced(snapshot_id, exclude_image_id=image_id):
                logger.warning(
                    "Snapshot is referenced by other AMIs, skipping deletion",
                    extra={"snapshot_id": snapshot_id, "image_id": image_id},
                )
                continue

            try:
                retry_api_call(lambda sid=snapshot_id: ec2.delete_snapshot(SnapshotId=sid))
                logger.info(
                    "Deleted snapshot",
                    extra={"snapshot_id": snapshot_id, "image_id": image_id},
                )
            except ClientError as e:
                error_msg = f"Failed to delete snapshot {snapshot_id}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)
                # Continue processing other snapshots even if one fails

        cleaned.append(image_id)

    logger.info(
        "AMI cleanup completed",
        extra={
            "cleaned_count": len(cleaned),
            "errors_count": len(errors),
        },
    )

    return {"cleaned": cleaned, "errors": errors}


# ─── MAINTENANCE: PATCHING ────────────────────────────────────────────────────
def run_security_update():
    running = [instance["InstanceId"] for instance in managed_instances(states=["running"])]
    if not running:
        return {"status": "skipped", "reason": "no running managed instances"}

    response = ssm.send_command(
        InstanceIds=running,
        DocumentName="AWS-RunPatchBaseline",
        Parameters={"Operation": ["Install"], "RebootOption": ["NoReboot"]},
        TimeoutSeconds=600,
    )

    command_id = response["Command"]["CommandId"]

    statuses = dict.fromkeys(running, "Pending")
    terminal = {"Success", "Failed", "TimedOut", "Cancelled"}
    # 24 iterations × 10 s = 240 s max; Lambda timeout is 360 s, leaving headroom.
    for _ in range(24):
        pending = [iid for iid, status in statuses.items() if status not in terminal]
        if not pending:
            break
        for instance_id in pending:
            try:
                invocation = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
                statuses[instance_id] = invocation["Status"]
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "InvocationDoesNotExist":
                    continue
                raise
        if all(status in terminal for status in statuses.values()):
            break
        time.sleep(10)

    return {"command_id": command_id, "statuses": statuses}


# ─── BACKUPS: VOLUME SNAPSHOTS ────────────────────────────────────────────────
def create_daily_snapshots():
    now = datetime.now(timezone.utc)
    created = []

    for instance in managed_instances():
        instance_id = instance["InstanceId"]
        # Snapshot only the root volume: that is what the Lambda's IAM policy
        # grants ec2:CreateSnapshot on. Snapshotting an extra data volume would
        # fail with AccessDenied, so scope to the root device.
        root_device_name = instance.get("RootDeviceName")
        for mapping in instance.get("BlockDeviceMappings", []):
            if root_device_name and mapping.get("DeviceName") != root_device_name:
                continue
            ebs = mapping.get("Ebs")
            if not ebs:
                continue
            volume_id = ebs.get("VolumeId")
            if not volume_id:
                continue

            snap = ec2.create_snapshot(
                VolumeId=volume_id,
                Description=f"Daily snapshot for {instance_id} volume {volume_id}",
                TagSpecifications=[
                    {
                        "ResourceType": "snapshot",
                        "Tags": [
                            {"Key": MANAGER_TAG_KEY, "Value": MANAGER_TAG_VALUE},
                            {"Key": "BackupType", "Value": "daily-snapshot"},
                            {"Key": "SourceInstanceId", "Value": instance_id},
                            {"Key": "SourceVolumeId", "Value": volume_id},
                            {"Key": "CreatedAtUtc", "Value": now.isoformat()},
                        ],
                    }
                ],
            )
            created.append(snap["SnapshotId"])

    return created


def cleanup_daily_snapshots():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAILY_RETENTION_DAYS)
    filters = [
        {"Name": f"tag:{MANAGER_TAG_KEY}", "Values": [MANAGER_TAG_VALUE]},
        {"Name": "tag:BackupType", "Values": ["daily-snapshot"]},
    ]

    snapshots = []
    paginator = ec2.get_paginator("describe_snapshots")
    for page in paginator.paginate(OwnerIds=["self"], Filters=filters):
        snapshots.extend(page.get("Snapshots", []))

    cleaned = []
    for snapshot in snapshots:
        snapshot_id = snapshot["SnapshotId"]
        started_at = snapshot["StartTime"]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        if started_at >= cutoff:
            continue

        try:
            retry_api_call(lambda sid=snapshot_id: ec2.delete_snapshot(SnapshotId=sid))
            cleaned.append(snapshot_id)
        except ClientError as e:
            logger.error("Failed to delete daily snapshot", extra={"snapshot_id": snapshot_id, "error": str(e)})

    return cleaned


def parse_aws_datetime(value):
    # AWS returns AMI creation date like 2026-05-25T01:30:04.000Z.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)
