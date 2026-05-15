"""Configuration paths for iac-code.

Provides unified configuration directory and file paths under
``~/.iac-code/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Default LLM model used when no model is saved in settings
DEFAULT_MODEL = "qwen3.6-plus"

# Configuration directory
_CONFIG_DIR_NAME = ".iac-code"

# Configuration files
_CREDENTIALS_FILE = ".credentials.yml"
_SETTINGS_FILE = "settings.yml"
_CLOUD_CREDENTIALS_FILE = ".cloud-credentials.yml"
_HISTORY_FILE = ".input_history"


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file, returning {} when the file does not exist."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to a YAML file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))


# ---------------------------------------------------------------------------
# Provider name normalization (env-facing PascalCase ↔ internal key_name)
# ---------------------------------------------------------------------------


def _build_provider_name_to_key() -> dict[str, str]:
    from iac_code.providers.registry import PROVIDER_REGISTRY

    mapping: dict[str, str] = {}
    for desc in PROVIDER_REGISTRY.values():
        normalized = desc.name.lower().replace(" ", "").replace("-", "").replace("_", "")
        mapping[normalized] = desc.key
        key_norm = desc.key.lower().replace("_", "").replace("-", "")
        mapping[key_norm] = desc.key
    return mapping


_PROVIDER_NAME_TO_KEY: dict[str, str] = _build_provider_name_to_key()


def _build_key_name_to_cred_slot() -> dict[str, str]:
    from iac_code.providers.registry import PROVIDER_REGISTRY

    return {key: key for key in PROVIDER_REGISTRY}


_KEY_NAME_TO_CRED_SLOT: dict[str, str] = _build_key_name_to_cred_slot()


def _build_canonical_names() -> tuple[str, ...]:
    from iac_code.providers.registry import PROVIDER_REGISTRY

    return tuple(desc.name for desc in PROVIDER_REGISTRY.values())


_PROVIDER_CANONICAL_NAMES: tuple[str, ...] = _build_canonical_names()

# Legacy key_name aliases accepted when reading settings.yml (write path always uses
# the canonical key on the right). Keep DashScope's old "bailian" name readable.
_LEGACY_KEY_NAME_ALIASES: dict[str, str] = {
    "bailian": "dashscope",
}

# Model-name prefix → provider key_name.  Used by _detect_provider_name (in
# providers/manager.py) and load_credentials to infer the provider from the
# model string when no explicit provider is configured.
_MODEL_PREFIX_TO_PROVIDER: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
    ("qwen", "dashscope"),
    ("deepseek-", "deepseek"),
    ("gemini-", "gemini"),
    ("glm-", "zhipu_cn"),
    ("kimi-", "kimi_cn"),
    ("minimax-", "minimax_cn"),
    ("doubao-", "volcengine_cn"),
)

# Module-level flag — warn once per process when IAC_CODE_BASE_URL is set
# but the active provider is not OpenAPICompatible. Reset by tests.
_warned_base_url_ignored: bool = False


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


def _get_env_overrides() -> dict[str, str | None]:
    """Read IAC_CODE_* env vars and return a normalized override dict.

    Returns dict with keys: ``provider_key`` (internal key_name or None),
    ``model``, ``api_base``, ``api_key``. Empty/whitespace values are normalized
    to None. Invalid ``IAC_CODE_PROVIDER`` raises ``ValueError`` listing canonical
    names.
    """
    import os

    def _read(name: str) -> str | None:
        raw = os.environ.get(name, "")
        stripped = raw.strip() if raw else ""
        return stripped or None

    provider_raw = _read("IAC_CODE_PROVIDER")
    provider_key: str | None = None
    if provider_raw is not None:
        key = _PROVIDER_NAME_TO_KEY.get(provider_raw.lower())
        if key is None:
            valid = ", ".join(_PROVIDER_CANONICAL_NAMES)
            raise ValueError(
                f"Invalid IAC_CODE_PROVIDER value: {provider_raw!r}. Valid values (case-insensitive): {valid}"
            )
        provider_key = key

    return {
        "provider_key": provider_key,
        "model": _read("IAC_CODE_MODEL"),
        "api_base": _read("IAC_CODE_BASE_URL"),
        "api_key": _read("IAC_CODE_API_KEY"),
    }


def get_llm_source() -> str:
    """Return the LLM source: 'qwenpaw', 'local', or 'env'."""
    env = _get_env_overrides()
    if env["api_key"]:
        return "env"
    try:
        settings = _load_yaml(get_settings_path())
    except Exception:
        return "local"
    source = settings.get("llm_source")
    if isinstance(source, str) and source.strip():
        return source.strip()
    return "local"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_config_dir() -> Path:
    """Get iac-code config directory (~/.iac-code/).

    Creates the directory if it doesn't exist.
    """
    config_dir = Path.home() / _CONFIG_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_credentials_path() -> Path:
    """Get credentials file path (~/.iac-code/.credentials.yml)."""
    return get_config_dir() / _CREDENTIALS_FILE


def get_settings_path() -> Path:
    """Get settings file path (~/.iac-code/settings.yml)."""
    return get_config_dir() / _SETTINGS_FILE


def get_cloud_credentials_path() -> Path:
    """Get cloud credentials file path (~/.iac-code/.cloud-credentials.yml)."""
    return get_config_dir() / _CLOUD_CREDENTIALS_FILE


def get_history_path() -> Path:
    """Get input history file path (~/.iac-code/.input_history)."""
    return get_config_dir() / _HISTORY_FILE


# ---------------------------------------------------------------------------
# Config loaders
# ---------------------------------------------------------------------------


def get_active_provider_key() -> str | None:
    """Return the keyName of the currently active provider, or None.

    ``IAC_CODE_PROVIDER`` env var takes precedence over settings.yml.
    Legacy keyNames in settings.yml (e.g. ``bailian``) are normalized to the
    canonical name (``dashscope``).
    """
    env_key = _get_env_overrides()["provider_key"]
    if env_key:
        return env_key
    settings = _load_yaml(get_settings_path())
    active = settings.get("activeProvider")
    if isinstance(active, str) and active:
        return _LEGACY_KEY_NAME_ALIASES.get(active, active)
    return None


def get_provider_config(key_name: str) -> dict[str, Any]:
    """Return the persisted per-provider config dict (empty when unset).

    When ``key_name`` is the active provider, IAC_CODE_MODEL and
    IAC_CODE_BASE_URL env values are overlaid. IAC_CODE_BASE_URL only
    applies when the active provider is ``openapi_compatible``; setting
    it for other providers logs a one-time warning and is ignored.
    """
    global _warned_base_url_ignored

    settings = _load_yaml(get_settings_path())
    providers = settings.get("providers")
    entry: dict[str, Any] = {}
    if isinstance(providers, dict):
        raw = providers.get(key_name)
        if not isinstance(raw, dict):
            for legacy, canonical in _LEGACY_KEY_NAME_ALIASES.items():
                if canonical == key_name:
                    legacy_raw = providers.get(legacy)
                    if isinstance(legacy_raw, dict):
                        raw = legacy_raw
                        break
        if isinstance(raw, dict):
            entry = dict(raw)

    env = _get_env_overrides()
    active_key = env["provider_key"]
    if active_key is None:
        active = settings.get("activeProvider")
        if isinstance(active, str) and active:
            active_key = _LEGACY_KEY_NAME_ALIASES.get(active, active)

    if key_name == active_key:
        if env["model"]:
            entry["model"] = env["model"]
        if env["api_base"]:
            if active_key == "openapi_compatible":
                entry["apiBase"] = env["api_base"]
            elif not _warned_base_url_ignored:
                from loguru import logger

                logger.warning(
                    "IAC_CODE_BASE_URL is set but active provider is "
                    f"{active_key!r}; the value is ignored. "
                    "IAC_CODE_BASE_URL only applies to OpenAPICompatible."
                )
                _warned_base_url_ignored = True

    return entry


def load_saved_model() -> str | None:
    """Load the active provider's saved model from settings.yml."""
    key = get_active_provider_key()
    if not key:
        return None
    model = get_provider_config(key).get("model")
    return model if isinstance(model, str) and model else None


def load_saved_effort() -> str | None:
    """Load saved effort level from settings.yml."""
    settings = _load_yaml(get_settings_path())
    effort = settings.get("effort")
    return effort if isinstance(effort, str) else None


def load_active_provider_config() -> dict[str, Any] | None:
    """Load the active provider's full config, including its keyName."""
    key = get_active_provider_key()
    if not key:
        return None
    cfg = dict(get_provider_config(key))
    cfg["keyName"] = key
    return cfg


def _infer_provider_key_from_model(model: str) -> str | None:
    """Return the provider key_name inferred from a model name prefix, or None."""
    model_lower = model.lower()
    for prefix, provider in _MODEL_PREFIX_TO_PROVIDER:
        if model_lower.startswith(prefix):
            return provider
    return None


def load_credentials(model: str | None = None) -> dict[str, str]:
    """Load API credentials from ``.credentials.yml`` with env override applied.

    Returns a dict with six fixed slots: ``anthropic``, ``openai``,
    ``dashscope``, ``dashscope_token_plan``, ``deepseek``,
    ``openapi_compatible``. The ``dashscope`` slot also accepts the legacy
    ``bailian`` key in the YAML file (file's ``dashscope`` value takes
    precedence when both are present).

    When ``IAC_CODE_API_KEY`` is set, the target slot is determined by:
    1. ``IAC_CODE_PROVIDER`` env var (explicit)
    2. ``activeProvider`` in settings.yml
    3. Model-name prefix heuristic (requires *model* argument)
    """
    try:
        raw = _load_yaml(get_credentials_path())
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    from iac_code.providers.registry import PROVIDER_REGISTRY

    creds: dict[str, str] = {}
    for key in PROVIDER_REGISTRY:
        creds[key] = str(raw.get(key, "") or "")
    if not creds.get("dashscope"):
        creds["dashscope"] = str(raw.get("bailian", "") or "")

    env = _get_env_overrides()
    if env["api_key"]:
        active_key = env["provider_key"] or get_active_provider_key()
        if active_key is None and model:
            active_key = _infer_provider_key_from_model(model)
        if active_key:
            slot = _KEY_NAME_TO_CRED_SLOT.get(active_key)
            if slot:
                creds[slot] = env["api_key"]

    return creds
