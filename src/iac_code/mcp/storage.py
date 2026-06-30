from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

from iac_code.config import get_config_dir
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.state_io import atomic_write_bytes

_FALLBACK_STORE_LOCK = "__fallback_store__"


class MCPSecretStorage:
    def __init__(self, *, keyring_backend: Any | None = None, service_name: str = "iac-code:mcp") -> None:
        if keyring_backend is None and os.environ.get("IAC_CODE_MCP_DISABLE_KEYRING") != "1":
            try:
                import keyring

                keyring_backend = keyring
            except Exception:
                keyring_backend = None
        self._keyring = keyring_backend
        self._service_name = service_name

    def set_secret(self, key: str, value: str) -> None:
        if self._try_keyring_set(key, value):
            return
        with self.lock(_FALLBACK_STORE_LOCK):
            data = self._load_fallback()
            data[key] = value
            self._save_fallback(data)

    def get_secret(self, key: str) -> str | None:
        value = self._try_keyring_get(key)
        if value is not None:
            return value
        with self.lock(_FALLBACK_STORE_LOCK):
            return self._load_fallback().get(key)

    def delete_secret(self, key: str) -> None:
        self._try_keyring_delete(key)
        with self.lock(_FALLBACK_STORE_LOCK):
            data = self._load_fallback()
            if key in data:
                data.pop(key)
                self._save_fallback(data)

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        ensure_private_dir(_fallback_dir())
        path = _fallback_dir() / "locks" / "{}.lock".format(_safe_lock_name(key))
        ensure_private_dir(path.parent)
        with _locked_file(path):
            yield

    def _try_keyring_set(self, key: str, value: str) -> bool:
        if self._keyring is None:
            return False
        try:
            self._keyring.set_password(self._service_name, key, value)
            return True
        except Exception:
            return False

    def _try_keyring_get(self, key: str) -> str | None:
        if self._keyring is None:
            return None
        try:
            return self._keyring.get_password(self._service_name, key)
        except Exception:
            return None

    def _try_keyring_delete(self, key: str) -> None:
        if self._keyring is None:
            return
        try:
            self._keyring.delete_password(self._service_name, key)
        except Exception:
            return

    def _load_fallback(self) -> dict[str, str]:
        path = _fallback_path()
        if not path.exists():
            return {}
        try:
            decrypted = _fernet().decrypt(path.read_bytes())
            data = json.loads(decrypted.decode("utf-8"))
        except (OSError, InvalidToken, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}

    def _save_fallback(self, data: dict[str, str]) -> None:
        ensure_private_dir(_fallback_dir())
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        atomic_write_bytes(_fallback_path(), _fernet().encrypt(payload))
        ensure_private_file(_fallback_path())


def _fallback_dir() -> Path:
    return get_config_dir() / "mcp"


def _fallback_path() -> Path:
    return _fallback_dir() / "secrets.json.enc"


def _key_path() -> Path:
    return _fallback_dir() / "secrets.key"


def _fernet() -> Fernet:
    path = _key_path()
    ensure_private_dir(path.parent)
    key_lock = _fallback_dir() / "locks" / "secrets-key.lock"
    with _locked_file(key_lock):
        if path.exists():
            key = path.read_bytes()
        else:
            key = Fernet.generate_key()
            atomic_write_bytes(path, key)
            ensure_private_file(path)
    return Fernet(key)


@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
    ensure_private_dir(path.parent)
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            try:
                import msvcrt
            except ImportError:  # pragma: no cover - defensive for unusual Python builds.
                yield
                return
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-Windows platforms normally provide fcntl.
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_lock_name(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
