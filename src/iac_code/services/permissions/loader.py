"""Load and merge tool permission configuration from settings files and CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from iac_code.i18n import _
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


def _get_global_settings_path() -> Path:
    """Get the global settings path. Uses iac_code.config.get_settings_path()."""
    from iac_code.config import get_settings_path

    return get_settings_path()


def _empty_permissions_dict() -> dict[str, Any]:
    return {
        "allow": [],
        "deny": [],
        "ask": [],
        "mode": None,
        "additional_directories": [],
    }


def _coerce_str_list(value: Any) -> list[str]:
    if value is None or not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif item is not None:
            out.append(str(item))
    return out


def parse_cli_permission_mode(value: str) -> PermissionMode:
    """Parse a CLI permission mode, raising on invalid explicit input."""
    try:
        return PermissionMode(value)
    except ValueError as exc:
        valid = ", ".join(m.value for m in PermissionMode)
        raise ValueError(_("Invalid --permission-mode {!r}. Valid values: {}").format(value, valid)) from exc


def load_settings_permissions(path: Path, source: str) -> dict[str, Any]:
    """Load the permissions section from a single settings.yml file.

    Returns: {"allow": [...], "deny": [...], "ask": [...], "mode": str|None, "additional_directories": [...]}
    If file doesn't exist or has no permissions section, returns empty lists.
    """
    _ = source
    if not path.exists():
        return _empty_permissions_dict()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse permissions from {}: {}", path, exc)
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    perms = raw.get("permissions")
    if not isinstance(perms, dict):
        return _empty_permissions_dict()

    mode_raw = perms.get("mode")
    mode: str | None
    if isinstance(mode_raw, str) and mode_raw.strip():
        mode = mode_raw.strip()
    else:
        mode = None

    return {
        "allow": _coerce_str_list(perms.get("allow")),
        "deny": _coerce_str_list(perms.get("deny")),
        "ask": _coerce_str_list(perms.get("ask")),
        "mode": mode,
        "additional_directories": _coerce_str_list(perms.get("additional_directories")),
    }


def _apply_yaml_layer(
    data: dict[str, Any],
    source_key: str,
    *,
    allow_rules: dict[str, list[str]],
    deny_rules: dict[str, list[str]],
    ask_rules: dict[str, list[str]],
    additional_directories: list[str],
    mode_holder: list[PermissionMode | None],
) -> None:
    if data["allow"]:
        allow_rules[source_key] = list(data["allow"])
    if data["deny"]:
        deny_rules[source_key] = list(data["deny"])
    if data["ask"]:
        ask_rules[source_key] = list(data["ask"])
    additional_directories.extend(data["additional_directories"])
    if data["mode"] is not None:
        try:
            mode_holder[0] = PermissionMode(data["mode"])
        except ValueError:
            valid = ", ".join(m.value for m in PermissionMode)
            logger.warning("Invalid permission mode '{}' in {}; valid: {}", data["mode"], source_key, valid)


def load_permission_context(
    cwd: str,
    cli_allowed: list[str] | None = None,
    cli_disallowed: list[str] | None = None,
    cli_mode: str | None = None,
) -> ToolPermissionContext:
    """Load and merge all permission configuration layers.

    Priority (later overrides earlier):
    1. global settings → user_settings
    2. project settings → project_settings
    3. local settings → local_settings
    4. CLI args → cli_arg

    Mode: last non-None mode wins.
    """
    cwd_path = Path(cwd)
    allow_rules: dict[str, list[str]] = {}
    deny_rules: dict[str, list[str]] = {}
    ask_rules: dict[str, list[str]] = {}
    additional_directories: list[str] = []
    mode_holder: list[PermissionMode | None] = [None]

    layers: list[tuple[str, Path]] = [
        ("user_settings", _get_global_settings_path()),
        ("project_settings", cwd_path / ".iac-code" / "settings.yml"),
        ("local_settings", cwd_path / ".iac-code" / "settings.local.yml"),
    ]

    for source_key, path in layers:
        layer = load_settings_permissions(path, source_key)
        _apply_yaml_layer(
            layer,
            source_key,
            allow_rules=allow_rules,
            deny_rules=deny_rules,
            ask_rules=ask_rules,
            additional_directories=additional_directories,
            mode_holder=mode_holder,
        )

    if cli_allowed:
        allow_rules["cli_arg"] = list(cli_allowed)
    if cli_disallowed:
        deny_rules["cli_arg"] = list(cli_disallowed)
    if cli_mode is not None:
        mode_holder[0] = parse_cli_permission_mode(cli_mode)

    resolved_mode = mode_holder[0] if mode_holder[0] is not None else PermissionMode.DEFAULT

    return ToolPermissionContext(
        mode=resolved_mode,
        cwd=cwd,
        allow_rules=allow_rules,
        deny_rules=deny_rules,
        ask_rules=ask_rules,
        additional_directories=additional_directories,
    )
