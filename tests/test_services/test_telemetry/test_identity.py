"""Tests for the Identity class."""

import re

import pytest
import yaml

from iac_code.services.telemetry.identity import (
    SESSION_ID_PREFIX,
    USER_ID_PREFIX,
    Identity,
)


@pytest.fixture
def settings_path(tmp_path):
    return tmp_path / "settings.yml"


def test_user_id_has_iac_user_prefix_and_uuid4(settings_path):
    user_id = Identity(settings_path).get_user_id()
    assert user_id.startswith(USER_ID_PREFIX)
    uuid_part = user_id[len(USER_ID_PREFIX) :]
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", uuid_part)


def test_user_id_is_stable_within_instance(settings_path):
    identity = Identity(settings_path)
    assert identity.get_user_id() == identity.get_user_id()


def test_user_id_persists_across_instances(settings_path):
    first = Identity(settings_path).get_user_id()
    second = Identity(settings_path).get_user_id()
    assert first == second


def test_user_id_persists_to_settings_yml(settings_path):
    user_id = Identity(settings_path).get_user_id()
    assert settings_path.exists()
    data = yaml.safe_load(settings_path.read_text())
    assert data["userID"] == user_id


def test_user_id_regenerated_after_file_delete(settings_path):
    first = Identity(settings_path).get_user_id()
    settings_path.unlink()
    second = Identity(settings_path).get_user_id()
    assert first != second


def test_session_id_has_iac_sess_prefix(settings_path):
    assert Identity(settings_path).get_session_id().startswith(SESSION_ID_PREFIX)


def test_session_id_stable_within_instance(settings_path):
    identity = Identity(settings_path)
    assert identity.get_session_id() == identity.get_session_id()


def test_session_ids_differ_across_instances(settings_path):
    a = Identity(settings_path).get_session_id()
    b = Identity(settings_path).get_session_id()
    assert a != b


def test_tenant_id_returns_none_when_env_not_set(settings_path, monkeypatch):
    monkeypatch.delenv("IAC_CODE_TENANT_ID", raising=False)
    assert Identity(settings_path).get_tenant_id() is None


def test_tenant_id_adds_prefix_when_not_present(settings_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_TENANT_ID", "acme")
    assert Identity(settings_path).get_tenant_id() == "iac_tenant_acme"


def test_tenant_id_keeps_prefix_when_user_already_added_it(settings_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_TENANT_ID", "iac_tenant_acme")
    assert Identity(settings_path).get_tenant_id() == "iac_tenant_acme"


def test_tenant_id_strips_whitespace(settings_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_TENANT_ID", "  acme  ")
    assert Identity(settings_path).get_tenant_id() == "iac_tenant_acme"


def test_tenant_id_empty_string_is_none(settings_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_TENANT_ID", "   ")
    assert Identity(settings_path).get_tenant_id() is None


def test_was_first_run_true_when_newly_generated(settings_path):
    identity = Identity(settings_path)
    identity.get_user_id()
    assert identity.was_first_run() is True


def test_was_first_run_false_when_existing_id_loaded(settings_path):
    Identity(settings_path).get_user_id()  # create
    identity = Identity(settings_path)
    identity.get_user_id()  # load existing
    assert identity.was_first_run() is False


def test_session_id_uses_injected_uuid(settings_path):
    identity = Identity(settings_path, session_id="99646984-35a9-4850-b72a-4131a1690774")
    assert identity.get_session_id() == f"{SESSION_ID_PREFIX}99646984-35a9-4850-b72a-4131a1690774"


def test_session_id_stable_when_injected(settings_path):
    identity = Identity(settings_path, session_id="99646984-35a9-4850-b72a-4131a1690774")
    assert identity.get_session_id() == identity.get_session_id()


def test_session_id_generated_when_not_injected(settings_path):
    # no injection → still generates a fresh iac_sess_ id
    session_id = Identity(settings_path).get_session_id()
    assert session_id.startswith(SESSION_ID_PREFIX)
    assert len(session_id) > len(SESSION_ID_PREFIX)


def test_context_override_takes_precedence_over_process_session_id(settings_path):
    from iac_code.services.telemetry.identity import use_session_id

    identity = Identity(settings_path, session_id="process-level")
    with use_session_id("per-context-call"):
        assert identity.get_session_id() == f"{SESSION_ID_PREFIX}per-context-call"
    # After the context manager exits, the process-level id is back.
    assert identity.get_session_id() == f"{SESSION_ID_PREFIX}process-level"


def test_context_override_prefix_idempotent(settings_path):
    from iac_code.services.telemetry.identity import use_session_id

    identity = Identity(settings_path, session_id="process-level")
    already_prefixed = f"{SESSION_ID_PREFIX}explicit"
    with use_session_id(already_prefixed):
        # Caller may pass a value that already contains the prefix; we don't double it.
        assert identity.get_session_id() == already_prefixed


def test_user_id_override_takes_precedence(settings_path):
    from iac_code.services.telemetry.identity import use_user_id

    identity = Identity(settings_path)
    original = identity.get_user_id()
    with use_user_id("custom-user-abc"):
        assert identity.get_user_id() == "custom-user-abc"
    assert identity.get_user_id() == original


def test_user_id_override_no_prefix_added(settings_path):
    from iac_code.services.telemetry.identity import use_user_id

    identity = Identity(settings_path)
    with use_user_id("raw-value-123"):
        assert identity.get_user_id() == "raw-value-123"


def test_user_id_override_rejects_empty_string(settings_path):
    from iac_code.services.telemetry.identity import use_user_id

    with pytest.raises(ValueError, match="user_id must be a non-empty string"):
        with use_user_id(""):
            pass


def test_user_id_override_isolated_between_async_tasks(settings_path):
    import asyncio

    from iac_code.services.telemetry.identity import use_user_id

    identity = Identity(settings_path)

    async def under_override(uid: str, started: asyncio.Event, release: asyncio.Event) -> str:
        with use_user_id(uid):
            started.set()
            await release.wait()
            return identity.get_user_id()

    async def main() -> tuple[str, str, str]:
        started_a = asyncio.Event()
        started_b = asyncio.Event()
        release = asyncio.Event()
        task_a = asyncio.create_task(under_override("user-a", started_a, release))
        task_b = asyncio.create_task(under_override("user-b", started_b, release))
        await started_a.wait()
        await started_b.wait()
        outside = identity.get_user_id()
        release.set()
        return outside, await task_a, await task_b

    outside, a, b = asyncio.run(main())
    assert outside.startswith(USER_ID_PREFIX)
    assert a == "user-a"
    assert b == "user-b"


def test_context_override_isolated_between_async_tasks(settings_path):
    import asyncio

    from iac_code.services.telemetry.identity import use_session_id

    identity = Identity(settings_path, session_id="process-level")

    async def under_override(sid: str, started: asyncio.Event, release: asyncio.Event) -> str:
        with use_session_id(sid):
            started.set()
            await release.wait()
            return identity.get_session_id()

    async def main() -> tuple[str, str, str]:
        started_a = asyncio.Event()
        started_b = asyncio.Event()
        release = asyncio.Event()
        task_a = asyncio.create_task(under_override("ctx-a", started_a, release))
        task_b = asyncio.create_task(under_override("ctx-b", started_b, release))
        await started_a.wait()
        await started_b.wait()
        # Outside any override, the parent task still sees the process-level id.
        outside = identity.get_session_id()
        release.set()
        return outside, await task_a, await task_b

    outside, a, b = asyncio.run(main())
    assert outside == f"{SESSION_ID_PREFIX}process-level"
    assert a == f"{SESSION_ID_PREFIX}ctx-a"
    assert b == f"{SESSION_ID_PREFIX}ctx-b"
