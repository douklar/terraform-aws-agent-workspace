import json
import logging
import os
from datetime import datetime, timezone

import boto3


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


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

ec2 = boto3.client("ec2")


def env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


MANAGED_INSTANCE_ID = os.getenv("MANAGED_INSTANCE_ID", "")
MANAGER_TAG_KEY = os.getenv("MANAGER_TAG_KEY", "CreatedBy")
# No default: must match the scheduler's per-deployment tag value.
MANAGER_TAG_VALUE = os.environ["MANAGER_TAG_VALUE"]
COPY_TARGET_REGION = os.getenv("COPY_TARGET_REGION", "")
EXPORT_S3_BUCKET = os.getenv("EXPORT_S3_BUCKET", "")
EXPORT_S3_PREFIX = os.getenv("EXPORT_S3_PREFIX", "ami-exports")
EXPORT_DISK_FORMAT = os.getenv("EXPORT_DISK_FORMAT", "VMDK")
AWS_REGION = os.getenv("AWS_REGION", "")
ENABLE_AMI_COPY = env_bool("ENABLE_AMI_COPY", False)
ENABLE_AMI_EXPORT = env_bool("ENABLE_AMI_EXPORT", False)
VMIMPORT_ROLE_NAME = os.getenv("VMIMPORT_ROLE_NAME", "vmimport")
# Set only for customer-managed encryption. Regional, so same-region copies only.
KMS_KEY_ARN = os.getenv("KMS_KEY_ARN", "")

ALLOWED_DISK_FORMATS = {"VMDK", "VHD", "RAW"}


def normalize_disk_format(value):
    normalized = (value or "").strip().upper()
    if normalized not in ALLOWED_DISK_FORMATS:
        allowed = ", ".join(sorted(ALLOWED_DISK_FORMATS))
        raise ValueError(f"disk_format must be one of: {allowed}")
    return normalized


def normalize_s3_prefix(value):
    prefix = (value or "").strip().strip("/")
    if not prefix or ".." in prefix:
        raise ValueError("s3_prefix must be a non-empty relative prefix")
    return prefix


def lambda_handler(event, _context):
    event = event or {}
    action = event.get("action")
    logger.info("Lambda handler invoked", extra={"action": action})

    if action == "copy_latest_ami":
        if not ENABLE_AMI_COPY:
            raise ValueError("copy_latest_ami action is disabled by Lambda environment")
        destination_region = event.get("destination_region") or COPY_TARGET_REGION
        copied = copy_latest_ami(destination_region)
        return {"status": "ok", "action": action, **copied}

    if action == "export_latest_ami":
        if not ENABLE_AMI_EXPORT:
            raise ValueError("export_latest_ami action is disabled by Lambda environment")
        if event.get("s3_bucket"):
            raise ValueError("s3_bucket is configured by the Lambda environment and cannot be overridden")
        if event.get("s3_prefix"):
            raise ValueError("s3_prefix is configured by the Lambda environment and cannot be overridden")
        s3_bucket = EXPORT_S3_BUCKET
        s3_prefix = EXPORT_S3_PREFIX
        disk_format = normalize_disk_format(event.get("disk_format") or EXPORT_DISK_FORMAT)
        exported = export_latest_ami(s3_bucket, normalize_s3_prefix(s3_prefix), disk_format)
        return {"status": "ok", "action": action, **exported}

    raise ValueError(f"Unsupported action: {action}")


def latest_managed_ami():
    if not MANAGED_INSTANCE_ID:
        raise ValueError("MANAGED_INSTANCE_ID is required")

    images = []
    paginator = ec2.get_paginator("describe_images")
    for page in paginator.paginate(
        Owners=["self"],
        Filters=[
            {"Name": f"tag:{MANAGER_TAG_KEY}", "Values": [MANAGER_TAG_VALUE]},
            {"Name": "tag:SourceInstanceId", "Values": [MANAGED_INSTANCE_ID]},
        ],
    ):
        images.extend(page.get("Images", []))

    if not images:
        raise ValueError("No managed AMIs found to copy/export")

    images.sort(key=lambda img: img["CreationDate"], reverse=True)
    return images[0]


def copy_latest_ami(destination_region):
    if not destination_region:
        raise ValueError("destination_region is required")

    source_image = latest_managed_ami()
    source_image_id = source_image["ImageId"]
    source_region = AWS_REGION or boto3.session.Session().region_name

    if not source_region:
        raise ValueError("Unable to determine source AWS region")

    dest_ec2 = boto3.client("ec2", region_name=destination_region)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    copy_name = f"{source_image.get('Name', source_image_id)}-copy-{destination_region}-{stamp}"

    source_tags = {tag["Key"]: tag["Value"] for tag in source_image.get("Tags", [])}
    # Not CopyImageTags: it carries SourceInstanceId, which latest_managed_ami()
    # selects on, so each copy would become the source of the next one.
    copy_tags = [
        {"Key": MANAGER_TAG_KEY, "Value": MANAGER_TAG_VALUE},
        {"Key": "SourceImageId", "Value": source_image_id},
    ]
    if source_tags.get("BackupType"):
        copy_tags.append({"Key": "BackupType", "Value": source_tags["BackupType"]})

    copy_kwargs = {
        "Name": copy_name,
        "Description": f"Manual copy of {source_image_id} from {source_region}",
        "SourceImageId": source_image_id,
        "SourceRegion": source_region,
        "TagSpecifications": [
            {"ResourceType": "image", "Tags": copy_tags},
            {"ResourceType": "snapshot", "Tags": copy_tags},
        ],
    }

    # A KMS key is regional. Cross-region, EC2 falls back to the AWS-managed
    # key in the destination, so only pass ours for a same-region copy.
    used_customer_managed_key = bool(KMS_KEY_ARN) and destination_region == source_region
    if used_customer_managed_key:
        copy_kwargs["Encrypted"] = True
        copy_kwargs["KmsKeyId"] = KMS_KEY_ARN
    elif KMS_KEY_ARN:
        logger.warning(
            "Cross-region AMI copy cannot use the source region's customer-managed "
            "key; the copy will use the AWS-managed EBS key in the destination "
            "region instead",
            extra={"source_region": source_region, "destination_region": destination_region},
        )

    response = dest_ec2.copy_image(**copy_kwargs)

    logger.info(
        "Copied AMI",
        extra={
            "source_image_id": source_image_id,
            "copied_image_id": response["ImageId"],
            "destination_region": destination_region,
            "used_customer_managed_key": used_customer_managed_key,
        },
    )

    return {
        "source_image_id": source_image_id,
        "copied_image_id": response["ImageId"],
        "destination_region": destination_region,
        "used_customer_managed_key": used_customer_managed_key,
    }


def export_latest_ami(s3_bucket, s3_prefix, disk_format):
    if not s3_bucket:
        raise ValueError("s3_bucket is required")

    image = latest_managed_ami()
    image_id = image["ImageId"]

    # ExportImage writes the artifact to "<S3Prefix><task-id>.<format>". The prefix
    # must end with "/" so the object lands inside the folder (e.g. ami-exports/),
    # matching the IAM policy and S3 lifecycle rule scoped to that prefix.
    s3_prefix_key = s3_prefix.rstrip("/") + "/"

    response = ec2.export_image(
        DiskImageFormat=disk_format,
        ImageId=image_id,
        S3ExportLocation={
            "S3Bucket": s3_bucket,
            "S3Prefix": s3_prefix_key,
        },
        Description=f"Manual export for {image_id}",
        RoleName=VMIMPORT_ROLE_NAME,
        TagSpecifications=[
            {
                "ResourceType": "export-image-task",
                "Tags": [
                    {"Key": MANAGER_TAG_KEY, "Value": MANAGER_TAG_VALUE},
                    {"Key": "SourceImageId", "Value": image_id},
                ],
            }
        ],
    )

    logger.info(
        "Started AMI export",
        extra={
            "source_image_id": image_id,
            "export_image_task_id": response["ExportImageTaskId"],
            "s3_bucket": s3_bucket,
            "s3_prefix": s3_prefix_key,
            "disk_format": disk_format,
        },
    )

    return {
        "source_image_id": image_id,
        "export_image_task_id": response["ExportImageTaskId"],
        "s3_bucket": s3_bucket,
        "s3_prefix": s3_prefix_key,
        "disk_format": disk_format,
    }
