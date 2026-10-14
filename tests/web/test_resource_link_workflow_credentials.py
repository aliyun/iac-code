"""Offline OPS credential migration: isolated fake configuration, no cloud or LLM."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from iac_code.services.providers.aliyun import AliyunCredentials
from scripts.web.e2e.resource_link_workflow.credentials import adopt_oauth, cli_snapshot, source_path, source_readiness
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, atomic_write, json_write, read_json
from scripts.web.e2e.resource_link_workflow.store import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store.initialize(tmp_path / "state")
    profile = {
        "name": "test-guima",
        "mode": "OAuth",
        "region_id": "cn-hangzhou",
        "oauth_site_type": "CN",
        "oauth_access_token": "fake-access",
        "oauth_refresh_token": "fake-refresh",
        "oauth_access_token_expire": int(time.time()) + 3600,
        "access_key_id": "fake-key",
        "access_key_secret": "fake-secret",
        "sts_token": "fake-sts",
        "sts_expiration": int(time.time()) + 3600,
    }
    cli = tmp_path / "cli.json"
    json_write(cli, {"current": "different", "profiles": [profile]}, private=True)
    aliases = []
    for slot in store.status()["slots"]:
        path = store.root / "slots" / str(slot["id"]) / "config/.cloud-credentials.yml"
        atomic_write(path, "aliyun: {mode: AK, access_key_id: fake-old}\n", private=True)
        aliases.append(path)
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ["aliyun", "sts", "GetCallerIdentity"]
        assert argv[argv.index("--profile") + 1] == "test-guima"
        assert argv[argv.index("--config-path") + 1] == str(cli)
        assert kwargs["timeout"] == 25 and kwargs["capture_output"] is True
        return subprocess.CompletedProcess(argv, 0, '{"AccountId":"123","RequestId":"fake-request"}\n', "")

    monkeypatch.setattr(subprocess, "run", invoke)
    return store, cli, aliases, profile, calls


def test_adoption_preserves_backups_and_single_source_without_dispatch(setup, monkeypatch):
    store, cli, aliases, _, calls = setup
    before = store.status()
    originals = [a.read_bytes() for a in aliases]
    result = adopt_oauth(store, cli, "123", "cn-hangzhou")
    canonical = source_path(store.root)
    assert result["shared_slots"] == 4 and result["new_pass"] == 0
    assert all(a.is_symlink() and a.resolve() == canonical for a in aliases)
    assert [a.read_bytes() for a in (store.root / result["backup"]).glob("slot-*.yml")] == originals
    assert read_json(store.root / result["evidence"])["identity"]["AccountId"] == "123"
    assert store.status() == before
    assert "fake-refresh" not in json.dumps(result)
    assert len(calls) == 1
    assert adopt_oauth(store, cli, "123", "cn-hangzhou")["already_adopted"] is True
    assert len(calls) == 1
    with pytest.raises(WorkflowError, match="identity cannot be changed"):
        adopt_oauth(store, cli, "456", "cn-hangzhou")
    # The product's atomic persistence must keep all four original aliases intact.
    credential = AliyunCredentials._load_from_iac_code_path(aliases[1])
    credential.oauth_refresh_token = "fake-rotated"
    AliyunCredentials.save(credential)
    assert all(a.is_symlink() and a.resolve() == canonical for a in aliases)
    monkeypatch.setattr(AliyunCredentials, "refresh_oauth_if_needed", lambda credential: credential)
    with cli_snapshot(store.root) as snapshot:
        data = read_json(snapshot)
        assert data["current"] == "test-guima"
        assert data["profiles"][0]["mode"] == "StsToken"
        assert data["profiles"][0]["sts_token"] == "fake-sts"
        assert not any(k.startswith("oauth_") for k in data["profiles"][0])
        if os.name != "nt":
            assert snapshot.stat().st_mode & 0o777 == 0o600
    assert not snapshot.exists()


@pytest.mark.parametrize("state", ["unpaused", "web", "lease", "partial", "alias", "cli_public"])
def test_adoption_refuses_unsafe_state_before_cloud_call(setup, state):
    store, cli, aliases, _, calls = setup
    if state == "unpaused":
        store.pause(False)
    elif state in {"web", "lease"}:
        with store.transaction() as connection:
            field = "web_pid" if state == "web" else "task_id"
            connection.execute(f"UPDATE slots SET {field}=? WHERE id=0", (999 if state == "web" else "R0009",))
    elif state == "partial":
        source_path(store.root).parent.mkdir(parents=True, mode=0o700)
        atomic_write(source_path(store.root), "fake-partial", private=True)
    elif state == "alias":
        aliases[0].unlink()
        aliases[0].symlink_to(cli)
    elif state == "cli_public":
        if os.name == "nt":
            pytest.skip("POSIX mode boundary")
        cli.chmod(0o644)
    with pytest.raises(WorkflowError):
        adopt_oauth(store, cli, "123", "cn-hangzhou")
    assert not calls


@pytest.mark.parametrize("invalid", ["account", "expired", "international", "missing_refresh", "duplicate"])
def test_failed_identity_or_incomplete_profile_never_changes_aliases(setup, invalid):
    store, cli, aliases, profile, calls = setup
    originals = [a.read_bytes() for a in aliases]
    account = "456" if invalid == "account" else "123"
    if invalid == "expired":
        profile["sts_expiration"] = 1
    elif invalid == "international":
        profile["oauth_site_type"] = "INTL"
    elif invalid == "missing_refresh":
        profile.pop("oauth_refresh_token")
    json_write(cli, {"profiles": [profile, profile] if invalid == "duplicate" else [profile]}, private=True)
    with pytest.raises(WorkflowError, match="no secrets exposed"):
        adopt_oauth(store, cli, account, "cn-hangzhou")
    assert len(calls) == 1
    assert [a.read_bytes() for a in aliases] == originals
    assert not source_path(store.root).exists()


def test_snapshot_cleanup_on_cli_failure_and_no_fallback(setup, monkeypatch):
    store, cli, _, _, _ = setup
    with cli_snapshot(store.root) as legacy:
        assert legacy is None
    adopt_oauth(store, cli, "123", "cn-hangzhou")
    monkeypatch.setattr(AliyunCredentials, "refresh_oauth_if_needed", lambda credential: credential)
    with pytest.raises(RuntimeError, match="fake CLI failure"):
        with cli_snapshot(store.root) as snapshot:
            raise RuntimeError("fake CLI failure")
    assert not snapshot.exists()

    def failed_refresh(_):
        raise RuntimeError("fake-secret fake-refresh")

    monkeypatch.setattr(AliyunCredentials, "refresh_oauth_if_needed", failed_refresh)
    with pytest.raises(WorkflowError) as failure:
        with cli_snapshot(store.root):
            pytest.fail("must not yield stale CLI config")
    assert "fake-secret" not in str(failure.value) and "fake-refresh" not in str(failure.value)


def test_source_readiness_is_current_secret_free_and_never_refreshes(setup, monkeypatch):
    store, cli, _, _, calls = setup
    assert source_readiness(store.root)["status"] == "not_adopted"
    adopt_oauth(store, cli, "123", "cn-hangzhou")

    def forbidden_refresh(*_):
        pytest.fail("readiness must not refresh or make a cloud call")

    monkeypatch.setattr(AliyunCredentials, "refresh_oauth_if_needed", forbidden_refresh)
    ready = source_readiness(store.root)
    assert ready["status"] == "signer_current"
    assert ready["approved_account"] == "123" and ready["region"] == "cn-hangzhou"
    assert ready["signed_identity_verified"] is False and ready["refresh_performed"] is False
    assert "fake-" not in json.dumps(ready)
    credential = AliyunCredentials._load_from_iac_code_path(source_path(store.root))
    credential.sts_expiration = 1
    AliyunCredentials.save(credential)
    expired = source_readiness(store.root)
    assert expired["status"] == "refresh_required" and expired["sts_remaining_seconds"] == 0
    assert len(calls) == 1


def test_source_readiness_redacts_partial_source(tmp_path):
    root = tmp_path / "state"
    atomic_write(source_path(root), "fake-secret: [broken", private=True)
    result = source_readiness(root)
    assert result["status"] == "source_unavailable"
    assert "fake-secret" not in json.dumps(result)


@pytest.mark.parametrize("invalid", ["missing_marker", "missing_source", "public", "source_symlink", "region"])
def test_snapshot_refuses_partial_or_rebound_source(setup, monkeypatch, invalid):
    store, cli, _, _, _ = setup
    adopt_oauth(store, cli, "123", "cn-hangzhou")
    canonical = source_path(store.root)
    marker = canonical.parent / "source.json"
    if invalid == "missing_marker":
        marker.unlink()
    elif invalid == "missing_source":
        canonical.unlink()
    elif invalid == "public":
        if os.name == "nt":
            pytest.skip("POSIX mode boundary")
        canonical.chmod(0o644)
    elif invalid == "source_symlink":
        canonical.unlink()
        canonical.symlink_to(cli)
    elif invalid == "region":
        value = read_json(marker)
        value["region"] = "cn-shanghai"
        json_write(marker, value, private=True)
    monkeypatch.setattr(AliyunCredentials, "refresh_oauth_if_needed", lambda credential: credential)
    with pytest.raises(WorkflowError):
        with cli_snapshot(store.root):
            pytest.fail("must fail closed")
