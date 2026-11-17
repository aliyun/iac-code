"""The account picker uses local non-secret context, never cloud discovery."""

import copy
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.web.app import create_app
from iac_code.web.resource_accounts import load_configured_resource_context
from iac_code.web.session_manager import WebSessionManager


@pytest.mark.parametrize("site", ["", "CN", "INTL"])
def test_context_projection_never_refreshes_or_exposes_credential_fields(monkeypatch, site):
    credential = AliyunCredential(
        mode="OAuth",
        access_key_id="fake-access-key",
        access_key_secret="fake-secret",
        oauth_refresh_token="fake-refresh-token",
        oauth_site_type=site,
        region_id="fake-default-region",
        credential_source="iac_code",
        credential_source_path="/fake/private/config.json",
        oauth_session_id="fake-login-generation",
    )
    monkeypatch.setattr("iac_code.services.cloud_credentials.CloudCredentials.get_provider", lambda *args: credential)
    refresh = Mock(side_effect=AssertionError("context selection cannot refresh OAuth or query an account"))
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials.refresh_oauth_if_needed", refresh)
    context = load_configured_resource_context()
    assert context["account_scope"].startswith("credential:")
    assert set(context) == ({"account_scope", "console_partition"} if site else {"account_scope"})
    assert context.get("console_partition") == {"": None, "CN": "aliyun", "INTL": "alibabacloud"}[site]
    assert "fake-" not in str(context) and "private" not in str(context)
    assert load_configured_resource_context() == context
    credential.access_key_id = "fake-rotated-sts-ak"
    credential.access_key_secret = "fake-rotated-sts-secret"
    credential.sts_token = "fake-rotated-sts-token"
    credential.oauth_refresh_token = "fake-rotated-refresh-token"
    assert load_configured_resource_context()["account_scope"] == context["account_scope"]
    refresh.assert_not_called()


@pytest.mark.parametrize("source", ["iac_code", "aliyun_cli"])
def test_unmanaged_oauth_context_does_not_identify_an_account_by_path(monkeypatch, source):
    credential = AliyunCredential(
        mode="OAuth", oauth_site_type="CN", oauth_refresh_token="fake-account-a",
        credential_source=source, credential_source_path="/fake/private/config.json",
    )
    monkeypatch.setattr("iac_code.services.cloud_credentials.CloudCredentials.get_provider", lambda *args: credential)
    refresh = Mock(side_effect=AssertionError("local context must not refresh or query the cloud"))
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials.refresh_oauth_if_needed", refresh)
    first = load_configured_resource_context()
    assert load_configured_resource_context() == first
    credential.oauth_refresh_token = "fake-account-b"
    second = load_configured_resource_context()
    assert second["account_scope"] != first["account_scope"]
    assert set(second) == {"account_scope", "console_partition"}
    assert "fake-" not in str(second) and "private" not in str(second)
    refresh.assert_not_called()


@pytest.mark.parametrize("provider", [None, ValueError("fake-secret must not reach the client")])
def test_absent_or_broken_configuration_is_unavailable(monkeypatch, provider):
    monkeypatch.setattr(
        "iac_code.services.cloud_credentials.CloudCredentials.get_provider",
        Mock(side_effect=provider) if isinstance(provider, Exception) else Mock(return_value=provider),
    )
    assert load_configured_resource_context() == {}


def test_http_context_offer_is_explicit_isolated_and_not_a_resource_fact(tmp_path, monkeypatch):
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="empty-resource-session")
    other = manager.create_session(session_id="other-resource-session")
    monkeypatch.setattr(manager, "load_visible_transcript", lambda *args, **kwargs: {"messages": [], "tools": {}})
    loader = Mock(return_value={"account_scope": "credential:fake", "console_partition": "aliyun"})
    monkeypatch.setattr("iac_code.web.resource_accounts.load_configured_resource_context", loader)
    with TestClient(create_app(session_manager=manager)) as client:
        base = f"/api/sessions/{session.web_session_id}/resource-references"
        assert client.get(base).json()["items"] == []
        assert client.get(f"/api/sessions/{session.web_session_id}/messages").status_code == 200
        loader.assert_not_called()
        assert client.get(base + "/account-context").status_code == 405
        assert client.post(base + "/account-context", json={"profile": "not-allowed"}).status_code == 400
        assert client.post("/api/sessions/missing/resource-references/account-context").status_code == 404
        loader.assert_not_called()
        first = client.post(base + "/account-context")
        assert first.status_code == 200
        payload = first.json()
        assert payload["status"] == "available"
        assert set(payload["choice"]) == {"accountContextId", "accountScope", "consolePartition"}
        assert client.get(base).json()["items"] == [] and not session.resource_references.path.exists()
        second = client.post(f"/api/sessions/{other.web_session_id}/resource-references/account-context").json()
        assert second["choice"]["accountScope"] == payload["choice"]["accountScope"]
        assert second["choice"]["accountContextId"] != payload["choice"]["accountContextId"]
        assert loader.call_count == 2


def test_read_only_session_never_loads_configured_context(tmp_path, monkeypatch):
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="readonly-resource-session")
    monkeypatch.setattr(manager, "is_session_read_only", lambda *args: True)
    loader = Mock(side_effect=AssertionError("foreign sessions cannot acquire current credential context"))
    monkeypatch.setattr("iac_code.web.resource_accounts.load_configured_resource_context", loader)
    with TestClient(create_app(session_manager=manager)) as client:
        result = client.post(f"/api/sessions/{session.web_session_id}/resource-references/account-context")
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "foreign_read_only"
    loader.assert_not_called()


def test_context_response_cannot_publish_loader_extras(tmp_path, monkeypatch):
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="projected-resource-session")
    values = {"account_scope": "credential:fake", "secret": "fake-secret", "region_id": "cn-hangzhou"}
    monkeypatch.setattr(
        "iac_code.web.resource_accounts.load_configured_resource_context", lambda: copy.deepcopy(values)
    )
    with TestClient(create_app(session_manager=manager)) as client:
        result = client.post(f"/api/sessions/{session.web_session_id}/resource-references/account-context").json()
        assert set(result["choice"]) == {"accountContextId", "accountScope"}
        values.clear()
        assert client.post(f"/api/sessions/{session.web_session_id}/resource-references/account-context").json() == {
            "status": "unavailable"
        }
        assert session.resource_references._account_context is None
