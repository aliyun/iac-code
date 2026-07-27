import sys
from types import SimpleNamespace

import pytest

from iac_code.web.security import ensure_loopback_host, redact_secrets


def test_loopback_hosts_are_allowed() -> None:
    assert ensure_loopback_host("127.0.0.1") == "127.0.0.1"
    assert ensure_loopback_host("127.0.0.2") == "127.0.0.2"
    assert ensure_loopback_host("::1") == "::1"
    assert ensure_loopback_host("[::1]") == "::1"
    assert ensure_loopback_host("localhost") == "localhost"


def test_non_loopback_host_is_rejected() -> None:
    with pytest.raises(ValueError, match="local Web server only supports loopback hosts"):
        ensure_loopback_host("0.0.0.0")


def test_redact_secrets_masks_nested_values() -> None:
    payload = {
        "api_key": "sk-real",
        "apiKey": "alternate",
        "cloud": {"access_key_secret": "secret", "region": "cn-hangzhou"},
        "items": [{"sts_token": "token"}, {"authorization": "Bearer real"}],
        "cookie": ("a=b",),
        "private_key": "-----BEGIN PRIVATE KEY-----",
        "credential_uri": "file:///secret.json",
    }

    assert redact_secrets(payload) == {
        "api_key": "[REDACTED]",
        "apiKey": "[REDACTED]",
        "cloud": {"access_key_secret": "[REDACTED]", "region": "cn-hangzhou"},
        "items": [{"sts_token": "[REDACTED]"}, {"authorization": "[REDACTED]"}],
        "cookie": "[REDACTED]",
        "private_key": "[REDACTED]",
        "credential_uri": "[REDACTED]",
    }


def test_redact_secrets_masks_hyphenated_header_and_camel_private_key_values() -> None:
    payload = {
        "headers": {
            "X-Api-Key": "plain-secret-value",
            "Private-Key": "plain-private-value",
            "Authorization": "Bearer real",
        },
        "privateKey": "camel-private-value",
        "safe": {"display_name": "visible"},
    }

    assert redact_secrets(payload) == {
        "headers": {
            "X-Api-Key": "[REDACTED]",
            "Private-Key": "[REDACTED]",
            "Authorization": "[REDACTED]",
        },
        "privateKey": "[REDACTED]",
        "safe": {"display_name": "visible"},
    }


def test_run_web_server_rejects_non_loopback_host_before_starting() -> None:
    import iac_code.web.server as server

    with pytest.raises(ValueError, match="local Web server only supports loopback hosts"):
        server.run_web_server(host="0.0.0.0", port=8766)


def test_run_web_server_uses_bracketed_ipv6_browser_url(monkeypatch) -> None:
    import iac_code.web.server as server

    scheduled: list[tuple[str, str, int]] = []
    uvicorn_calls: list[dict] = []

    def fake_uvicorn_run(app, *, host: str, port: int) -> None:
        uvicorn_calls.append({"app": app, "host": host, "port": port})

    monkeypatch.setattr(
        server,
        "_schedule_browser_open",
        lambda url, host, port: scheduled.append((url, host, port)),
    )
    monkeypatch.setattr("iac_code.web.server.get_ui_language", lambda: None)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=fake_uvicorn_run))

    import iac_code.i18n as i18n

    _prev_lang = i18n._current_language
    try:
        server.run_web_server(host="::1", port=8766, open_browser=True)

        assert scheduled == [("http://[::1]:8766", "::1", 8766)]
        assert len(uvicorn_calls) == 1
        assert uvicorn_calls[0]["host"] == "::1"
        assert uvicorn_calls[0]["port"] == 8766
    finally:
        i18n.set_language(_prev_lang)


def test_browser_open_waits_until_server_accepts_connections(monkeypatch) -> None:
    import iac_code.web.server as server

    attempts: list[tuple[str, int]] = []
    opened: list[str] = []

    class ConnectedSocket:
        def close(self) -> None:
            return None

    def connect(address, *, timeout: float):
        attempts.append(address)
        if len(attempts) == 1:
            raise ConnectionRefusedError
        assert timeout > 0
        return ConnectedSocket()

    monkeypatch.setattr(server.socket, "create_connection", connect)
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)

    server._open_browser_when_ready(
        "http://127.0.0.1:8766",
        "127.0.0.1",
        8766,
        timeout_seconds=1,
        opener=opened.append,
    )

    assert attempts == [("127.0.0.1", 8766), ("127.0.0.1", 8766)]
    assert opened == ["http://127.0.0.1:8766"]


def test_run_web_server_accepts_loopback_ip_alias(monkeypatch) -> None:
    import iac_code.web.server as server

    uvicorn_calls: list[dict] = []

    def fake_uvicorn_run(app, *, host: str, port: int) -> None:
        uvicorn_calls.append({"app": app, "host": host, "port": port})

    monkeypatch.setattr("iac_code.web.server.get_ui_language", lambda: None)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=fake_uvicorn_run))

    import iac_code.i18n as i18n

    _prev_lang = i18n._current_language
    try:
        server.run_web_server(host="127.0.0.2", port=8767)

        assert len(uvicorn_calls) == 1
        assert uvicorn_calls[0]["host"] == "127.0.0.2"
        assert uvicorn_calls[0]["port"] == 8767
    finally:
        i18n.set_language(_prev_lang)
