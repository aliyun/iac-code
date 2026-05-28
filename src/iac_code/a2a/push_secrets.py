from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from iac_code.utils.file_security import restrict_file_permissions


class A2APushSecretError(ValueError):
    pass


class A2APushSecretKeyring:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._loaded = False
        self._env_managed = False
        self._active_key_id = ""
        self._keys: dict[str, str] = {}

    @property
    def active_key_id(self) -> str:
        self._ensure_loaded()
        return self._active_key_id

    def encrypt(self, value: str) -> dict[str, str]:
        self._ensure_loaded()
        key_id = self._active_key_id
        token = Fernet(self._keys[key_id].encode("ascii")).encrypt(value.encode("utf-8")).decode("ascii")
        return {"keyId": key_id, "ciphertext": token}

    def decrypt(self, envelope: dict[str, Any]) -> str:
        self._ensure_loaded()
        key_id = str(envelope.get("keyId") or "")
        ciphertext = str(envelope.get("ciphertext") or "")
        key = self._keys.get(key_id)
        if not key:
            raise A2APushSecretError(f"A2A push secret encryption key is not available: {key_id}")
        try:
            return Fernet(key.encode("ascii")).decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise A2APushSecretError("A2A push secret ciphertext could not be decrypted") from exc

    def rotate(self, key_id: str | None = None) -> str:
        self._ensure_loaded()
        if self._env_managed:
            raise A2APushSecretError("A2A push secret keyring is environment-managed and cannot be rotated locally")
        key_id = key_id or _new_key_id()
        if key_id in self._keys:
            raise A2APushSecretError(f"A2A push secret encryption key already exists: {key_id}")
        self._keys[key_id] = Fernet.generate_key().decode("ascii")
        self._active_key_id = key_id
        self._write()
        return key_id

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        env_keyring = os.environ.get("IAC_CODE_A2A_PUSH_KEYRING")
        if env_keyring:
            self._load_data(json.loads(env_keyring))
            self._env_managed = True
            self._loaded = True
            return
        if self._path.exists():
            self._load_data(json.loads(self._path.read_text(encoding="utf-8")))
            self._loaded = True
            return
        self._active_key_id = _new_key_id()
        self._keys = {self._active_key_id: Fernet.generate_key().decode("ascii")}
        self._loaded = True
        self._write()

    def _load_data(self, data: dict[str, Any]) -> None:
        keys = data.get("keys")
        if not isinstance(keys, list):
            raise A2APushSecretError("A2A push secret keyring is malformed")
        self._keys = {
            str(item["id"]): str(item["fernetKey"])
            for item in keys
            if isinstance(item, dict) and item.get("id") and item.get("fernetKey")
        }
        self._active_key_id = str(data.get("activeKeyId") or "")
        if not self._active_key_id or self._active_key_id not in self._keys:
            raise A2APushSecretError("A2A push secret keyring does not contain its active key")

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        restrict_file_permissions(self._path.parent, directory=True)
        data = {
            "activeKeyId": self._active_key_id,
            "keys": [
                {"id": key_id, "fernetKey": key, "createdAt": int(time.time())}
                for key_id, key in sorted(self._keys.items())
            ],
        }
        self._path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        restrict_file_permissions(self._path, directory=False)


def _new_key_id() -> str:
    return f"push-{int(time.time())}-{uuid.uuid4().hex[:12]}"
