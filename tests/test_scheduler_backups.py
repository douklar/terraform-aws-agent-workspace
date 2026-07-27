"""AMI creation and the retry helper.

Cleanup only deletes what creation tagged, so a missing CreatedBy tag leaves the
AMI and its snapshots billing forever. retry_api_call has to retry throttling
but not permission errors.
"""

import pytest
from botocore.exceptions import ClientError

import scheduler


class FakeEc2:
    def __init__(self, image_id="ami-new"):
        self.create_image_calls = []
        self._image_id = image_id

    def create_image(self, **kwargs):
        self.create_image_calls.append(kwargs)
        return {"ImageId": self._image_id}


def running_instance(instance_id="i-1"):
    return {"InstanceId": instance_id, "State": {"Name": "running"}}


@pytest.fixture
def manager_tag(monkeypatch):
    monkeypatch.setattr(scheduler, "MANAGER_TAG_KEY", "CreatedBy")
    monkeypatch.setattr(scheduler, "MANAGER_TAG_VALUE", "wp-scheduler")


class TestCreateAmis:
    @pytest.fixture
    def fake(self, monkeypatch, manager_tag):
        fake = FakeEc2()
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(scheduler, "managed_instances", lambda states=None: [running_instance()])
        return fake

    def test_only_running_instances_are_imaged(self, monkeypatch, manager_tag):
        fake = FakeEc2()
        monkeypatch.setattr(scheduler, "ec2", fake)
        seen = {}

        def record(states=None):
            seen["states"] = states
            return []

        monkeypatch.setattr(scheduler, "managed_instances", record)
        scheduler.create_amis("weekly")

        # Imaging a stopped box works, but backs up a machine the user turned
        # off, at full snapshot cost.
        assert seen["states"] == ["running"]

    def test_image_is_tagged_for_cleanup(self, fake):
        scheduler.create_amis("weekly")

        specs = {s["ResourceType"]: s for s in fake.create_image_calls[0]["TagSpecifications"]}
        image_tags = {t["Key"]: t["Value"] for t in specs["image"]["Tags"]}
        assert image_tags["CreatedBy"] == "wp-scheduler"
        assert image_tags["BackupType"] == "weekly"
        assert image_tags["SourceInstanceId"] == "i-1"

    def test_backing_snapshots_are_tagged_too(self, fake):
        # Cleanup and the IAM delete rule both filter on CreatedBy, so an
        # untagged snapshot outlives its AMI.
        scheduler.create_amis("weekly")

        specs = {s["ResourceType"]: s for s in fake.create_image_calls[0]["TagSpecifications"]}
        assert "snapshot" in specs
        snapshot_tags = {t["Key"]: t["Value"] for t in specs["snapshot"]["Tags"]}
        assert snapshot_tags["CreatedBy"] == "wp-scheduler"

    def test_backup_type_reaches_the_tag(self, fake):
        # cleanup_amis picks the retention window from this tag.
        scheduler.create_amis("monthly")

        specs = {s["ResourceType"]: s for s in fake.create_image_calls[0]["TagSpecifications"]}
        image_tags = {t["Key"]: t["Value"] for t in specs["image"]["Tags"]}
        assert image_tags["BackupType"] == "monthly"

    def test_image_is_created_without_rebooting(self, fake):
        # A reboot would interrupt whatever is running on the instance.
        scheduler.create_amis("weekly")
        assert fake.create_image_calls[0]["NoReboot"] is True

    def test_name_identifies_instance_and_backup_type(self, fake):
        scheduler.create_amis("weekly")

        name = fake.create_image_calls[0]["Name"]
        assert name.startswith("i-1-weekly-")
        # AMI names are capped at 128 characters by EC2.
        assert len(name) <= 128

    def test_each_running_instance_gets_its_own_image(self, monkeypatch, manager_tag):
        fake = FakeEc2()
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(
            scheduler,
            "managed_instances",
            lambda states=None: [running_instance("i-1"), running_instance("i-2")],
        )

        created = scheduler.create_amis("weekly")

        assert len(created) == 2
        assert [c["InstanceId"] for c in fake.create_image_calls] == ["i-1", "i-2"]

    def test_empty_fleet_creates_nothing(self, monkeypatch, manager_tag):
        fake = FakeEc2()
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(scheduler, "managed_instances", lambda states=None: [])

        assert scheduler.create_amis("weekly") == []
        assert fake.create_image_calls == []


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "DeleteSnapshot")


class TestRetryApiCall:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr(scheduler.time, "sleep", lambda *_a, **_k: None)

    def test_returns_result_without_retrying_on_success(self):
        calls = []

        def ok():
            calls.append(1)
            return "done"

        assert scheduler.retry_api_call(ok) == "done"
        assert len(calls) == 1

    @pytest.mark.parametrize("code", ["Throttling", "RequestLimitExceeded", "InternalError"])
    def test_transient_errors_are_retried_then_succeed(self, code):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise client_error(code)
            return "recovered"

        assert scheduler.retry_api_call(flaky) == "recovered"
        assert len(attempts) == 3

    def test_transient_error_raises_after_exhausting_retries(self):
        attempts = []

        def always_throttled():
            attempts.append(1)
            raise client_error("Throttling")

        with pytest.raises(ClientError):
            scheduler.retry_api_call(always_throttled, max_retries=3)

        assert len(attempts) == 3

    def test_permanent_error_is_not_retried(self):
        # An AccessDenied cannot succeed on retry, it only burns the timeout.
        attempts = []

        def denied():
            attempts.append(1)
            raise client_error("UnauthorizedOperation")

        with pytest.raises(ClientError):
            scheduler.retry_api_call(denied)

        assert len(attempts) == 1

    def test_backoff_delay_grows_between_attempts(self, monkeypatch):
        delays = []
        monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: delays.append(seconds))

        def always_throttled():
            raise client_error("Throttling")

        with pytest.raises(ClientError):
            scheduler.retry_api_call(always_throttled, max_retries=4, base_delay=1)

        # Doubling, not flat: a fixed delay keeps hitting the same rate limit.
        assert delays == [1, 2, 4]
