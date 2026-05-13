from __future__ import annotations

import acp
import pytest

from iac_code.acp.server import ACPServer
from iac_code.acp.session import ACPSession, _is_auth_error
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage


class FakeConn:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))


class FakeLoop:
    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="ok")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class ErrorFakeLoop:
    """A FakeLoop whose run_streaming raises RuntimeError."""

    async def run_streaming(self, prompt: str):
        raise RuntimeError("agent exploded")
        yield  # make it an async generator  # noqa: E501


class FakeRuntime:
    def __init__(self, loop=None) -> None:
        self.session_id = "err-s1"
        self.agent_loop = loop or FakeLoop()
        self.tool_registry = None


def _patch_server(monkeypatch: pytest.MonkeyPatch, loop=None) -> None:
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: FakeRuntime(loop=loop),
    )
    monkeypatch.setattr(
        "iac_code.acp.server.replace_bash_with_acp_terminal",
        lambda *args, **kwargs: None,
    )


# ---------------------------------------------------------------------------
# new_session when conn is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_conn_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0: new_session raises error when conn is None."""
    _patch_server(monkeypatch)
    server = ACPServer()
    # conn is None by default

    with pytest.raises(acp.RequestError):
        await server.new_session(cwd="/tmp")


# ---------------------------------------------------------------------------
# prompt / cancel with missing session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_session_not_found() -> None:
    """P0: prompt raises error when session does not exist."""
    server = ACPServer()
    server.on_connect(FakeConn())

    with pytest.raises(acp.RequestError):
        await server.prompt(
            session_id="nonexistent",
            prompt=[acp.schema.TextContentBlock(type="text", text="hi")],
        )


@pytest.mark.asyncio
async def test_cancel_session_not_found() -> None:
    """P0: cancel raises error when session does not exist."""
    server = ACPServer()
    server.on_connect(FakeConn())

    with pytest.raises(acp.RequestError):
        await server.cancel(session_id="nonexistent")


# ---------------------------------------------------------------------------
# agent loop exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_agent_loop_exception_becomes_request_error() -> None:
    """P0: agent_loop exception during execution."""
    conn = FakeConn()
    session = ACPSession("err-s1", ErrorFakeLoop(), conn)

    with pytest.raises(acp.RequestError):
        await session.prompt([acp.schema.TextContentBlock(type="text", text="boom")])


# ---------------------------------------------------------------------------
# auth error detection
# ---------------------------------------------------------------------------


def test_is_auth_error_value_error_provider() -> None:
    """ValueError mentioning 'provider' is detected as auth error."""
    exc = ValueError("Cannot determine provider for model: foo. Run /auth to configure.")
    assert _is_auth_error(exc) is True


def test_is_auth_error_value_error_configure() -> None:
    """ValueError mentioning 'configure' is detected as auth error."""
    exc = ValueError("Please configure your credentials")
    assert _is_auth_error(exc) is True


def test_is_auth_error_generic_value_error() -> None:
    """ValueError without auth keywords is NOT detected as auth error."""
    exc = ValueError("invalid literal for int()")
    assert _is_auth_error(exc) is False


def test_is_auth_error_authentication_error_class_name() -> None:
    """Exception with class name AuthenticationError is detected."""

    class AuthenticationError(Exception):
        pass

    assert _is_auth_error(AuthenticationError("bad key")) is True


def test_is_auth_error_401_status() -> None:
    """Exception with status_code=401 is detected as auth error."""

    class HttpError(Exception):
        def __init__(self, status_code: int):
            self.status_code = status_code

    assert _is_auth_error(HttpError(401)) is True
    assert _is_auth_error(HttpError(500)) is False


def test_is_auth_error_plain_runtime_error() -> None:
    """Plain RuntimeError is NOT an auth error."""
    assert _is_auth_error(RuntimeError("boom")) is False


# ---------------------------------------------------------------------------
# auth error in session prompt
# ---------------------------------------------------------------------------


class AuthErrorFakeLoop:
    """A FakeLoop whose run_streaming raises a provider configuration ValueError."""

    async def run_streaming(self, prompt: str):
        raise ValueError("Cannot determine provider for model: foo. Run /auth to configure.")
        yield  # make it an async generator  # noqa: E501


@pytest.mark.asyncio
async def test_prompt_auth_error_returns_auth_required() -> None:
    """P0: provider ValueError -> RequestError with code=auth_required."""
    conn = FakeConn()
    session = ACPSession("auth-s1", AuthErrorFakeLoop(), conn)

    with pytest.raises(acp.RequestError) as exc_info:
        await session.prompt([acp.schema.TextContentBlock(type="text", text="hi")])

    # Verify it's an internal error with auth_required code
    err = exc_info.value
    assert err.data["code"] == "auth_required"
    assert "Authentication required" in err.data["error"]


# ---------------------------------------------------------------------------
# unimplemented methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_session_returns_not_implemented() -> None:
    """P1: load_session is not implemented."""
    server = ACPServer()

    with pytest.raises(acp.RequestError):
        await server.load_session(cwd="/tmp", session_id="any-id")


@pytest.mark.asyncio
async def test_resume_session_not_connected() -> None:
    """P1: resume_session without connection raises error for unknown session."""
    server = ACPServer()

    with pytest.raises(acp.RequestError):
        await server.resume_session(cwd="/tmp", session_id="nonexistent-id")


# ---------------------------------------------------------------------------
# new_session auth_required when provider not configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_auth_required_when_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 7: Provider not configured → new_session returns auth_required error."""
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        _raise_provider_not_configured,
    )
    server = ACPServer()
    server.on_connect(FakeConn())

    with pytest.raises(acp.RequestError) as exc_info:
        await server.new_session(cwd="/tmp")

    err = exc_info.value
    assert err.data["code"] == "auth_required"
    assert "Authentication required" in err.data["error"]


def _raise_provider_not_configured(options):
    raise ValueError("Cannot determine provider for model: foo. Run /auth to configure.")


# ---------------------------------------------------------------------------
# new_session normal creation with valid credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_normal_creation_with_valid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 8: Valid credentials → new_session creates session normally."""
    _patch_server(monkeypatch)
    server = ACPServer()
    server.on_connect(FakeConn())

    response = await server.new_session(cwd="/tmp")

    assert response.session_id is not None
    assert response.session_id in server.sessions


# ---------------------------------------------------------------------------
# auth error does not expose sensitive information
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_error_does_not_expose_sensitive_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 9: Auth error response must not contain API keys or secrets."""
    fake_key = "sk-super-secret-key-12345"

    def _raise_with_key(options):
        raise ValueError(f"Cannot determine provider for model with key {fake_key}. Run /auth to configure.")

    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", _raise_with_key)
    server = ACPServer()
    server.on_connect(FakeConn())

    with pytest.raises(acp.RequestError) as exc_info:
        await server.new_session(cwd="/tmp")

    err = exc_info.value
    # The generic auth_required message should NOT leak the original exception text
    error_str = str(err.data)
    assert fake_key not in error_str
    assert err.data["code"] == "auth_required"


@pytest.mark.asyncio
async def test_session_prompt_auth_error_does_not_expose_sensitive_info() -> None:
    """Scenario 9b: Auth error in session.prompt also does not leak secrets."""
    fake_key = "sk-another-secret-99999"

    class LeakyAuthLoop:
        async def run_streaming(self, prompt: str):
            raise ValueError(f"Cannot determine provider with key {fake_key}. Run /auth to configure.")
            yield  # noqa: E501

    conn = FakeConn()
    session = ACPSession("sens-s1", LeakyAuthLoop(), conn)

    with pytest.raises(acp.RequestError) as exc_info:
        await session.prompt([acp.schema.TextContentBlock(type="text", text="hi")])

    error_str = str(exc_info.value.data)
    assert fake_key not in error_str
    assert exc_info.value.data["code"] == "auth_required"
