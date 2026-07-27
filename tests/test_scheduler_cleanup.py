"""AMI and snapshot cleanup in scheduler.py.

These functions delete real backups, so the risky paths get explicit tests:

  * the retention cutoff math,
  * the BackupType branch that picks which window applies, and
  * the "never delete a snapshot another live AMI still uses" guard, plus its
    fail-safe when the reference check itself errors.

Everything runs against an in-memory fake EC2 client - no AWS calls, no wall
clock dependence beyond "N days ago is older than an N-minus-something cutoff",
so the suite is deterministic and offline.
"""

from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

import scheduler


def iso_z(dt):
    # AWS AMI CreationDate format, e.g. 2026-05-25T01:30:04.000Z
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ami(image_id, age_days, backup_type="weekly", snapshot_id="snap-aaa"):
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    return {
        "ImageId": image_id,
        "CreationDate": iso_z(created),
        "Tags": [{"Key": "BackupType", "Value": backup_type}],
        "BlockDeviceMappings": [
            {"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": snapshot_id}}
        ],
    }


class FakePaginator:
    def __init__(self, pages, raise_error=None):
        self._pages = pages
        self._raise = raise_error

    def paginate(self, **kwargs):
        if self._raise is not None:
            raise self._raise
        yield from self._pages


class FakeEc2:
    """Records the destructive calls the cleanup logic makes so tests can assert
    exactly what would have been deleted at AWS."""

    def __init__(self, image_pages=None, snapshot_pages=None, images_error=None):
        self._image_pages = image_pages or []
        self._snapshot_pages = snapshot_pages or []
        self._images_error = images_error
        self.deregistered = []
        self.deleted_snapshots = []
        self.create_snapshot_calls = []

    def get_paginator(self, name):
        if name == "describe_images":
            return FakePaginator(self._image_pages, raise_error=self._images_error)
        if name == "describe_snapshots":
            return FakePaginator(self._snapshot_pages)
        raise AssertionError(f"unexpected paginator requested: {name}")

    def deregister_image(self, ImageId):
        self.deregistered.append(ImageId)
        return {}

    def delete_snapshot(self, SnapshotId):
        self.deleted_snapshots.append(SnapshotId)
        return {}

    def create_snapshot(self, **kwargs):
        self.create_snapshot_calls.append(kwargs)
        return {"SnapshotId": f"snap-for-{kwargs['VolumeId']}"}


@pytest.fixture
def retention(monkeypatch):
    # Deliberately distinct windows so the BackupType branch selection is
    # observable: an age between two windows lands on opposite sides of them.
    monkeypatch.setattr(scheduler, "DAILY_RETENTION_DAYS", 7)
    monkeypatch.setattr(scheduler, "WEEKLY_RETENTION_DAYS", 30)
    monkeypatch.setattr(scheduler, "MONTHLY_RETENTION_DAYS", 90)
    monkeypatch.setattr(scheduler, "DRY_RUN", False)


class TestIsSnapshotReferenced:
    def _patch(self, monkeypatch, pages=None, error=None):
        monkeypatch.setattr(scheduler, "ec2", FakeEc2(image_pages=pages, images_error=error))

    def test_true_when_another_ami_uses_snapshot(self, monkeypatch):
        self._patch(monkeypatch, pages=[{"Images": [{"ImageId": "ami-other"}]}])
        assert scheduler.is_snapshot_referenced("snap-1", exclude_image_id="ami-self") is True

    def test_false_when_only_excluded_ami_uses_snapshot(self, monkeypatch):
        # The one AMI referencing it is the very image being deregistered, so the
        # snapshot is safe to delete.
        self._patch(monkeypatch, pages=[{"Images": [{"ImageId": "ami-self"}]}])
        assert scheduler.is_snapshot_referenced("snap-1", exclude_image_id="ami-self") is False

    def test_false_when_no_ami_uses_snapshot(self, monkeypatch):
        self._patch(monkeypatch, pages=[{"Images": []}])
        assert scheduler.is_snapshot_referenced("snap-1", exclude_image_id="ami-self") is False

    def test_fails_safe_to_referenced_on_api_error(self, monkeypatch):
        # If the reference check itself fails, the code must assume the snapshot
        # IS referenced so a live snapshot is never deleted on incomplete info.
        err = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DescribeImages")
        self._patch(monkeypatch, error=err)
        assert scheduler.is_snapshot_referenced("snap-1", exclude_image_id="ami-self") is True


class TestCleanupAmis:
    def _run(self, monkeypatch, images, referenced=False):
        fake = FakeEc2(image_pages=[{"Images": images}])
        monkeypatch.setattr(scheduler, "ec2", fake)
        # is_snapshot_referenced is verified on its own above; isolate the
        # orchestration here by controlling its result directly.
        monkeypatch.setattr(scheduler, "is_snapshot_referenced", lambda *a, **k: referenced)
        return fake, scheduler.cleanup_amis()

    def test_expired_weekly_ami_is_deregistered(self, retention, monkeypatch):
        fake, result = self._run(monkeypatch, [ami("ami-old", age_days=60, backup_type="weekly")])
        assert fake.deregistered == ["ami-old"]
        assert result["cleaned"] == ["ami-old"]

    def test_fresh_ami_is_kept(self, retention, monkeypatch):
        fake, result = self._run(monkeypatch, [ami("ami-new", age_days=1, backup_type="weekly")])
        assert fake.deregistered == []
        assert result["cleaned"] == []

    def test_weekly_ami_inside_weekly_window_is_kept(self, retention, monkeypatch):
        # 15 days is past the 7-day daily window but inside the 30-day weekly one.
        # If the weekly branch were mis-selected and fell through to the daily
        # cutoff, this AMI would be wrongly deleted - so this pins the weekly
        # branch specifically (distinct from the daily fallback).
        fake, _ = self._run(monkeypatch, [ami("ami-w15", age_days=15, backup_type="weekly")])
        assert fake.deregistered == []

    def test_backuptype_branch_keeps_monthly_that_weekly_would_delete(self, retention, monkeypatch):
        # 60 days is past the 30-day weekly window but inside the 90-day monthly
        # one. A weekly-tagged AMI this age is deleted (asserted above); a
        # monthly-tagged one must survive - proving the branch changes the cutoff.
        fake, _ = self._run(monkeypatch, [ami("ami-m", age_days=60, backup_type="monthly")])
        assert fake.deregistered == []

    def test_unknown_backuptype_uses_daily_window(self, retention, monkeypatch):
        # 10 days old, unrecognized type -> daily (7-day) window -> expired.
        fake, _ = self._run(monkeypatch, [ami("ami-x", age_days=10, backup_type="mystery")])
        assert fake.deregistered == ["ami-x"]

    def test_unreferenced_snapshot_is_deleted(self, retention, monkeypatch):
        fake, _ = self._run(
            monkeypatch, [ami("ami-old", age_days=60, snapshot_id="snap-x")], referenced=False
        )
        assert fake.deleted_snapshots == ["snap-x"]

    def test_referenced_snapshot_is_not_deleted(self, retention, monkeypatch):
        # The core data-loss guard: the expired AMI is removed, but its snapshot
        # still backs another live AMI, so the snapshot must NOT be deleted.
        fake, _ = self._run(
            monkeypatch, [ami("ami-old", age_days=60, snapshot_id="snap-shared")], referenced=True
        )
        assert fake.deregistered == ["ami-old"]
        assert fake.deleted_snapshots == []

    def test_dry_run_deletes_nothing_but_reports(self, retention, monkeypatch):
        monkeypatch.setattr(scheduler, "DRY_RUN", True)
        fake, result = self._run(monkeypatch, [ami("ami-old", age_days=60)])
        assert fake.deregistered == []
        assert fake.deleted_snapshots == []
        # Still reported as a would-clean so the run's output is truthful.
        assert result["cleaned"] == ["ami-old"]


class TestCleanupDailySnapshots:
    def _run(self, monkeypatch, snapshots):
        fake = FakeEc2(snapshot_pages=[{"Snapshots": snapshots}])
        monkeypatch.setattr(scheduler, "ec2", fake)
        return fake, scheduler.cleanup_daily_snapshots()

    def test_old_snapshot_deleted_new_kept(self, retention, monkeypatch):
        now = datetime.now(timezone.utc)
        snaps = [
            {"SnapshotId": "snap-old", "StartTime": now - timedelta(days=30)},
            {"SnapshotId": "snap-new", "StartTime": now - timedelta(days=1)},
        ]
        fake, cleaned = self._run(monkeypatch, snaps)
        assert fake.deleted_snapshots == ["snap-old"]
        assert cleaned == ["snap-old"]

    def test_naive_starttime_treated_as_utc(self, retention, monkeypatch):
        # boto3 can hand back tz-naive datetimes; cleanup must not crash on the
        # comparison and must still apply the cutoff.
        naive_old = (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None)
        fake, cleaned = self._run(monkeypatch, [{"SnapshotId": "snap-naive", "StartTime": naive_old}])
        assert cleaned == ["snap-naive"]


class TestCreateDailySnapshots:
    def test_only_root_volume_is_snapshotted(self, monkeypatch):
        # The Lambda's IAM policy grants CreateSnapshot on the root volume only;
        # snapshotting an attached data volume would fail with AccessDenied, so
        # the function must skip non-root block devices.
        fake = FakeEc2()
        monkeypatch.setattr(scheduler, "ec2", fake)
        instance = {
            "InstanceId": "i-123",
            "RootDeviceName": "/dev/sda1",
            "BlockDeviceMappings": [
                {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-root"}},
                {"DeviceName": "/dev/sdf", "Ebs": {"VolumeId": "vol-data"}},
            ],
        }
        monkeypatch.setattr(scheduler, "managed_instances", lambda *a, **k: [instance])

        created = scheduler.create_daily_snapshots()

        assert [c["VolumeId"] for c in fake.create_snapshot_calls] == ["vol-root"]
        assert created == ["snap-for-vol-root"]
