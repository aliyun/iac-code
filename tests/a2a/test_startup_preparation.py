"""Public startup resources and durable snapshot write reduction."""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest

from iac_code.a2a import warmup
from iac_code.a2a.transports.dispatcher import A2ARuntimeComponents
from iac_code.utils import bundled_data, state_io


@pytest.mark.asyncio
@pytest.mark.parametrize("value,enabled", [(None, True), ("1", True), ("0", False), ("false", False), ("OFF", False)])
async def test_startup_warms_before_background_admission(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("IAC_CODE_A2A_STARTUP_WARMUP", raising=False)
    else:
        monkeypatch.setenv("IAC_CODE_A2A_STARTUP_WARMUP", value)
    order = []
    monkeypatch.setattr(warmup, "warmup_runtime", lambda: order.append("warm"))

    async def recover():
        order.append("recover")

    components = A2ARuntimeComponents(
        handler=None,
        task_store=None,
        card=None,
        app=None,
        _exit_stack=AsyncExitStack(),
        backup_staging_process=SimpleNamespace(start=lambda: order.append("start")),
        backup_coordinator=SimpleNamespace(recover=recover),
    )
    await components.start_background_services()
    assert order == (["warm"] if enabled else []) + ["start", "recover"]


def test_public_warmup_does_not_construct_session_or_credential_bound_services(monkeypatch):
    from iac_code.providers import manager
    from iac_code.services import agent_factory, cloud_credentials, session_storage

    def forbidden(*args, **kwargs):
        pytest.fail("warmup must not create session state, clients or load credentials")

    monkeypatch.setattr(agent_factory, "create_agent_runtime", forbidden)
    monkeypatch.setattr(manager.ProviderManager, "__init__", forbidden)
    monkeypatch.setattr(cloud_credentials.CloudCredentials, "__init__", forbidden)
    monkeypatch.setattr(session_storage.SessionStorage, "__init__", forbidden)
    warmup.warmup_runtime()


@pytest.mark.parametrize("format,text", [("json", '{"values":[1]}'), ("yaml", "values: [1]\n")])
def test_bundled_parse_cache_copies_and_invalidates_replacements(tmp_path, monkeypatch, format, text):
    path = tmp_path / "public-data"
    path.write_text(text, encoding="utf-8")
    original = bundled_data.json.loads if format == "json" else bundled_data.yaml.safe_load
    calls = []

    def parse(value):
        calls.append(value)
        return original(value)

    module = bundled_data.json if format == "json" else bundled_data.yaml
    monkeypatch.setattr(module, "loads" if format == "json" else "safe_load", parse)
    first = bundled_data.load_bundled_data(path, format=format)
    first["values"].append(2)
    assert bundled_data.load_bundled_data(path, format=format) == {"values": [1]}
    assert len(calls) == 1
    replacement = tmp_path / "replacement"
    replacement.write_text(text.replace("1", "3"), encoding="utf-8")
    replacement.replace(path)
    assert bundled_data.load_bundled_data(path, format=format) == {"values": [3]}
    assert len(calls) == 2
    path.write_text("[", encoding="utf-8")
    with pytest.raises((ValueError, bundled_data.yaml.YAMLError)):
        bundled_data.load_bundled_data(path, format=format)


def test_snapshot_skips_only_known_durable_unchanged_version(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    payload = json.dumps({"state": "working"})
    syncs = []
    monkeypatch.setattr(state_io.os, "fsync", lambda fd: syncs.append(fd))
    state_io.write_snapshot_text(path, payload)
    initial = len(syncs)
    assert initial > 0
    state_io.write_snapshot_text(path, payload)
    assert len(syncs) == initial
    # Another writer's version, even with the same contents, needs a new durability proof.
    replacement = tmp_path / "other-version"
    replacement.write_text(payload, encoding="utf-8")
    replacement.replace(path)
    state_io.write_snapshot_text(path, payload)
    assert len(syncs) == initial * 2
    path.write_text("external change", encoding="utf-8")
    state_io.write_snapshot_text(path, payload)
    assert path.read_text(encoding="utf-8") == payload
    assert len(syncs) == initial * 3
    path.unlink()
    state_io.write_snapshot_text(path, payload)
    assert len(syncs) == initial * 4
    with state_io._SNAPSHOT_GUARD:
        state_io._SNAPSHOT_WRITES.clear()
    state_io.write_snapshot_text(path, payload)
    assert len(syncs) == initial * 5


def test_snapshot_durability_failure_is_not_cached(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"

    def fail(fd):
        raise OSError("fixture fsync failed")

    monkeypatch.setattr(state_io.os, "fsync", fail)
    for _ in range(2):
        with pytest.raises(OSError, match="fixture fsync failed"):
            state_io.write_snapshot_text(path, "new state")
    assert not path.exists()


@pytest.mark.parametrize("replace", [False, True])
def test_snapshot_does_not_cache_another_writers_version(tmp_path, monkeypatch, replace):
    path = tmp_path / "snapshot.json"
    original = state_io.safe_replace

    def concurrent_write(source, target, **kwargs):
        original(source, target, **kwargs)
        if replace:
            other = tmp_path / "other-writer"
            other.write_text("snapshot-B", encoding="utf-8")
            other.replace(target)
        else:
            target.write_text("snapshot-B", encoding="utf-8")

    monkeypatch.setattr(state_io, "safe_replace", concurrent_write)
    state_io.write_snapshot_text(path, "snapshot-A")
    assert path.read_text(encoding="utf-8") == "snapshot-B"
    monkeypatch.setattr(state_io, "safe_replace", original)
    state_io.write_snapshot_text(path, "snapshot-A")
    assert path.read_text(encoding="utf-8") == "snapshot-A"


@pytest.mark.skipif(os.name == "nt", reason="symlink privileges differ on Windows")
def test_snapshot_replaces_symlink_without_following_it(tmp_path):
    target = tmp_path / "target"
    target.write_text("private", encoding="utf-8")
    path = tmp_path / "snapshot.json"
    path.symlink_to(target)
    state_io.write_snapshot_text(path, "new state")
    assert not path.is_symlink()
    assert path.read_text(encoding="utf-8") == "new state"
    assert target.read_text(encoding="utf-8") == "private"
