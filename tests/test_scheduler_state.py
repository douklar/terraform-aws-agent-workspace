"""Start/stop and reconcile logic.

A missed stop bills around the clock, a wrong stop kills a live session. EC2
also refuses a start while stopping, so those waits are covered too.

The EC2 client is a fake. No AWS calls, no real sleeping.
"""

from datetime import datetime, timezone

import pytest

import scheduler


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    """Move time forward on each sleep instead of really waiting.

    wait_for_instance_state loops until time.monotonic() passes a deadline, so
    stubbing sleep alone would leave a real 45-second wait.
    """
    clock = {"t": 0.0}

    def advance(seconds=0):
        clock["t"] += max(seconds, 1)

    monkeypatch.setattr(scheduler.time, "sleep", advance)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock["t"])


@pytest.fixture(autouse=True)
def live_run(monkeypatch):
    # Tests opt into DRY_RUN themselves.
    monkeypatch.setattr(scheduler, "DRY_RUN", False)


class FakeEc2:
    """Returns the given states one per describe, then repeats the last one.

    That lets a test show an instance stopping on the first look and stopped on
    the next. Start and stop calls are recorded.
    """

    def __init__(self, states):
        self._states = list(states)
        self.started = []
        self.stopped = []
        self.describe_calls = 0

    def _next_state(self):
        self.describe_calls += 1
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]

    def describe_instances(self, InstanceIds=None, **_kwargs):
        return {
            "Reservations": [
                {"Instances": [{"InstanceId": InstanceIds[0], "State": {"Name": self._next_state()}}]}
            ]
        }

    def start_instances(self, InstanceIds):
        self.started.extend(InstanceIds)
        return {}

    def stop_instances(self, InstanceIds):
        self.stopped.extend(InstanceIds)
        return {}


def instance(instance_id="i-1", state="running", mode="free-time"):
    tags = [] if mode is None else [{"Key": "scheduler", "Value": mode}]
    return {"InstanceId": instance_id, "State": {"Name": state}, "Tags": tags}


class TestStartInstance:
    def test_stopped_instance_is_started(self, monkeypatch):
        fake = FakeEc2(["stopped"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.start_instance("i-1", "scheduled_start", initial_state="stopped")

        assert fake.started == ["i-1"]
        assert result["result"] == "start_requested"

    @pytest.mark.parametrize("state", ["running", "pending", "shutting-down", "terminated"])
    def test_non_stopped_states_are_left_alone(self, monkeypatch, state):
        fake = FakeEc2([state])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.start_instance("i-1", "scheduled_start", initial_state=state)

        assert fake.started == []
        assert result["result"] == "no_action"

    def test_stopping_instance_is_waited_out_then_started(self, monkeypatch):
        # EC2 refuses a start while the instance is stopping, so wait first.
        fake = FakeEc2(["stopping", "stopped"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.start_instance("i-1", "scheduled_start", initial_state="stopping")

        assert fake.started == ["i-1"]
        assert result["previous_state"] == "stopping"
        assert result["evaluated_state"] == "stopped"

    def test_start_is_deferred_when_still_stopping_after_wait(self, monkeypatch):
        # Still stopping at the timeout, so leave it for the next reconcile.
        fake = FakeEc2(["stopping"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.start_instance("i-1", "scheduled_start", initial_state="stopping")

        assert fake.started == []
        assert result["result"] == "stopping_start_deferred"

    def test_dry_run_reports_without_starting(self, monkeypatch):
        monkeypatch.setattr(scheduler, "DRY_RUN", True)
        fake = FakeEc2(["stopped"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.start_instance("i-1", "scheduled_start", initial_state="stopped")

        assert fake.started == []
        assert result["result"] == "dry_run_start"

    def test_state_is_looked_up_when_not_supplied(self, monkeypatch):
        fake = FakeEc2(["stopped"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        scheduler.start_instance("i-1", "scheduled_start")

        assert fake.describe_calls >= 1
        assert fake.started == ["i-1"]


class TestStopInstance:
    def test_running_instance_is_stopped(self, monkeypatch):
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.stop_instance("i-1", "scheduled_stop", initial_state="running")

        assert fake.stopped == ["i-1"]
        assert result["result"] == "stop_requested"

    @pytest.mark.parametrize("state", ["stopped", "stopping", "shutting-down", "terminated"])
    def test_non_running_states_are_left_alone(self, monkeypatch, state):
        fake = FakeEc2([state])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.stop_instance("i-1", "scheduled_stop", initial_state=state)

        assert fake.stopped == []
        assert result["result"] == "no_action"

    def test_pending_instance_is_waited_out_then_stopped(self, monkeypatch):
        fake = FakeEc2(["pending", "running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.stop_instance("i-1", "scheduled_stop", initial_state="pending")

        assert fake.stopped == ["i-1"]
        assert result["evaluated_state"] == "running"

    def test_stop_is_deferred_when_still_pending_after_wait(self, monkeypatch):
        fake = FakeEc2(["pending"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.stop_instance("i-1", "scheduled_stop", initial_state="pending")

        assert fake.stopped == []
        assert result["result"] == "pending_stop_deferred"

    def test_dry_run_reports_without_stopping(self, monkeypatch):
        monkeypatch.setattr(scheduler, "DRY_RUN", True)
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.stop_instance("i-1", "scheduled_stop", initial_state="running")

        assert fake.stopped == []
        assert result["result"] == "dry_run_stop"


class TestWaitForInstanceState:
    def test_returns_as_soon_as_target_reached(self, monkeypatch):
        fake = FakeEc2(["stopping", "stopping", "stopped"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        state = scheduler.wait_for_instance_state("i-1", {"stopped"}, timeout_seconds=60, poll_seconds=0)

        assert state == "stopped"

    def test_gives_up_at_timeout_with_last_state(self, monkeypatch):
        fake = FakeEc2(["stopping"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        state = scheduler.wait_for_instance_state("i-1", {"stopped"}, timeout_seconds=0, poll_seconds=0)

        # Must return rather than loop forever - the Lambda has a hard timeout.
        assert state == "stopping"


class TestManageOneCohortDispatch:
    """manage_one decides whether a scheduled event applies to this instance."""

    def test_disabled_instance_is_never_touched(self, monkeypatch):
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.manage_one(instance(mode="disabled"), "stop", "free-time")

        assert result["result"] == "skipped_disabled"
        assert fake.stopped == [] and fake.started == []

    def test_on_demand_instance_is_never_stopped(self, monkeypatch):
        # on-demand means the scheduler may start it but never stop it.
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.manage_one(instance(mode="on-demand"), "stop", None)

        assert result["result"] == "skipped_on_demand_stop"
        assert fake.stopped == []

    def test_on_demand_instance_is_started(self, monkeypatch):
        fake = FakeEc2(["stopped"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.manage_one(instance(state="stopped", mode="on-demand"), "start", None)

        assert fake.started == ["i-1"]
        assert result["reason"] == "on_demand_start"

    def test_other_cohorts_event_does_not_stop_this_instance(self, monkeypatch):
        # Each window's stop cron reaches the whole fleet, so the cohort filter
        # is what keeps one schedule off another cohort's instances.
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.manage_one(instance(mode="team-alpha"), "stop", "free-time")

        assert result["result"] == "skipped_mode_mismatch"
        assert fake.stopped == []

    def test_matching_cohort_event_stops_the_instance(self, monkeypatch):
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.manage_one(instance(mode="team-alpha"), "stop", "team-alpha")

        assert fake.stopped == ["i-1"]
        assert result["reason"] == "scheduled_stop"

    def test_event_without_cohort_applies_to_every_scheduled_instance(self, monkeypatch):
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        result = scheduler.manage_one(instance(mode="team-alpha"), "stop", None)

        assert fake.stopped == ["i-1"]
        assert result["scheduler_mode"] == "team-alpha"

    def test_unsupported_action_raises(self):
        with pytest.raises(ValueError, match="Unsupported instance action"):
            scheduler.manage_one(instance(), "explode", None)


class TestManageInstances:
    def test_fans_out_over_the_managed_fleet(self, monkeypatch):
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(
            scheduler,
            "managed_instances",
            lambda *a, **k: [instance("i-1"), instance("i-2")],
        )

        result = scheduler.manage_instances("stop", {"scheduler_mode": "free-time"})

        assert result["count"] == 2
        assert sorted(fake.stopped) == ["i-1", "i-2"]


class TestEnforceOneReconcile:
    """The 15-minute reconcile is the authority on what should be running."""

    @pytest.fixture
    def now(self):
        return datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)  # a Monday

    def _windows(self, monkeypatch, allowed):
        monkeypatch.setattr(scheduler, "active_allowed_windows", lambda mode: [{"stub": True}])
        monkeypatch.setattr(scheduler, "is_allowed_time", lambda *_a, **_k: allowed)

    def test_starts_instance_inside_its_window(self, monkeypatch, now):
        fake = FakeEc2(["stopped"])
        monkeypatch.setattr(scheduler, "ec2", fake)
        self._windows(monkeypatch, allowed=True)

        result = scheduler.enforce_one(instance(state="stopped"), now)

        assert fake.started == ["i-1"]
        assert result["allowed_now"] is True

    def test_stops_instance_outside_its_window(self, monkeypatch, now):
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)
        self._windows(monkeypatch, allowed=False)

        result = scheduler.enforce_one(instance(state="running"), now)

        assert fake.stopped == ["i-1"]
        assert result["allowed_now"] is False
        assert result["reason"] == "outside_allowed_window"

    def test_disabled_instance_is_skipped(self, monkeypatch, now):
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)
        self._windows(monkeypatch, allowed=False)

        result = scheduler.enforce_one(instance(state="running", mode="disabled"), now)

        assert result["result"] == "skipped_disabled"
        assert fake.stopped == []

    def test_on_demand_instance_is_kept_running(self, monkeypatch, now):
        fake = FakeEc2(["stopped"])
        monkeypatch.setattr(scheduler, "ec2", fake)
        self._windows(monkeypatch, allowed=False)

        result = scheduler.enforce_one(instance(state="stopped", mode="on-demand"), now)

        # Reconcile restarts an on-demand box even outside every window.
        assert fake.started == ["i-1"]
        assert result["reason"] == "on_demand_reconcile"

    def test_unresolvable_cohort_leaves_instance_untouched(self, monkeypatch, now):
        # No windows for this cohort, so leave the instance alone rather than
        # guess. It may be in use.
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)

        def no_windows(_mode):
            raise ValueError("No instance_schedule_windows configured for scheduler mode: ghost")

        monkeypatch.setattr(scheduler, "active_allowed_windows", no_windows)

        result = scheduler.enforce_one(instance(state="running", mode="ghost"), now)

        assert result["result"] == "schedule_error_no_action"
        assert fake.stopped == [] and fake.started == []

    def test_bad_timezone_leaves_instance_untouched(self, monkeypatch, now):
        # An unknown timezone raises too, and is handled the same way.
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(
            scheduler,
            "active_allowed_windows",
            lambda _m: [{"days": ["MON"], "timezone": "Mars/Olympus", "start_time": "09:00", "stop_time": "17:00"}],
        )

        result = scheduler.enforce_one(instance(state="running"), now)

        assert result["result"] == "schedule_error_no_action"
        assert fake.stopped == []


class TestEnforceSchedule:
    def test_reports_every_evaluated_instance(self, monkeypatch):
        fake = FakeEc2(["running"])
        monkeypatch.setattr(scheduler, "ec2", fake)
        monkeypatch.setattr(
            scheduler, "managed_instances", lambda *a, **k: [instance("i-1"), instance("i-2")]
        )
        monkeypatch.setattr(scheduler, "enforce_one", lambda inst, _now: {"instance_id": inst["InstanceId"]})

        result = scheduler.enforce_schedule()

        assert result["count"] == 2
        assert "evaluated_at" in result
