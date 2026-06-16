"""Multimodal capability registry.

Indexed by model name (not provider+model) — different providers that share
the same model name (e.g. ``kimi-k2.5``) reuse the same spec.

Resolution order: settings.yml override > provider registry (``ModelEntry.
support_multimodal``) > OpenAI-compatible auto-detect (when applicable) >
default (no image support).
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from iac_code.config import get_settings_path

DEFAULT_FORMATS: tuple[str, ...] = ("image/png", "image/jpeg", "image/gif", "image/webp")


@dataclass(frozen=True)
class MultiModalSpec:
    support_multimodal: bool = False
    formats: tuple[str, ...] = DEFAULT_FORMATS
    max_images_per_message: int = 20


_NO_IMAGES = MultiModalSpec(support_multimodal=False)
_DEFAULT_VL = MultiModalSpec(support_multimodal=True)


def _builtin_multimodal_models() -> set[str]:
    """Collect every model id flagged as multimodal in the provider registry.

    Imported lazily to avoid an import cycle with ``providers.registry`` at
    module load time, and to keep the lookup live across hot-reloads.
    """
    from iac_code.providers.registry import PROVIDER_REGISTRY

    out: set[str] = set()
    for desc in PROVIDER_REGISTRY.values():
        for m in desc.models:
            if m.support_multimodal:
                out.add(m.id)
    return out


def _load_settings_overrides() -> dict[str, MultiModalSpec]:
    path = get_settings_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    section = data.get("multiModal") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return {}
    raw_models = section.get("models")
    if not isinstance(raw_models, dict):
        return {}
    out: dict[str, MultiModalSpec] = {}
    for name, value in raw_models.items():
        if not isinstance(value, dict):
            continue
        out[str(name)] = MultiModalSpec(
            support_multimodal=bool(value.get("supportMultimodal", False)),
            formats=tuple(value.get("formats", DEFAULT_FORMATS)),
            max_images_per_message=int(value.get("maxImagesPerMessage", 20)),
        )
    return out


def get_multimodal_spec(model: str) -> MultiModalSpec:
    """Resolve the multimodal spec for a model.

    Order: 1) settings.yml override 2) provider registry flag 3) default (no images).
    """
    overrides = _load_settings_overrides()
    if model in overrides:
        return overrides[model]
    if model in _builtin_multimodal_models():
        return _DEFAULT_VL
    return _NO_IMAGES


def is_model_multimodal(
    model: str,
    *,
    provider_key: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> bool:
    overrides = _load_settings_overrides()
    if model in overrides:
        return overrides[model].support_multimodal
    if model in _builtin_multimodal_models():
        return True
    if provider_key == "openai_compatible" and base_url:
        from iac_code.services.capabilities.auto_detect import (
            AutoDetectCache,
            probe_openai_compatible,
        )

        cache = AutoDetectCache()
        cached = cache.get(base_url, model)
        if cached is not None:
            return cached
        result = probe_openai_compatible(base_url=base_url, api_key=api_key, model=model)
        if result is not None:
            cache.set(base_url, model, result)
            cache.flush()
            return result
    return False
