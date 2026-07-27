"""The JSON log formatter.

Most of the useful detail is passed as extra={...}, which a plain formatter drops.
"""

import json
import logging

import pytest

import ami_transfer
import scheduler


@pytest.fixture(params=[scheduler, ami_transfer], ids=["scheduler", "ami_transfer"])
def formatter(request):
    # Both handlers use the same formatter, so check both.
    return request.param.JsonFormatter()


def record(message="msg", level=logging.INFO, **extra):
    rec = logging.LogRecord("test", level, "test.py", 1, message, (), None)
    rec.__dict__.update(extra)
    return rec


class TestJsonFormatter:
    def test_emits_a_single_json_line(self, formatter):
        # Logs Insights reads one JSON object per line.
        output = formatter.format(record("hello"))

        assert "\n" not in output
        assert json.loads(output)["message"] == "hello"

    def test_includes_level_and_timestamp(self, formatter):
        payload = json.loads(formatter.format(record("hello", level=logging.WARNING)))

        assert payload["level"] == "WARNING"
        assert payload["timestamp"]

    def test_extra_fields_are_preserved(self, formatter):
        payload = json.loads(
            formatter.format(record("Evaluated instance stop", instance_id="i-1", result="stop_requested"))
        )

        assert payload["instance_id"] == "i-1"
        assert payload["result"] == "stop_requested"

    def test_reserved_record_attributes_are_not_leaked(self, formatter):
        # Without the filter, LogRecord internals like pathname and lineno would
        # bury the fields that matter.
        payload = json.loads(formatter.format(record("hello")))

        assert "pathname" not in payload
        assert "lineno" not in payload
        assert set(payload) == {"timestamp", "level", "message"}

    def test_message_interpolation_is_applied(self, formatter):
        rec = logging.LogRecord("test", logging.INFO, "test.py", 1, "count=%s", ("3",), None)

        assert json.loads(formatter.format(rec))["message"] == "count=3"

    def test_non_serializable_values_do_not_break_the_line(self, formatter):
        # datetimes and boto objects land here often, and a TypeError in the
        # formatter would lose the whole entry.
        payload = json.loads(formatter.format(record("hello", when=object())))

        assert isinstance(payload["when"], str)

    def test_exception_info_is_captured(self, formatter):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = logging.LogRecord("test", logging.ERROR, "test.py", 1, "failed", (), sys.exc_info())

        payload = json.loads(formatter.format(rec))

        assert "ValueError: boom" in payload["exc_info"]


class TestHandlerConfiguration:
    @pytest.mark.parametrize("module", [scheduler, ami_transfer], ids=["scheduler", "ami_transfer"])
    def test_logger_is_configured_at_info(self, module):
        assert module.logger.level == logging.INFO

    @pytest.mark.parametrize("module", [scheduler, ami_transfer], ids=["scheduler", "ami_transfer"])
    def test_logger_uses_the_json_formatter(self, module):
        assert module.logger.handlers
        assert isinstance(module.logger.handlers[0].formatter, module.JsonFormatter)
