import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
from botocore.exceptions import ClientError

# Configure structured logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Add console handler if not already present
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)


ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")

MANAGED_INSTANCE_ID = os.getenv("MANAGED_INSTANCE_ID", "")
MANAGER_TAG_KEY = os.getenv("MANAGER_TAG_KEY", "CreatedBy")
MANAGER_TAG_VALUE = os.getenv("MANAGER_TAG_VALUE", "workspace-scheduler")
SCHEDULER_MODE_TAG_KEY = os.getenv("SCHEDULER_MODE_TAG_KEY", "scheduler")
SCHEDULER_MODE_DEFAULT = os.getenv("SCHEDULER_MODE_DEFAULT", "free-time")
SCHEDULER_MODE_ON_DEMAND = os.getenv("SCHEDULER_MODE_ON_DEMAND", "on-demand")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "UTC")
INSTANCE_ALLOWED_WINDOWS = os.getenv("INSTANCE_ALLOWED_WINDOWS", "[]")

DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


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
MONTHLY_RETENTION_DAYS = positive_int_env("MONTHLY_RETENTION_DAYS", 30)


def lambda_handler(event, _context):
    event = event or {}
    action = event.get("action")
    logger.info("Lambda handler invoked", extra={"action": action})

    if action in ("start", "stop"):
        result = manage_instance(action, event)
        return {"status": "ok", "action": action, "instance_id": MANAGED_INSTANCE_ID, **result}

    if action in ("enforce_schedule", "reconcile_schedule"):
        result = enforce_schedule()
        return {"status": "ok", "action": action, "instance_id": MANAGED_INSTANCE_ID, **result}

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


def all_backup_instance_ids(only_running=False):
    instance_states = ["running"] if only_running else ["pending", "running", "stopping", "stopped"]
    instance = managed_instance_details()
    return [MANAGED_INSTANCE_ID] if instance["State"]["Name"] in instance_states else []


def managed_instance_details():
    if not MANAGED_INSTANCE_ID:
        raise ValueError("MANAGED_INSTANCE_ID is required")

    response = ec2.describe_instances(InstanceIds=[MANAGED_INSTANCE_ID])
    reservations = response.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        raise ValueError(f"Managed instance not found: {MANAGED_INSTANCE_ID}")

    return reservations[0]["Instances"][0]


def managed_instance_tags(instance=None):
    instance = instance or managed_instance_details()
    return {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}


def scheduler_mode(instance=None):
    tags = managed_instance_tags(instance)
    return tags.get(SCHEDULER_MODE_TAG_KEY, SCHEDULER_MODE_DEFAULT).strip().lower()


def event_scheduler_mode(event):
    mode = (event or {}).get("scheduler_mode")
    if mode is None:
        return None
    return str(mode).strip().lower()


def is_on_demand(instance=None):
    return scheduler_mode(instance) == SCHEDULER_MODE_ON_DEMAND


def managed_instance_state():
    return managed_instance_details()["State"]["Name"]


def manage_instance(action, event=None):
    instance = managed_instance_details()
    state = instance["State"]["Name"]
    current_mode = scheduler_mode(instance)
    requested_mode = event_scheduler_mode(event)

    if is_on_demand(instance):
        if action == "start":
            return {"scheduler_mode": current_mode, **start_managed_instance("on_demand_start", state)}
        return {"previous_state": state, "scheduler_mode": current_mode, "result": "skipped_on_demand_stop"}

    if requested_mode and requested_mode != current_mode:
        return {
            "previous_state": state,
            "scheduler_mode": current_mode,
            "event_scheduler_mode": requested_mode,
            "result": "skipped_mode_mismatch",
        }

    if action == "start":
        return {"scheduler_mode": current_mode, **start_managed_instance("scheduled_start", state)}
    elif action == "stop":
        return {"scheduler_mode": current_mode, **stop_managed_instance("scheduled_stop", state)}
    else:
        raise ValueError(f"Unsupported instance action: {action}")


def start_managed_instance(reason, initial_state=None):
    state = initial_state or managed_instance_state()
    previous_state = state

    if state == "stopping":
        state = wait_for_instance_state(
            {"pending", "running", "stopped", "shutting-down", "terminated"},
            timeout_seconds=45,
            poll_seconds=5,
        )

    if state == "stopped":
        ec2.start_instances(InstanceIds=[MANAGED_INSTANCE_ID])
        result = "start_requested"
    elif state == "stopping":
        result = "stopping_start_deferred"
    else:
        result = "no_action"

    logger.info(
        "Evaluated managed instance start",
        extra={
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


def stop_managed_instance(reason, initial_state=None):
    state = initial_state or managed_instance_state()
    previous_state = state

    if state == "pending":
        state = wait_for_instance_state(
            {"running", "stopping", "stopped", "shutting-down", "terminated"},
            timeout_seconds=45,
            poll_seconds=5,
        )

    if state == "running":
        ec2.stop_instances(InstanceIds=[MANAGED_INSTANCE_ID])
        result = "stop_requested"
    elif state == "pending":
        result = "pending_stop_deferred"
    else:
        result = "no_action"

    logger.info(
        "Evaluated managed instance stop",
        extra={
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


def wait_for_instance_state(target_states, timeout_seconds=45, poll_seconds=5):
    deadline = time.monotonic() + timeout_seconds
    state = managed_instance_state()

    while state not in target_states and time.monotonic() < deadline:
        time.sleep(poll_seconds)
        state = managed_instance_state()

    return state


def enforce_schedule():
    utc_now = datetime.now(timezone.utc)
    instance = managed_instance_details()
    current_mode = scheduler_mode(instance)
    result = {
        "evaluated_at": utc_now.isoformat(),
        "scheduler_mode": current_mode,
    }

    if is_on_demand(instance):
        start_result = start_managed_instance("on_demand_reconcile", instance["State"]["Name"])
        return {**result, **start_result}

    try:
        windows = active_allowed_windows(current_mode)
        allowed_now = is_allowed_time(utc_now, windows)
        result.update({"allowed_now": allowed_now, "window_count": len(windows)})
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        logger.exception("Schedule evaluation failed; stopping instance as a safe default")
        stop_result = stop_managed_instance("schedule_evaluation_failed")
        return {
            **result,
            "allowed_now": False,
            "window_count": 0,
            "schedule_error": str(exc),
            **stop_result,
        }

    if allowed_now:
        start_result = start_managed_instance("scheduled_reconcile", instance["State"]["Name"])
        return {**result, **start_result}

    stop_result = stop_managed_instance("outside_allowed_window")
    return {**result, **stop_result}


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
    return str(window.get("mode", SCHEDULER_MODE_DEFAULT)).strip().lower()


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
    start_minutes = parse_window_time(window, "start_time", "start_minute", "start")
    stop_minutes = parse_window_time(window, "stop_time", "stop_minute", "stop")

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


def parse_window_time(window, time_key, legacy_minute_key, legacy_time_key):
    if time_key in window:
        return parse_hhmm(window[time_key])

    if legacy_minute_key in window:
        return validate_minute(window[legacy_minute_key], legacy_minute_key)

    if legacy_time_key in window:
        return parse_hhmm(window[legacy_time_key])

    if legacy_time_key == "stop" and "end" in window:
        return parse_hhmm(window["end"])

    raise ValueError(f"Allowed window is missing {time_key}: {window}")


def validate_minute(value, key):
    try:
        minute = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a minute from 0 to 1439: {value}") from exc

    if minute < 0 or minute > 1439:
        raise ValueError(f"{key} must be a minute from 0 to 1439: {value}")

    return minute


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


def create_amis(backup_type):
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    created = []

    # Create backups only from running instances to avoid backups while powered off.
    for instance_id in all_backup_instance_ids(only_running=True):
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


def is_snapshot_referenced(snapshot_id):
    """Check if a snapshot is referenced by other AMIs (excluding the scheduler-created ones)."""
    try:
        response = ec2.describe_images(
            Filters=[
                {"Name": "block-device-mapping.snapshot-id", "Values": [snapshot_id]},
            ],
        )
        # Filter out scheduler-created images - we only care about other images
        other_images = [
            img for img in response.get("Images", [])
            if not any(t["Key"] == MANAGER_TAG_KEY and t["Value"] == MANAGER_TAG_VALUE
                      for t in img.get("Tags", []))
        ]
        return len(other_images) > 0
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
    monthly_cutoff = now - timedelta(days=MONTHLY_RETENTION_DAYS)

    logger.info(
        "Starting AMI cleanup",
        extra={
            "daily_cutoff": daily_cutoff.isoformat(),
            "monthly_cutoff": monthly_cutoff.isoformat(),
            "dry_run": DRY_RUN,
        },
    )

    filters = [
        {"Name": f"tag:{MANAGER_TAG_KEY}", "Values": [MANAGER_TAG_VALUE]},
    ]
    filters.append({"Name": "tag:SourceInstanceId", "Values": [MANAGED_INSTANCE_ID]})

    images = ec2.describe_images(
        Owners=["self"],
        Filters=filters,
    ).get("Images", [])

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
        else:
            # weekly (and any legacy daily type) use this retention window
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
            retry_api_call(lambda: ec2.deregister_image(ImageId=image_id))
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
            if is_snapshot_referenced(snapshot_id):
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


def run_security_update():
    if not MANAGED_INSTANCE_ID:
        raise ValueError("MANAGED_INSTANCE_ID is required")

    state = managed_instance_state()
    if state != "running":
        return {"status": "skipped", "reason": f"Instance is {state}, not running"}

    response = ssm.send_command(
        InstanceIds=[MANAGED_INSTANCE_ID],
        DocumentName="AWS-RunPatchBaseline",
        Parameters={"Operation": ["Install"], "RebootOption": ["NoReboot"]},
        TimeoutSeconds=600,
    )

    command_id = response["Command"]["CommandId"]

    result = {
        "Status": "Pending",
        "StandardOutputContent": "",
        "StandardErrorContent": "",
    }
    # 24 iterations × 10 s = 240 s max; Lambda timeout is 360 s, leaving ~120 s headroom.
    for _ in range(24):
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=MANAGED_INSTANCE_ID)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "InvocationDoesNotExist":
                time.sleep(10)
                continue
            raise
        if result["Status"] in ("Success", "Failed", "TimedOut"):
            break
        time.sleep(10)

    return {
        "command_id": command_id,
        "status": result["Status"],
        "output": result.get("StandardOutputContent", ""),
        "error_output": result.get("StandardErrorContent", ""),
    }


def create_daily_snapshots():
    now = datetime.now(timezone.utc)
    created = []

    instance = managed_instance_details()
    instance_states = ["pending", "running", "stopping", "stopped"]
    if instance["State"]["Name"] not in instance_states:
        return created

    instance_id = MANAGED_INSTANCE_ID
    for mapping in instance.get("BlockDeviceMappings", []):
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
    filters.append({"Name": "tag:SourceInstanceId", "Values": [MANAGED_INSTANCE_ID]})

    snapshots = ec2.describe_snapshots(
        OwnerIds=["self"],
        Filters=filters,
    ).get("Snapshots", [])

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
