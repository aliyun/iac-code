from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path

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
