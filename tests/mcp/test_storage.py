from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path

import iac_code.mcp.storage as storage_module
from iac_code.mcp.storage import MCPSecretStorage, _safe_lock_name


def test_fallback_secret_store_uses_lock_for_file_io(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    storage = MCPSecretStorage()
    lock_calls: list[str] = []

    @contextmanager
    def fake_lock(key: str):
        lock_calls.append(key)
        yield

    monkeypatch.setattr(storage, "lock", fake_lock)

    storage.set_secret("mcp:access_token:test", "token")
    assert storage.get_secret("mcp:access_token:test") == "token"
    storage.delete_secret("mcp:access_token:test")

    assert lock_calls == ["__fallback_store__", "__fallback_store__", "__fallback_store__"]


def test_safe_lock_name_does_not_use_plain_sha256() -> None:
    value = "mcp:access_token:secret-like-key"

    lock_name = _safe_lock_name(value)

    assert lock_name == _safe_lock_name(value)
    assert len(lock_name) == 64
    int(lock_name, 16)
    assert lock_name != hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_storage_lock_serializes_storage_instances_in_process(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    first_storage = MCPSecretStorage(keyring_backend=False)
    second_storage = MCPSecretStorage(keyring_backend=False)
    first_entered = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    @contextmanager
    def unlocked_file(_path: Path):
        yield

    monkeypatch.setattr(storage_module, "_locked_file", unlocked_file)

    def hold_first_lock() -> None:
        with first_storage.lock("shared-key"):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def enter_second_lock() -> None:
        second_attempted.set()
        with second_storage.lock("shared-key"):
            second_entered.set()

    first = threading.Thread(target=hold_first_lock)
    second = threading.Thread(target=enter_second_lock)
    first.start()
    try:
        assert first_entered.wait(timeout=1)
        second.start()
        assert second_attempted.wait(timeout=1)
        assert not second_entered.wait(timeout=0.05)
    finally:
        release_first.set()
        first.join(timeout=1)
        if second.ident is not None:
            second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
