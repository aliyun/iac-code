"""Best-effort multimodal capability probe for OpenAI-compatible endpoints.

Deliberately conservative: many compatible services do not return
``architecture.input_modalities``. In that case the probe returns ``None``
and the caller falls back to user override configuration.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import httpx
import yaml

from iac_code.config import get_config_dir
from iac_code.utils.file_security import safe_replace


def _cache_path() -> Path:
    return get_config_dir() / ".multimodal-cache.yml"


class AutoDetectCache:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, bool]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        path = _cache_path()
        if not path.exists():
            return
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        if isinstance(raw, dict):
            for base_url, models in raw.items():
                if isinstance(models, dict):
                    self._data[str(base_url)] = {str(k): bool(v) for k, v in models.items()}

    def get(self, base_url: str, model: str) -> bool | None:
        return self._data.get(base_url, {}).get(model)

    def set(self, base_url: str, model: str, value: bool) -> None:
        self._data.setdefault(base_url, {})[model] = value
        self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: avoids interleaved writes from concurrent REPL sessions
        # corrupting the cache file. tempfile in the same directory ensures
        # os.replace can rename without crossing filesystems.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".multimodal-cache.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                yaml.dump(self._data, f, default_flow_style=False)
            safe_replace(tmp_name, str(path))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._dirty = False


def probe_openapi_compatible(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    client: httpx.Client | None = None,
    timeout: float = 5.0,
) -> bool | None:
    """Return True / False / None. None means "cannot determine"."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = base_url.rstrip("/") + "/models"
    own_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or []
        for entry in data:
            if entry.get("id") != model:
                continue
            arch = entry.get("architecture") or {}
            modalities = arch.get("input_modalities")
            if isinstance(modalities, list):
                return "image" in modalities
            return None
        return None
    except Exception:
        return None
    finally:
        if own_client:
            client.close()
