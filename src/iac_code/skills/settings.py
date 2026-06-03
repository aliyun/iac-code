"""Persistence helpers for skill enable/disable settings."""

from __future__ import annotations

from typing import Any

import yaml

from iac_code.config import get_settings_path
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file


def normalize_skill_name(name: str) -> str:
    """Normalize skill names for settings and command lookup."""
    return name.lstrip("/$").strip().lower()


def _load_settings() -> dict[str, Any]:
    path = get_settings_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_disabled_skills() -> set[str]:
    """Return normalized disabled skill names from settings.yml."""
    raw = _load_settings().get("disabled_skills")
    if not isinstance(raw, list):
        return set()

    disabled: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = normalize_skill_name(item)
        if name:
            disabled.add(name)
    return disabled


def save_disabled_skills(disabled: set[str], *, locked_skill_names: set[str] | None = None) -> None:
    """Persist normalized disabled skill names, preserving unrelated settings."""
    locked = {normalize_skill_name(name) for name in (locked_skill_names or set())}
    normalized = sorted(
        name for name in {normalize_skill_name(item) for item in disabled} if name and name not in locked
    )

    path = get_settings_path()
    data = _load_settings()
    if normalized:
        data["disabled_skills"] = normalized
    else:
        data.pop("disabled_skills", None)

    ensure_private_dir(path.parent)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    ensure_private_file(path)
