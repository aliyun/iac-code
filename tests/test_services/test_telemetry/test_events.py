"""Tests for EventEmitter."""

from unittest.mock import MagicMock

import pytest

from iac_code.services.telemetry.attributes import AttributeBuilder
from iac_code.services.telemetry.events import EventEmitter
from iac_code.services.telemetry.identity import Identity


@pytest.fixture
def emitter(tmp_path):
    identity = Identity(tmp_path / "settings.yml")
    attrs = AttributeBuilder(identity, "iac-code", "0.1.0")
    return EventEmitter(attrs)


def test_emit_calls_attached_logger(emitter):
    logger = MagicMock()
    emitter.attach(logger)
    emitter.emit("iac.test.happened", {"k": 1})
    assert logger.emit.call_count == 1


def test_emit_body_is_event_name(emitter):
    logger = MagicMock()
    emitter.attach(logger)
    emitter.emit("iac.test.happened", {})
    record = logger.emit.call_args.args[0]
    assert record.body == "iac.test.happened"


def test_emit_merges_resource_event_caller_metadata(emitter):
    logger = MagicMock()
    emitter.attach(logger)
    emitter.emit("iac.test.happened", {"k": 1})
    record = logger.emit.call_args.args[0]
    assert record.attributes["event.name"] == "iac.test.happened"
    assert record.attributes["service.name"] == "iac-code"
    assert record.attributes["k"] == 1


def test_emit_without_logger_is_noop(emitter):
    # Not attached — should not raise.
    emitter.emit("iac.test.happened", {"k": 1})


def test_emit_drops_none_values(emitter):
    logger = MagicMock()
    emitter.attach(logger)
    emitter.emit("iac.test", {"keep": 1, "drop": None})
    record = logger.emit.call_args.args[0]
    assert "keep" in record.attributes
    assert "drop" not in record.attributes
