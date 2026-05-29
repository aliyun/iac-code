"""Tests for telemetry privacy-level and content capture detection."""

import pytest

from iac_code.services.telemetry.config import (
    ContentCaptureMode,
    PrivacyLevel,
    get_content_capture_mode,
    get_privacy_level,
    is_essential_traffic_only,
    is_telemetry_disabled,
    should_capture_content_on_span,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("IAC_CODE_TELEMETRY_DEBUG", raising=False)
    # Reset log module state so is_debug_enabled() starts False
    from loguru import logger

    import iac_code.utils.log as log_mod

    logger.remove()
    log_mod._startup_handler_id = None
    log_mod._runtime_debug_handler_ids = []
    log_mod._debug_enabled = False
    log_mod._current_log_file = None


def test_default_level_when_no_env_vars_set():
    assert get_privacy_level() == PrivacyLevel.DEFAULT
    assert is_telemetry_disabled() is False
    assert is_essential_traffic_only() is False


def test_no_telemetry_level_when_flag_set(monkeypatch):
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    assert get_privacy_level() == PrivacyLevel.NO_TELEMETRY
    assert is_telemetry_disabled() is True
    assert is_essential_traffic_only() is False


def test_essential_traffic_level_when_flag_set(monkeypatch):
    monkeypatch.setenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    assert get_privacy_level() == PrivacyLevel.ESSENTIAL_TRAFFIC
    assert is_telemetry_disabled() is True
    assert is_essential_traffic_only() is True


def test_most_restrictive_wins_when_both_set(monkeypatch):
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    assert get_privacy_level() == PrivacyLevel.ESSENTIAL_TRAFFIC


@pytest.mark.parametrize("val", ["0", "", "false"])
def test_flag_set_to_falsy_string_treated_as_unset(monkeypatch, val):
    monkeypatch.setenv("DISABLE_TELEMETRY", val)
    assert is_telemetry_disabled() is False


# --- Local-build (empty __release_date__) gate ---


@pytest.mark.parametrize("blank", ["", "   "])
def test_local_build_disables_telemetry(monkeypatch, blank):
    """Empty __release_date__ marks an unpackaged local build; telemetry must be off."""
    monkeypatch.setattr("iac_code.__release_date__", blank)
    assert is_telemetry_disabled() is True
    # Privacy level itself reflects only env vars, not the build stamp.
    assert get_privacy_level() == PrivacyLevel.DEFAULT
    assert is_essential_traffic_only() is False


def test_released_build_with_no_env_vars_enables_telemetry(monkeypatch):
    monkeypatch.setattr("iac_code.__release_date__", "2026-01-01")
    assert is_telemetry_disabled() is False


# --- Content capture mode ---


def test_content_capture_default_is_no_content():
    assert get_content_capture_mode() == ContentCaptureMode.NO_CONTENT
    assert should_capture_content_on_span() is False


def test_content_capture_span_only(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY")
    assert get_content_capture_mode() == ContentCaptureMode.SPAN_ONLY
    assert should_capture_content_on_span() is True


def test_content_capture_span_and_event(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_AND_EVENT")
    assert get_content_capture_mode() == ContentCaptureMode.SPAN_AND_EVENT
    assert should_capture_content_on_span() is True


def test_content_capture_event_only_does_not_enable_span(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "EVENT_ONLY")
    assert get_content_capture_mode() == ContentCaptureMode.EVENT_ONLY
    assert should_capture_content_on_span() is False


def test_content_capture_case_insensitive(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "span_only")
    assert get_content_capture_mode() == ContentCaptureMode.SPAN_ONLY


# --- Debug mode drives sensitive-content capture via is_debug_enabled() ---


def test_debug_env_var_no_longer_enables_span_capture(monkeypatch):
    """Legacy DEBUG=1 env var must not enable sensitive capture anymore."""
    monkeypatch.setenv("DEBUG", "1")
    assert should_capture_content_on_span() is False


def test_telemetry_debug_env_var_no_longer_enables_span_capture(monkeypatch):
    """Legacy IAC_CODE_TELEMETRY_DEBUG env var must not enable sensitive capture anymore."""
    monkeypatch.setenv("IAC_CODE_TELEMETRY_DEBUG", "1")
    assert should_capture_content_on_span() is False


def test_runtime_debug_enables_span_capture(tmp_path, monkeypatch):
    """enable_debug_at_runtime flips sensitive capture on."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    from iac_code.utils.log import disable_debug_at_runtime, enable_debug_at_runtime, setup_logging

    setup_logging(session_id="tele1", debug=False)
    assert should_capture_content_on_span() is False

    enable_debug_at_runtime("tele1")
    assert should_capture_content_on_span() is True

    disable_debug_at_runtime()
    assert should_capture_content_on_span() is False


def test_startup_debug_flag_enables_span_capture(tmp_path, monkeypatch):
    """setup_logging(debug=True) enables sensitive capture via --debug."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    from iac_code.utils.log import setup_logging

    setup_logging(session_id="tele2", debug=True)
    assert should_capture_content_on_span() is True
