"""QwenPaw mode — read LLM config from QwenPaw's active_model.json."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from iac_code.i18n import _


class QwenPawError(RuntimeError):
    """Raised when QwenPaw configuration is present but invalid."""


@dataclass(frozen=True)
class QwenPawConfig:
    """Resolved QwenPaw configuration."""

    model: str
    provider_key: str
    api_key: str | None
    base_url: str | None


def _resolve_secret_dir() -> Path | None:
    """Resolve QwenPaw SECRET_DIR from env, home dir, or working directory.

    Search order mirrors QwenPaw's own constant.py:
    1. QWENPAW_SECRET_DIR / COPAW_SECRET_DIR env var
    2. ~/.qwenpaw.secret  (default)
    3. ~/.copaw.secret    (legacy)
    4. .secret in cwd or parent directories
    """
    env_dir = os.environ.get("QWENPAW_SECRET_DIR") or os.environ.get("COPAW_SECRET_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        return p if p.is_dir() else None

    home = Path.home()
    for name in (".qwenpaw.secret", ".copaw.secret"):
        p = home / name
        if p.is_dir():
            return p

    cwd = Path.cwd()
    secret = cwd / ".secret"
    if secret.is_dir():
        return secret
    for parent in cwd.parents:
        secret = parent / ".secret"
        if secret.is_dir():
            return secret
    return None


def _read_active_model(secret_dir: Path) -> dict | None:
    """Read active_model.json from SECRET_DIR/providers/."""
    for candidate in (secret_dir / "providers" / "active_model.json", secret_dir / "active_model.json"):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                logger.debug("Failed to read {}", candidate)
    return None


def _read_provider_config(secret_dir: Path, provider_id: str) -> dict | None:
    """Read a QwenPaw provider config file.

    Searches builtin/, custom/, plugin/ sub-directories under providers/,
    then falls back to providers/{id}.json for compatibility.
    """
    providers_root = secret_dir / "providers"
    candidates = [
        providers_root / "builtin" / f"{provider_id}.json",
        providers_root / "custom" / f"{provider_id}.json",
        providers_root / "plugin" / f"{provider_id}.json",
        providers_root / f"{provider_id}.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                logger.debug("Failed to read provider config: {}", path)
    return None


_ENC_PREFIX = "ENC:"


def _decrypt_api_key(raw_value: str) -> str | None:
    """Decrypt a Fernet-encrypted api_key using OS keychain or .master_key file.

    QwenPaw stores encrypted values with an ``ENC:`` prefix. Plaintext values
    (no prefix) are returned as-is for backward compatibility.
    """
    if not raw_value:
        return None

    if not raw_value.startswith(_ENC_PREFIX):
        return raw_value

    ciphertext = raw_value[len(_ENC_PREFIX) :]

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning(
            "cryptography package not installed; cannot decrypt QwenPaw API key. "
            "Install it with: pip install cryptography"
        )
        return None

    master_key = _get_master_key()
    if not master_key:
        return None

    try:
        raw_bytes = bytes.fromhex(master_key) if isinstance(master_key, str) else master_key
        fernet_key = base64.urlsafe_b64encode(raw_bytes[:32])
        f = Fernet(fernet_key)
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        logger.warning(
            "Failed to decrypt QwenPaw API key. The master key may be incorrect or the encrypted data may be corrupted."
        )
        return None


def _get_master_key() -> str | None:
    """Get master key from OS keychain (qwenpaw/copaw) or .master_key file."""
    try:
        import keyring

        key = keyring.get_password("qwenpaw", "master_key")
        if key:
            return key
        key = keyring.get_password("copaw", "master_key")
        if key:
            return key
    except Exception:
        logger.debug("OS Keychain unavailable for QwenPaw master key lookup")

    secret_dir = _resolve_secret_dir()
    if secret_dir:
        master_key_file = secret_dir / ".master_key"
        if master_key_file.exists():
            try:
                content = master_key_file.read_text(encoding="utf-8").strip()
                if content:
                    bytes.fromhex(content)
                    return content
            except (OSError, ValueError):
                logger.debug("Master key file is corrupt or unreadable")

    logger.warning(
        "QwenPaw master key not found. Checked: OS Keychain (qwenpaw/copaw) "
        "and .master_key file in SECRET_DIR. Encrypted API keys cannot be decrypted."
    )
    return None


def _map_qwenpaw_provider_id(qwenpaw_id: str) -> str | None:
    """Map a QwenPaw provider ID to an iac-code provider key."""
    from iac_code.providers.registry import PROVIDER_REGISTRY

    for desc in PROVIDER_REGISTRY.values():
        if qwenpaw_id in desc.qwenpaw_provider_ids:
            return desc.key
    return None


def load_from_qwenpaw() -> QwenPawConfig | None:
    """Load QwenPaw configuration. Returns None if unavailable."""
    secret_dir = _resolve_secret_dir()
    if not secret_dir:
        return None

    active = _read_active_model(secret_dir)
    if not active:
        return None

    model = active.get("model")
    qwenpaw_provider_id = active.get("provider_id") or active.get("provider")
    if not model or not qwenpaw_provider_id:
        return None

    provider_key = _map_qwenpaw_provider_id(qwenpaw_provider_id)
    if not provider_key:
        from iac_code.providers.registry import PROVIDER_REGISTRY

        known_ids = sorted(pid for desc in PROVIDER_REGISTRY.values() for pid in desc.qwenpaw_provider_ids)
        raise QwenPawError(
            _(
                "[QwenPaw mode] Unknown provider '{provider_id}'. "
                "iac-code does not support this provider.\n"
                "Supported QwenPaw provider IDs: {supported_ids}\n"
                "To fix: switch to a supported provider in QwenPaw, "
                "or disable QwenPaw mode (remove 'llm_source: qwenpaw' from settings.yml)."
            ).format(provider_id=qwenpaw_provider_id, supported_ids=", ".join(known_ids))
        )

    provider_config = _read_provider_config(secret_dir, qwenpaw_provider_id)
    api_key: str | None = None
    base_url: str | None = None

    if provider_config:
        raw_key = provider_config.get("api_key", "")
        if raw_key:
            api_key = _decrypt_api_key(raw_key)
        base_url = provider_config.get("base_url") or None

    from iac_code.providers.registry import PROVIDER_REGISTRY

    desc = PROVIDER_REGISTRY.get(provider_key)

    if not base_url and desc:
        base_url = desc.base_url

    if api_key is None and desc and desc.require_api_key:
        logger.warning(
            "QwenPaw provider '{}' requires an API key but decryption failed. "
            "LLM requests will likely fail until this is resolved.",
            provider_key,
        )

    return QwenPawConfig(
        model=model,
        provider_key=provider_key,
        api_key=api_key,
        base_url=base_url,
    )
