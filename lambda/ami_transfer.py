import os
from datetime import datetime, timezone

import boto3


ec2 = boto3.client("ec2")


def env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


MANAGED_INSTANCE_ID = os.getenv("MANAGED_INSTANCE_ID", "")
MANAGER_TAG_KEY = os.getenv("MANAGER_TAG_KEY", "CreatedBy")
MANAGER_TAG_VALUE = os.getenv("MANAGER_TAG_VALUE", "workspace-scheduler")
COPY_TARGET_REGION = os.getenv("COPY_TARGET_REGION", "")
EXPORT_S3_BUCKET = os.getenv("EXPORT_S3_BUCKET", "")
EXPORT_S3_PREFIX = os.getenv("EXPORT_S3_PREFIX", "ami-exports")
EXPORT_DISK_FORMAT = os.getenv("EXPORT_DISK_FORMAT", "VMDK")
AWS_REGION = os.getenv("AWS_REGION", "")
ENABLE_AMI_COPY = env_bool("ENABLE_AMI_COPY", False)
ENABLE_AMI_EXPORT = env_bool("ENABLE_AMI_EXPORT", False)
VMIMPORT_ROLE_NAME = os.getenv("VMIMPORT_ROLE_NAME", "vmimport")

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

    images = ec2.describe_images(
        Owners=["self"],
        Filters=[
            {"Name": f"tag:{MANAGER_TAG_KEY}", "Values": [MANAGER_TAG_VALUE]},
            {"Name": "tag:SourceInstanceId", "Values": [MANAGED_INSTANCE_ID]},
        ],
    ).get("Images", [])

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

    response = dest_ec2.copy_image(
        Name=copy_name,
        Description=f"Manual copy of {source_image_id} from {source_region}",
        SourceImageId=source_image_id,
        SourceRegion=source_region,
        CopyImageTags=True,
    )

    return {
        "source_image_id": source_image_id,
        "copied_image_id": response["ImageId"],
        "destination_region": destination_region,
    }


def export_latest_ami(s3_bucket, s3_prefix, disk_format):
    if not s3_bucket:
        raise ValueError("s3_bucket is required")

    image = latest_managed_ami()
    image_id = image["ImageId"]

    response = ec2.export_image(
        DiskImageFormat=disk_format,
        ImageId=image_id,
        S3ExportLocation={
            "S3Bucket": s3_bucket,
            "S3Prefix": s3_prefix,
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

    return {
        "source_image_id": image_id,
        "export_image_task_id": response["ExportImageTaskId"],
        "s3_bucket": s3_bucket,
        "s3_prefix": s3_prefix,
        "disk_format": disk_format,
    }
