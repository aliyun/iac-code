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
