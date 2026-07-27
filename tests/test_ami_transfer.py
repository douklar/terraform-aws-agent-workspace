"""Tests for ami_transfer.py.

Mostly copy_latest_ami's KMS handling: the key is regional, so it applies to a
same-region copy only and must not be passed cross-region.
"""

import pytest

import ami_transfer


class FakeEc2:
    """Records copy_image kwargs so the test can assert what was sent to AWS."""

    def __init__(self):
        self.copy_image_kwargs = None

    def copy_image(self, **kwargs):
        self.copy_image_kwargs = kwargs
        return {"ImageId": "ami-copydest"}


@pytest.fixture
def fake_source_ami(monkeypatch):
    monkeypatch.setattr(
        ami_transfer,
        "latest_managed_ami",
        lambda: {"ImageId": "ami-source", "Name": "workspace-ami"},
    )
    monkeypatch.setattr(ami_transfer, "AWS_REGION", "eu-central-1")


@pytest.fixture
def capture_dest_client(monkeypatch):
    fake = FakeEc2()
    monkeypatch.setattr(ami_transfer.boto3, "client", lambda *a, **k: fake)
    return fake


class TestCopyLatestAmiKms:
    def test_same_region_copy_applies_customer_key(self, fake_source_ami, capture_dest_client, monkeypatch):
        monkeypatch.setattr(ami_transfer, "KMS_KEY_ARN", "arn:aws:kms:eu-central-1:111122223333:key/abc")
        result = ami_transfer.copy_latest_ami("eu-central-1")

        kwargs = capture_dest_client.copy_image_kwargs
        assert kwargs["Encrypted"] is True
        assert kwargs["KmsKeyId"] == "arn:aws:kms:eu-central-1:111122223333:key/abc"
        assert result["used_customer_managed_key"] is True
        assert result["copied_image_id"] == "ami-copydest"

    def test_cross_region_copy_drops_customer_key(self, fake_source_ami, capture_dest_client, monkeypatch, caplog):
        monkeypatch.setattr(ami_transfer, "KMS_KEY_ARN", "arn:aws:kms:eu-central-1:111122223333:key/abc")
        with caplog.at_level("WARNING"):
            result = ami_transfer.copy_latest_ami("us-east-1")

        kwargs = capture_dest_client.copy_image_kwargs
        # The regional key must NOT be sent to a different-region copy.
        assert "KmsKeyId" not in kwargs
        assert "Encrypted" not in kwargs
        assert result["used_customer_managed_key"] is False
        assert any("cross-region" in r.message.lower() for r in caplog.records)

    def test_no_customer_key_leaves_encryption_to_aws(self, fake_source_ami, capture_dest_client, monkeypatch):
        monkeypatch.setattr(ami_transfer, "KMS_KEY_ARN", "")
        result = ami_transfer.copy_latest_ami("eu-central-1")

        kwargs = capture_dest_client.copy_image_kwargs
        assert "KmsKeyId" not in kwargs
        assert result["used_customer_managed_key"] is False

    def test_missing_destination_region_raises(self):
        with pytest.raises(ValueError, match="destination_region is required"):
            ami_transfer.copy_latest_ami("")


class TestCopyLatestAmiTagging:
    """A copy must never become selectable as "the latest managed AMI".

    latest_managed_ami() selects on SourceInstanceId, so if a copy carried that
    tag the next copy would copy the copy - unbounded, with the name growing by
    one "-copy-<region>-<stamp>" each round until AMI's 128-character name limit
    rejects it. Same-region copy is the default path, so this is not exotic.
    """

    @pytest.fixture
    def tagged_source(self, monkeypatch):
        monkeypatch.setattr(
            ami_transfer,
            "latest_managed_ami",
            lambda: {
                "ImageId": "ami-source",
                "Name": "workspace-ami",
                "Tags": [
                    {"Key": "CreatedBy", "Value": "wp-scheduler"},
                    {"Key": "BackupType", "Value": "weekly"},
                    {"Key": "SourceInstanceId", "Value": "i-123"},
                ],
            },
        )
        monkeypatch.setattr(ami_transfer, "AWS_REGION", "eu-central-1")
        monkeypatch.setattr(ami_transfer, "MANAGER_TAG_KEY", "CreatedBy")
        monkeypatch.setattr(ami_transfer, "MANAGER_TAG_VALUE", "wp-scheduler")

    def _image_tags(self, kwargs):
        spec = next(s for s in kwargs["TagSpecifications"] if s["ResourceType"] == "image")
        return {t["Key"]: t["Value"] for t in spec["Tags"]}

    def test_copy_does_not_inherit_source_instance_id(self, tagged_source, capture_dest_client, monkeypatch):
        monkeypatch.setattr(ami_transfer, "KMS_KEY_ARN", "")
        ami_transfer.copy_latest_ami("eu-central-1")

        kwargs = capture_dest_client.copy_image_kwargs
        # CopyImageTags would propagate SourceInstanceId wholesale; it must not
        # be used, and the tag must not be set by any other route either.
        assert "CopyImageTags" not in kwargs
        assert "SourceInstanceId" not in self._image_tags(kwargs)

    def test_copy_stays_in_retention_scope(self, tagged_source, capture_dest_client, monkeypatch):
        monkeypatch.setattr(ami_transfer, "KMS_KEY_ARN", "")
        ami_transfer.copy_latest_ami("eu-central-1")

        tags = self._image_tags(capture_dest_client.copy_image_kwargs)
        # Without CreatedBy the scheduler's cleanup would never reap copies.
        assert tags["CreatedBy"] == "wp-scheduler"
        assert tags["BackupType"] == "weekly"
        assert tags["SourceImageId"] == "ami-source"

    def test_snapshots_are_tagged_alongside_the_image(self, tagged_source, capture_dest_client, monkeypatch):
        monkeypatch.setattr(ami_transfer, "KMS_KEY_ARN", "")
        ami_transfer.copy_latest_ami("eu-central-1")

        types = {s["ResourceType"] for s in capture_dest_client.copy_image_kwargs["TagSpecifications"]}
        # An untagged backing snapshot survives every cleanup filter and bills forever.
        assert types == {"image", "snapshot"}

    def test_tag_keys_stay_within_the_iam_allow_list(self, tagged_source, capture_dest_client, monkeypatch):
        monkeypatch.setattr(ami_transfer, "KMS_KEY_ARN", "")
        ami_transfer.copy_latest_ami("eu-central-1")

        # transfer_lambda_copy restricts aws:TagKeys to exactly these three, so
        # emitting any other key makes CopyImage fail with UnauthorizedOperation.
        allowed = {"CreatedBy", "BackupType", "SourceImageId"}
        for spec in capture_dest_client.copy_image_kwargs["TagSpecifications"]:
            assert {t["Key"] for t in spec["Tags"]} <= allowed

    def test_untagged_source_omits_backup_type(self, fake_source_ami, capture_dest_client, monkeypatch):
        monkeypatch.setattr(ami_transfer, "KMS_KEY_ARN", "")
        ami_transfer.copy_latest_ami("eu-central-1")

        assert "BackupType" not in self._image_tags(capture_dest_client.copy_image_kwargs)


class TestNormalizeHelpers:
    @pytest.mark.parametrize("value, expected", [("vmdk", "VMDK"), (" VHD ", "VHD"), ("raw", "RAW")])
    def test_disk_format_normalized(self, value, expected):
        assert ami_transfer.normalize_disk_format(value) == expected

    @pytest.mark.parametrize("value", ["", "ISO", None, "qcow2"])
    def test_disk_format_rejects_unsupported(self, value):
        with pytest.raises(ValueError, match="disk_format must be one of"):
            ami_transfer.normalize_disk_format(value)

    @pytest.mark.parametrize("value, expected", [("ami-exports", "ami-exports"), ("/a/b/", "a/b")])
    def test_s3_prefix_normalized(self, value, expected):
        assert ami_transfer.normalize_s3_prefix(value) == expected

    @pytest.mark.parametrize("value", ["", "/", "../etc", "a/../b"])
    def test_s3_prefix_rejects_empty_or_traversal(self, value):
        with pytest.raises(ValueError, match="non-empty relative prefix"):
            ami_transfer.normalize_s3_prefix(value)


class TestEnvBool:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_truthy(self, monkeypatch, value):
        monkeypatch.setenv("SOME_FLAG", value)
        assert ami_transfer.env_bool("SOME_FLAG", False) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "maybe"])
    def test_falsy(self, monkeypatch, value):
        monkeypatch.setenv("SOME_FLAG", value)
        assert ami_transfer.env_bool("SOME_FLAG", True) is False

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SOME_FLAG", raising=False)
        assert ami_transfer.env_bool("SOME_FLAG", True) is True


class TestLambdaHandlerDispatch:
    def test_copy_action_disabled_raises(self, monkeypatch):
        monkeypatch.setattr(ami_transfer, "ENABLE_AMI_COPY", False)
        with pytest.raises(ValueError, match="disabled by Lambda environment"):
            ami_transfer.lambda_handler({"action": "copy_latest_ami"}, None)

    def test_export_rejects_caller_supplied_bucket(self, monkeypatch):
        monkeypatch.setattr(ami_transfer, "ENABLE_AMI_EXPORT", True)
        with pytest.raises(ValueError, match="s3_bucket is configured"):
            ami_transfer.lambda_handler({"action": "export_latest_ami", "s3_bucket": "evil"}, None)

    def test_unsupported_action_raises(self):
        with pytest.raises(ValueError, match="Unsupported action"):
            ami_transfer.lambda_handler({"action": "nope"}, None)


class FakeExportEc2:
    """Records the export_image request so tests can assert what would be sent."""

    def __init__(self):
        self.export_image_kwargs = None

    def export_image(self, **kwargs):
        self.export_image_kwargs = kwargs
        return {"ExportImageTaskId": "export-ami-123"}


class TestExportLatestAmi:
    @pytest.fixture
    def fake(self, monkeypatch):
        # latest_managed_ami is exercised on its own below; isolate export here.
        monkeypatch.setattr(ami_transfer, "latest_managed_ami", lambda: {"ImageId": "ami-src", "Name": "workspace"})
        monkeypatch.setattr(ami_transfer, "VMIMPORT_ROLE_NAME", "vmimport")
        monkeypatch.setattr(ami_transfer, "MANAGER_TAG_KEY", "CreatedBy")
        monkeypatch.setattr(ami_transfer, "MANAGER_TAG_VALUE", "wp-scheduler")
        fake = FakeExportEc2()
        monkeypatch.setattr(ami_transfer, "ec2", fake)
        return fake

    def test_requires_bucket(self, fake):
        with pytest.raises(ValueError, match="s3_bucket is required"):
            ami_transfer.export_latest_ami("", "ami-exports", "VMDK")

    def test_prefix_gets_trailing_slash(self, fake):
        # ExportImage writes to "<S3Prefix><task-id>", so the prefix must end in
        # "/" or the artifact lands beside the folder, outside the lifecycle rule.
        result = ami_transfer.export_latest_ami("bkt", "ami-exports", "VMDK")
        assert fake.export_image_kwargs["S3ExportLocation"]["S3Prefix"] == "ami-exports/"
        assert result["s3_prefix"] == "ami-exports/"

    def test_existing_trailing_slash_is_not_doubled(self, fake):
        ami_transfer.export_latest_ami("bkt", "ami-exports/", "VMDK")
        assert fake.export_image_kwargs["S3ExportLocation"]["S3Prefix"] == "ami-exports/"

    def test_export_request_fields(self, fake):
        result = ami_transfer.export_latest_ami("bkt", "sub/dir", "VHD")
        kwargs = fake.export_image_kwargs
        assert kwargs["DiskImageFormat"] == "VHD"
        assert kwargs["ImageId"] == "ami-src"
        assert kwargs["S3ExportLocation"]["S3Bucket"] == "bkt"
        assert kwargs["RoleName"] == "vmimport"
        assert kwargs["TagSpecifications"][0]["ResourceType"] == "export-image-task"
        tags = {t["Key"]: t["Value"] for t in kwargs["TagSpecifications"][0]["Tags"]}
        assert tags["CreatedBy"] == "wp-scheduler"
        assert tags["SourceImageId"] == "ami-src"
        assert result["export_image_task_id"] == "export-ami-123"
        assert result["disk_format"] == "VHD"


class TestLatestManagedAmi:
    def _patch_images(self, monkeypatch, pages):
        class Paginator:
            def paginate(self, **kwargs):
                yield from pages

        class Ec2:
            def get_paginator(self, name):
                assert name == "describe_images"
                return Paginator()

        monkeypatch.setattr(ami_transfer, "MANAGED_INSTANCE_ID", "i-123")
        monkeypatch.setattr(ami_transfer, "ec2", Ec2())

    def test_requires_managed_instance_id(self, monkeypatch):
        monkeypatch.setattr(ami_transfer, "MANAGED_INSTANCE_ID", "")
        with pytest.raises(ValueError, match="MANAGED_INSTANCE_ID is required"):
            ami_transfer.latest_managed_ami()

    def test_raises_when_no_managed_amis(self, monkeypatch):
        self._patch_images(monkeypatch, [{"Images": []}])
        with pytest.raises(ValueError, match="No managed AMIs found"):
            ami_transfer.latest_managed_ami()

    def test_returns_newest_by_creation_date(self, monkeypatch):
        # Order is intentionally shuffled so the test fails if selection isn't
        # actually sorting by CreationDate descending.
        self._patch_images(monkeypatch, [{"Images": [
            {"ImageId": "ami-old", "CreationDate": "2026-01-01T00:00:00.000Z"},
            {"ImageId": "ami-new", "CreationDate": "2026-06-01T00:00:00.000Z"},
            {"ImageId": "ami-mid", "CreationDate": "2026-03-01T00:00:00.000Z"},
        ]}])
        assert ami_transfer.latest_managed_ami()["ImageId"] == "ami-new"
