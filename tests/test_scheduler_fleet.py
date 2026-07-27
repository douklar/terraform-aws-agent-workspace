"""Which instances the scheduler is allowed to touch.

Anything found here can be stopped, imaged, snapshotted and patched, so the tag
filter matters. So does paging: describe_instances caps at 1000 results, and an
unpaginated read would simply skip the rest of the fleet.
"""

import pytest
from botocore.exceptions import ClientError

import scheduler


class FakePaginator:
    def __init__(self, pages, recorder):
        self._pages = pages
        self._recorder = recorder

    def paginate(self, **kwargs):
        self._recorder.append(kwargs)
        yield from self._pages


class FakeEc2:
    def __init__(self, pages=None, describe_result=None, describe_error=None):
        self._pages = pages or []
        self._describe_result = describe_result
        self._describe_error = describe_error
        self.paginate_kwargs = []
        self.described_ids = []

    def get_paginator(self, name):
        assert name == "describe_instances"
        return FakePaginator(self._pages, self.paginate_kwargs)

    def describe_instances(self, InstanceIds=None, **_kwargs):
        self.described_ids.append(InstanceIds)
        if self._describe_error is not None:
            raise self._describe_error
        return self._describe_result or {"Reservations": []}


def reservation(*instances):
    return {"Instances": list(instances)}


def inst(instance_id, state="running"):
    return {"InstanceId": instance_id, "State": {"Name": state}}


@pytest.fixture(autouse=True)
def no_extra_instance(monkeypatch):
    # Tests opt into the MANAGED_INSTANCE_ID fallback themselves.
    monkeypatch.setattr(scheduler, "MANAGED_INSTANCE_ID", "")


class TestManagedInstances:
    def test_filters_on_the_scheduler_tag_key(self, monkeypatch):
        fake = FakeEc2(pages=[{"Reservations": [reservation(inst("i-1"))]}])
        monkeypatch.setattr(scheduler, "ec2", fake)

        scheduler.managed_instances()

        filters = {f["Name"]: f["Values"] for f in fake.paginate_kwargs[0]["Filters"]}
        assert filters["tag-key"] == [scheduler.SCHEDULER_MODE_TAG_KEY]

    def test_defaults_to_live_states_only(self, monkeypatch):
        fake = FakeEc2(pages=[{"Reservations": []}])
        monkeypatch.setattr(scheduler, "ec2", fake)

        scheduler.managed_instances()

        filters = {f["Name"]: f["Values"] for f in fake.paginate_kwargs[0]["Filters"]}
        # Terminated instances must never enter the fleet.
        assert filters["instance-state-name"] == scheduler.LIVE_INSTANCE_STATES
        assert "terminated" not in filters["instance-state-name"]

    def test_explicit_states_override_the_default(self, monkeypatch):
        fake = FakeEc2(pages=[{"Reservations": []}])
        monkeypatch.setattr(scheduler, "ec2", fake)

        scheduler.managed_instances(states=["running"])

        filters = {f["Name"]: f["Values"] for f in fake.paginate_kwargs[0]["Filters"]}
        assert filters["instance-state-name"] == ["running"]

    def test_collects_across_pages_and_reservations(self, monkeypatch):
        # An unpaginated read would return only i-1 and silently drop the rest.
        fake = FakeEc2(pages=[
            {"Reservations": [reservation(inst("i-1"), inst("i-2"))]},
            {"Reservations": [reservation(inst("i-3"))]},
        ])
        monkeypatch.setattr(scheduler, "ec2", fake)

        found = [i["InstanceId"] for i in scheduler.managed_instances()]

        assert found == ["i-1", "i-2", "i-3"]

    def test_duplicates_across_pages_are_collapsed(self, monkeypatch):
        # A repeated instance would otherwise be stopped (or imaged) twice.
        fake = FakeEc2(pages=[
            {"Reservations": [reservation(inst("i-1"))]},
            {"Reservations": [reservation(inst("i-1"))]},
        ])
        monkeypatch.setattr(scheduler, "ec2", fake)

        assert [i["InstanceId"] for i in scheduler.managed_instances()] == ["i-1"]

    def test_own_instance_is_added_when_its_tag_has_not_propagated(self, monkeypatch):
        # Tags take a moment to show up after apply, so the module's own
        # instance is added by ID as well.
        fake = FakeEc2(
            pages=[{"Reservations": []}],
            describe_result={"Reservations": [reservation(inst("i-own"))]},
        )
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(scheduler, "MANAGED_INSTANCE_ID", "i-own")

        assert [i["InstanceId"] for i in scheduler.managed_instances()] == ["i-own"]

    def test_own_instance_is_not_added_twice(self, monkeypatch):
        fake = FakeEc2(pages=[{"Reservations": [reservation(inst("i-own"))]}])
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(scheduler, "MANAGED_INSTANCE_ID", "i-own")

        assert [i["InstanceId"] for i in scheduler.managed_instances()] == ["i-own"]
        assert fake.described_ids == []  # already found by tag; no extra lookup

    def test_own_instance_in_a_wrong_state_is_excluded(self, monkeypatch):
        # The AMI path asks for running only, so a stopped box must not appear.
        fake = FakeEc2(
            pages=[{"Reservations": []}],
            describe_result={"Reservations": [reservation(inst("i-own", state="stopped"))]},
        )
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(scheduler, "MANAGED_INSTANCE_ID", "i-own")

        assert scheduler.managed_instances(states=["running"]) == []

    def test_missing_own_instance_is_not_fatal(self, monkeypatch):
        # Terraform destroy can remove the instance while schedules still fire.
        fake = FakeEc2(
            pages=[{"Reservations": []}],
            describe_error=ClientError(
                {"Error": {"Code": "InvalidInstanceID.NotFound"}}, "DescribeInstances"
            ),
        )
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(scheduler, "MANAGED_INSTANCE_ID", "i-gone")

        assert scheduler.managed_instances() == []


class TestDescribeInstance:
    def test_returns_none_for_a_deleted_instance(self, monkeypatch):
        fake = FakeEc2(describe_error=ClientError(
            {"Error": {"Code": "InvalidInstanceID.NotFound"}}, "DescribeInstances"
        ))
        monkeypatch.setattr(scheduler, "ec2", fake)

        assert scheduler.describe_instance("i-gone") is None

    def test_other_client_errors_propagate(self, monkeypatch):
        # Treating AccessDenied as "not found" would make a broken permission
        # look like an empty fleet.
        fake = FakeEc2(describe_error=ClientError(
            {"Error": {"Code": "UnauthorizedOperation"}}, "DescribeInstances"
        ))
        monkeypatch.setattr(scheduler, "ec2", fake)

        with pytest.raises(ClientError):
            scheduler.describe_instance("i-1")

    def test_returns_none_when_response_is_empty(self, monkeypatch):
        monkeypatch.setattr(scheduler, "ec2", FakeEc2(describe_result={"Reservations": []}))
        assert scheduler.describe_instance("i-1") is None


class TestInstanceState:
    def test_reads_the_state_name(self, monkeypatch):
        fake = FakeEc2(describe_result={"Reservations": [reservation(inst("i-1", state="stopped"))]})
        monkeypatch.setattr(scheduler, "ec2", fake)

        assert scheduler.instance_state("i-1") == "stopped"

    def test_raises_when_instance_is_gone(self, monkeypatch):
        monkeypatch.setattr(scheduler, "ec2", FakeEc2(describe_result={"Reservations": []}))

        with pytest.raises(ValueError, match="Instance not found"):
            scheduler.instance_state("i-1")
