"""Tests for scheduler mode resolution and window-cohort selection.

The `scheduler` tag value drives whether an instance is started/stopped on a
schedule, kept always-on, or ignored. normalize_mode maps the many spellings a
human might type into the three behaviors, so its aliasing is worth pinning.
"""

import json

import pytest

import scheduler


class TestNormalizeMode:
    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_blank_falls_back_to_default(self, value):
        assert scheduler.normalize_mode(value) == scheduler.SCHEDULER_MODE_DEFAULT

    @pytest.mark.parametrize(
        "value",
        ["on-demand", "ondemand", "on_demand", "always-on", "ALWAYS ON", "keep-running", "On-Demand"],
    )
    def test_on_demand_aliases(self, value):
        assert scheduler.normalize_mode(value) == scheduler.SCHEDULER_MODE_ON_DEMAND

    @pytest.mark.parametrize("value", ["disabled", "off", "manual", "paused", "IGNORE", "none"])
    def test_disabled_aliases(self, value):
        assert scheduler.normalize_mode(value) == "disabled"

    def test_custom_cohort_passthrough_lowercased(self):
        # An unrecognized value is a custom cohort name, matched against window modes.
        assert scheduler.normalize_mode("Team-Alpha") == "team-alpha"

    def test_underscores_and_spaces_normalize_to_hyphens(self):
        assert scheduler.normalize_mode("free_time") == "free-time"
        assert scheduler.normalize_mode("free time") == "free-time"


class TestInstanceHelpers:
    def test_instance_tags_flattens_tag_list(self):
        instance = {"Tags": [{"Key": "scheduler", "Value": "free-time"}, {"Key": "Name", "Value": "box"}]}
        assert scheduler.instance_tags(instance) == {"scheduler": "free-time", "Name": "box"}

    def test_instance_mode_reads_scheduler_tag(self):
        instance = {"Tags": [{"Key": "scheduler", "Value": "on_demand"}]}
        assert scheduler.instance_mode(instance) == scheduler.SCHEDULER_MODE_ON_DEMAND

    def test_instance_mode_defaults_when_tag_absent(self):
        assert scheduler.instance_mode({"Tags": []}) == scheduler.SCHEDULER_MODE_DEFAULT


class TestEventSchedulerMode:
    def test_none_when_absent(self):
        assert scheduler.event_scheduler_mode({}) is None
        assert scheduler.event_scheduler_mode(None) is None

    def test_normalizes_present_value(self):
        assert scheduler.event_scheduler_mode({"scheduler_mode": "on_demand"}) == scheduler.SCHEDULER_MODE_ON_DEMAND


class TestActiveAllowedWindows:
    def _set_windows(self, monkeypatch, windows):
        monkeypatch.setattr(scheduler, "INSTANCE_ALLOWED_WINDOWS", json.dumps(windows))

    def test_filters_to_matching_mode(self, monkeypatch):
        self._set_windows(
            monkeypatch,
            [
                {"name": "a", "mode": "free-time", "days": ["MON"], "start_time": "09:00", "stop_time": "17:00"},
                {"name": "b", "mode": "team-alpha", "days": ["MON"], "start_time": "10:00", "stop_time": "12:00"},
            ],
        )
        result = scheduler.active_allowed_windows("team-alpha")
        assert [w["name"] for w in result] == ["b"]

    def test_window_without_mode_defaults_to_free_time(self, monkeypatch):
        self._set_windows(
            monkeypatch,
            [{"name": "a", "days": ["MON"], "start_time": "09:00", "stop_time": "17:00"}],
        )
        assert len(scheduler.active_allowed_windows("free-time")) == 1

    def test_no_matching_mode_raises(self, monkeypatch):
        self._set_windows(
            monkeypatch,
            [{"name": "a", "mode": "free-time", "days": ["MON"], "start_time": "09:00", "stop_time": "17:00"}],
        )
        with pytest.raises(ValueError, match="No instance_schedule_windows configured"):
            scheduler.active_allowed_windows("team-alpha")


class TestAllowedWindows:
    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setattr(scheduler, "INSTANCE_ALLOWED_WINDOWS", "{not json")
        with pytest.raises(ValueError, match="must be valid JSON"):
            scheduler.allowed_windows()

    def test_non_list_raises(self, monkeypatch):
        monkeypatch.setattr(scheduler, "INSTANCE_ALLOWED_WINDOWS", json.dumps({"a": 1}))
        with pytest.raises(ValueError, match="at least one window"):
            scheduler.allowed_windows()

    def test_empty_list_raises(self, monkeypatch):
        monkeypatch.setattr(scheduler, "INSTANCE_ALLOWED_WINDOWS", "[]")
        with pytest.raises(ValueError, match="at least one window"):
            scheduler.allowed_windows()
