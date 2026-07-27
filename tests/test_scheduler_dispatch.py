"""Action routing in lambda_handler.

Each schedule in main.tf sends an action name. If the handler does not know it,
the invocation fails into the DLQ and that job never runs.
"""

import pytest

import scheduler


@pytest.fixture
def stub_actions(monkeypatch):
    """Swap each action for a recorder, so only the routing is tested.

    The implementations have their own tests.
    """
    calls = []

    def recorder(name, result):
        def _fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result

        return _fn

    monkeypatch.setattr(scheduler, "manage_instances", recorder("manage_instances", {"count": 1, "instances": []}))
    monkeypatch.setattr(scheduler, "enforce_schedule", recorder("enforce_schedule", {"count": 2, "instances": []}))
    monkeypatch.setattr(scheduler, "create_amis", recorder("create_amis", ["ami-1"]))
    monkeypatch.setattr(scheduler, "cleanup_amis", recorder("cleanup_amis", {"cleaned": ["ami-old"], "errors": []}))
    monkeypatch.setattr(scheduler, "cleanup_daily_snapshots", recorder("cleanup_daily_snapshots", ["snap-old"]))
    monkeypatch.setattr(scheduler, "create_daily_snapshots", recorder("create_daily_snapshots", ["snap-1"]))
    monkeypatch.setattr(scheduler, "run_security_update", recorder("run_security_update", {"command_id": "cmd-1"}))
    return calls


class TestStartStopRouting:
    @pytest.mark.parametrize("action", ["start", "stop"])
    def test_routes_to_manage_instances(self, stub_actions, action):
        result = scheduler.lambda_handler({"action": action}, None)

        assert [c[0] for c in stub_actions] == ["manage_instances"]
        assert result["status"] == "ok"
        assert result["action"] == action

    def test_forwards_the_cohort_from_the_event(self, stub_actions):
        # main.tf puts scheduler_mode in each window's payload. Lose it here and
        # every window's cron acts on every cohort.
        event = {"action": "stop", "scheduler_mode": "team-alpha"}
        scheduler.lambda_handler(event, None)

        _name, args, _kwargs = stub_actions[0]
        assert args[0] == "stop"
        assert args[1] == event


class TestReconcileRouting:
    @pytest.mark.parametrize("action", ["enforce_schedule", "reconcile_schedule"])
    def test_both_spellings_reach_reconcile(self, stub_actions, action):
        # main.tf sends enforce_schedule. The alias keeps hand-run payloads working.
        result = scheduler.lambda_handler({"action": action}, None)

        assert [c[0] for c in stub_actions] == ["enforce_schedule"]
        assert result["count"] == 2


class TestBackupRouting:
    def test_create_ami_passes_backup_type(self, stub_actions):
        result = scheduler.lambda_handler({"action": "create_ami", "backup_type": "monthly"}, None)

        assert stub_actions[0][1][0] == "monthly"
        assert result["created_images"] == ["ami-1"]

    def test_create_ami_defaults_to_daily(self, stub_actions):
        # Falls back to daily, the shortest retention window.
        scheduler.lambda_handler({"action": "create_ami"}, None)

        assert stub_actions[0][1][0] == "daily"

    def test_create_daily_snapshots_routes(self, stub_actions):
        result = scheduler.lambda_handler({"action": "create_daily_snapshots"}, None)

        assert [c[0] for c in stub_actions] == ["create_daily_snapshots"]
        assert result["created_snapshots"] == ["snap-1"]

    def test_cleanup_runs_both_amis_and_snapshots(self, stub_actions):
        # One cron drives both sweeps; dropping either leaks storage cost.
        result = scheduler.lambda_handler({"action": "cleanup_amis"}, None)

        assert [c[0] for c in stub_actions] == ["cleanup_amis", "cleanup_daily_snapshots"]
        assert result["cleaned_images"] == ["ami-old"]
        assert result["cleaned_snapshots"] == ["snap-old"]
        assert result["cleanup_errors"] == []

    def test_cleanup_surfaces_partial_failures(self, monkeypatch, stub_actions):
        monkeypatch.setattr(
            scheduler, "cleanup_amis", lambda: {"cleaned": [], "errors": ["failed to deregister ami-x"]}
        )

        result = scheduler.lambda_handler({"action": "cleanup_amis"}, None)

        # Errors must reach the response, not be swallowed into a green run.
        assert result["cleanup_errors"] == ["failed to deregister ami-x"]


class TestMaintenanceRouting:
    def test_security_update_routes(self, stub_actions):
        result = scheduler.lambda_handler({"action": "security_update"}, None)

        assert [c[0] for c in stub_actions] == ["run_security_update"]
        assert result["command_id"] == "cmd-1"


class TestUnknownActions:
    @pytest.mark.parametrize("event", [{}, None, {"action": None}, {"action": "drop_database"}])
    def test_unknown_action_raises(self, stub_actions, event):
        # Raising puts the invocation in the DLQ. Returning quietly would hide a
        # broken schedule for good.
        with pytest.raises(ValueError, match="Unsupported action"):
            scheduler.lambda_handler(event, None)

        assert stub_actions == []
