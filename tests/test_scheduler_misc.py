"""Tests for scheduler date/env helpers used by the backup-retention logic."""

from datetime import datetime, timezone

import pytest

import scheduler


class TestParseAwsDatetime:
    def test_parses_zulu_suffix(self):
        parsed = scheduler.parse_aws_datetime("2026-05-25T01:30:04.000Z")
        assert parsed == datetime(2026, 5, 25, 1, 30, 4, tzinfo=timezone.utc)
        assert parsed.tzinfo is not None  # must be tz-aware so retention math is correct

    def test_parses_explicit_offset(self):
        parsed = scheduler.parse_aws_datetime("2026-05-25T01:30:04+00:00")
        assert parsed.utcoffset().total_seconds() == 0


class TestPositiveIntEnv:
    def test_reads_valid_value(self, monkeypatch):
        monkeypatch.setenv("SOME_RETENTION", "14")
        assert scheduler.positive_int_env("SOME_RETENTION", 7) == 14

    def test_uses_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SOME_RETENTION", raising=False)
        assert scheduler.positive_int_env("SOME_RETENTION", 7) == 7

    @pytest.mark.parametrize("value", ["0", "-3"])
    def test_rejects_non_positive(self, monkeypatch, value):
        monkeypatch.setenv("SOME_RETENTION", value)
        with pytest.raises(ValueError, match="at least 1"):
            scheduler.positive_int_env("SOME_RETENTION", 7)

    def test_rejects_non_integer(self, monkeypatch):
        monkeypatch.setenv("SOME_RETENTION", "not-a-number")
        with pytest.raises(ValueError, match="must be a positive integer"):
            scheduler.positive_int_env("SOME_RETENTION", 7)
