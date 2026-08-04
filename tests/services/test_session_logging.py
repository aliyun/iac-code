import logging

from iac_code.services.session_logging import (
    is_custom_endpoint,
    log_session_configured,
    log_session_start_safely,
    log_session_started,
    sanitize_endpoint_origin,
)


def test_session_started_log_quotes_values_with_spaces(caplog) -> None:
    caplog.set_level(logging.INFO, logger="iac_code.services.session_logging")

    log_session_started(
        session_id="s1",
        cwd="/tmp/project with spaces",
        provider="dashscope",
        provider_display="Alibaba Cloud Bailian",
        model="qwen3.6-plus",
    )

    message = caplog.records[-1].getMessage()
    assert "session_id=s1" in message
    assert 'cwd="/tmp/project with spaces"' in message
    assert "provider=dashscope" in message
    assert 'provider_display="Alibaba Cloud Bailian"' in message
    assert "model=qwen3.6-plus" in message


def test_log_session_start_safely_does_not_raise_from_callback(caplog) -> None:
    caplog.set_level(logging.INFO, logger="iac_code.services.session_logging")

    def fail() -> None:
        raise RuntimeError("logging-only failure")

    log_session_start_safely(fail)


def test_log_session_start_safely_skips_callback_when_info_disabled(monkeypatch) -> None:
    called = False

    def record_call() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        "iac_code.services.session_logging.logger.isEnabledFor",
        lambda _level: False,
    )

    log_session_start_safely(record_call)

    assert called is False


def test_session_configured_uses_distinct_message(caplog) -> None:
    caplog.set_level(logging.INFO, logger="iac_code.services.session_logging")

    log_session_configured(session_id="s1", source="web")

    assert "Session configured" in caplog.text
    assert "Session started" not in caplog.text
    assert "session_id=s1" in caplog.text
    assert "source=web" in caplog.text


def test_endpoint_origin_sanitizer_strips_sensitive_parts_and_preserves_ipv6() -> None:
    assert sanitize_endpoint_origin("http://user:pass@[::1]:11434/v1?api_key=secret") == "http://[::1]:11434"
    assert is_custom_endpoint("HTTPS://Example.com/v1/", "https://example.com/v1") is False
    assert is_custom_endpoint("https://example.com/v1/", "https://example.com/v1") is False
    assert is_custom_endpoint("https://example.com/v2", "https://example.com/v1") is True
    assert is_custom_endpoint("not-a-url", "https://example.com/v1") is False
