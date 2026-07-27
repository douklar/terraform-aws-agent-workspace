"""Window evaluation in scheduler.py.

If window_allows_time gets it wrong, reconcile either stops an in-use instance
or never stops one that should be off. Cross-midnight and all-day windows are
the cases the start/stop crons cannot handle, so they get their own tests.
"""

from datetime import datetime, timezone

import pytest

import scheduler


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# 2024-01-01 is a Monday; the dates below are chosen so weekday() is obvious.
MON = utc(2024, 1, 1, 0)
FRI = utc(2024, 1, 5, 0)
SAT = utc(2024, 1, 6, 0)


class TestNormalWindow:
    """start_time < stop_time - a same-day window."""

    window = {"days": ["MON"], "timezone": "UTC", "start_time": "09:00", "stop_time": "17:00"}

    @pytest.mark.parametrize(
        "now, expected",
        [
            (utc(2024, 1, 1, 9, 0), True),    # exactly at start (inclusive)
            (utc(2024, 1, 1, 12, 0), True),   # mid-window
            (utc(2024, 1, 1, 16, 59), True),  # just before stop
            (utc(2024, 1, 1, 17, 0), False),  # exactly at stop (exclusive)
            (utc(2024, 1, 1, 8, 59), False),  # just before start
            (utc(2024, 1, 2, 12, 0), False),  # right time, wrong day (Tue)
        ],
    )
    def test_boundaries(self, now, expected):
        assert scheduler.window_allows_time(now, self.window) is expected


class TestCrossMidnightWindow:
    """start_time > stop_time - the window spans midnight into the next day."""

    window = {"days": ["FRI"], "timezone": "UTC", "start_time": "22:00", "stop_time": "02:00"}

    @pytest.mark.parametrize(
        "now, expected",
        [
            (utc(2024, 1, 5, 22, 0), True),   # Fri at start
            (utc(2024, 1, 5, 23, 30), True),  # Fri late evening
            (utc(2024, 1, 6, 1, 0), True),    # Sat 01:00 - tail of Fri's window
            (utc(2024, 1, 6, 2, 0), False),   # Sat 02:00 - window closed (exclusive)
            (utc(2024, 1, 5, 21, 0), False),  # Fri before start
            (utc(2024, 1, 6, 12, 0), False),  # Sat midday - unrelated
            (utc(2024, 1, 5, 1, 0), False),   # Fri 01:00 - Thu is not in days
        ],
    )
    def test_boundaries(self, now, expected):
        assert scheduler.window_allows_time(now, self.window) is expected


class TestAllDayWindow:
    """start_time == stop_time - active for the whole matching day."""

    window = {"days": ["SAT", "SUN"], "timezone": "UTC", "start_time": "00:00", "stop_time": "00:00"}

    @pytest.mark.parametrize(
        "now, expected",
        [
            (utc(2024, 1, 6, 0, 0), True),    # Sat midnight
            (utc(2024, 1, 6, 13, 37), True),  # Sat afternoon
            (utc(2024, 1, 7, 23, 59), True),  # Sun almost-midnight
            (utc(2024, 1, 1, 12, 0), False),  # Mon - not in days
        ],
    )
    def test_all_day(self, now, expected):
        assert scheduler.window_allows_time(now, self.window) is expected


class TestTimezoneHandling:
    """The window's timezone, not UTC, decides the local hour and weekday."""

    def test_local_hour_is_used(self):
        # Berlin is UTC+1 in January. 08:30 UTC == 09:30 Berlin -> inside 09:00-17:00.
        window = {"days": ["MON"], "timezone": "Europe/Berlin", "start_time": "09:00", "stop_time": "17:00"}
        assert scheduler.window_allows_time(utc(2024, 1, 1, 8, 30), window) is True
        # 07:30 UTC == 08:30 Berlin -> before the window opens.
        assert scheduler.window_allows_time(utc(2024, 1, 1, 7, 30), window) is False

    def test_timezone_can_roll_the_weekday_forward(self):
        # Mon 23:45 UTC == Tue 00:45 Berlin, so a Tue window matches.
        window = {"days": ["TUE"], "timezone": "Europe/Berlin", "start_time": "00:30", "stop_time": "01:00"}
        assert scheduler.window_allows_time(utc(2024, 1, 1, 23, 45), window) is True

    def test_missing_timezone_falls_back_to_module_default(self, monkeypatch):
        monkeypatch.setattr(scheduler, "SCHEDULE_TIMEZONE", "UTC")
        window = {"days": ["MON"], "start_time": "09:00", "stop_time": "17:00"}
        assert scheduler.window_allows_time(utc(2024, 1, 1, 12, 0), window) is True


class TestWindowValidation:
    def test_missing_days_raises(self):
        window = {"timezone": "UTC", "start_time": "09:00", "stop_time": "17:00"}
        with pytest.raises(ValueError, match="missing days"):
            scheduler.window_allows_time(MON, window)

    def test_missing_start_time_raises(self):
        window = {"days": ["MON"], "timezone": "UTC", "stop_time": "17:00"}
        with pytest.raises(ValueError, match="missing start_time"):
            scheduler.window_allows_time(MON, window)

    def test_days_are_matched_case_insensitively(self):
        window = {"days": ["mon"], "timezone": "UTC", "start_time": "09:00", "stop_time": "17:00"}
        assert scheduler.window_allows_time(utc(2024, 1, 1, 12, 0), window) is True


class TestIsAllowedTime:
    """is_allowed_time is True if *any* window matches."""

    def test_matches_second_window(self):
        windows = [
            {"days": ["MON"], "timezone": "UTC", "start_time": "09:00", "stop_time": "10:00"},
            {"days": ["MON"], "timezone": "UTC", "start_time": "14:00", "stop_time": "15:00"},
        ]
        assert scheduler.is_allowed_time(utc(2024, 1, 1, 14, 30), windows) is True

    def test_matches_no_window(self):
        windows = [
            {"days": ["MON"], "timezone": "UTC", "start_time": "09:00", "stop_time": "10:00"},
        ]
        assert scheduler.is_allowed_time(utc(2024, 1, 1, 11, 0), windows) is False


class TestParseHhmm:
    @pytest.mark.parametrize(
        "value, minutes",
        [("00:00", 0), ("09:30", 570), ("23:59", 1439), ("9:05", 545)],
    )
    def test_valid(self, value, minutes):
        assert scheduler.parse_hhmm(value) == minutes

    @pytest.mark.parametrize("value", ["24:00", "12:60", "-1:00", "9", "aa:bb", "", "1230"])
    def test_invalid_raises(self, value):
        with pytest.raises(ValueError, match="expected HH:MM"):
            scheduler.parse_hhmm(value)

    def test_non_string_raises(self):
        with pytest.raises(ValueError, match="expected HH:MM"):
            scheduler.parse_hhmm(930)


class TestWindowTimeMinutes:
    def test_missing_key_raises(self):
        with pytest.raises(ValueError, match="missing stop_time"):
            scheduler.window_time_minutes({"start_time": "09:00"}, "stop_time")

    def test_present_key_parses(self):
        assert scheduler.window_time_minutes({"start_time": "08:15"}, "start_time") == 495
