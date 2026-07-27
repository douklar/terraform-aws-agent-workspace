"""Tests for run_security_update's SSM patch dispatch and status polling.

The function sends AWS-RunPatchBaseline to every running managed instance and
then polls each invocation to a terminal state. The behaviors worth pinning:
it must no-op when nothing is running, send the exact patch document/params,
tolerate the brief window where an invocation isn't registered yet
(InvocationDoesNotExist), and stop polling once every instance is terminal -
while any *other* SSM error still surfaces.

time.sleep is neutralized so the polling loop runs instantly and deterministically.
"""

import pytest
from botocore.exceptions import ClientError

import scheduler


class FakeSsm:
    def __init__(self, results):
        # results: {instance_id: [status_str | Exception, ...]} consumed per poll.
        self.send_command_kwargs = None
        self._results = {k: list(v) for k, v in results.items()}

    def send_command(self, **kwargs):
        self.send_command_kwargs = kwargs
        return {"Command": {"CommandId": "cmd-123"}}

    def get_command_invocation(self, CommandId, InstanceId):
        item = self._results[InstanceId].pop(0)
        if isinstance(item, Exception):
            raise item
        return {"Status": item}


def invocation_missing():
    return ClientError({"Error": {"Code": "InvocationDoesNotExist"}}, "GetCommandInvocation")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(scheduler.time, "sleep", lambda *_a, **_k: None)


def _running(monkeypatch, instance_ids):
    monkeypatch.setattr(
        scheduler, "managed_instances", lambda states=None: [{"InstanceId": i} for i in instance_ids]
    )


class TestRunSecurityUpdate:
    def test_skips_when_no_running_instances(self, monkeypatch):
        _running(monkeypatch, [])
        result = scheduler.run_security_update()
        assert result["status"] == "skipped"

    def test_sends_patch_baseline_command(self, monkeypatch):
        _running(monkeypatch, ["i-1"])
        fake = FakeSsm({"i-1": ["Success"]})
        monkeypatch.setattr(scheduler, "ssm", fake)

        result = scheduler.run_security_update()

        assert fake.send_command_kwargs["DocumentName"] == "AWS-RunPatchBaseline"
        assert fake.send_command_kwargs["InstanceIds"] == ["i-1"]
        assert fake.send_command_kwargs["Parameters"]["Operation"] == ["Install"]
        assert fake.send_command_kwargs["Parameters"]["RebootOption"] == ["NoReboot"]
        assert result["command_id"] == "cmd-123"
        assert result["statuses"] == {"i-1": "Success"}

    def test_polls_until_terminal(self, monkeypatch):
        _running(monkeypatch, ["i-1"])
        # First poll not terminal, second poll succeeds.
        monkeypatch.setattr(scheduler, "ssm", FakeSsm({"i-1": ["InProgress", "Success"]}))
        result = scheduler.run_security_update()
        assert result["statuses"] == {"i-1": "Success"}

    def test_tolerates_invocation_not_yet_registered(self, monkeypatch):
        _running(monkeypatch, ["i-1"])
        # InvocationDoesNotExist on the first poll must be swallowed, not raised.
        monkeypatch.setattr(scheduler, "ssm", FakeSsm({"i-1": [invocation_missing(), "Success"]}))
        result = scheduler.run_security_update()
        assert result["statuses"] == {"i-1": "Success"}

    def test_non_transient_ssm_error_propagates(self, monkeypatch):
        _running(monkeypatch, ["i-1"])
        denied = ClientError({"Error": {"Code": "AccessDenied"}}, "GetCommandInvocation")
        monkeypatch.setattr(scheduler, "ssm", FakeSsm({"i-1": [denied]}))
        with pytest.raises(ClientError):
            scheduler.run_security_update()

    def test_multiple_instances_all_reach_terminal(self, monkeypatch):
        _running(monkeypatch, ["i-1", "i-2"])
        monkeypatch.setattr(scheduler, "ssm", FakeSsm({"i-1": ["Success"], "i-2": ["Failed"]}))
        result = scheduler.run_security_update()
        assert result["statuses"] == {"i-1": "Success", "i-2": "Failed"}
