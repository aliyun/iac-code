"""Desktop-only explicit executable and PATH directory settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from iac_code.config import _load_yaml, _save_yaml, get_settings_path

TOOLS = ("git", "terraform", "node", "npm", "npx", "infraguard")


def load_desktop_tool_paths() -> dict[str, Any]:
    settings = _load_yaml(get_settings_path())
    desktop = settings.get("desktop")
    if not isinstance(desktop, Mapping):
        desktop = {}
    raw_tools = desktop.get("toolPaths")
    raw_search = desktop.get("searchPaths")
    tool_paths = {
        name: value
        for name, value in (raw_tools.items() if isinstance(raw_tools, Mapping) else ())
        if name in TOOLS and isinstance(value, str) and value
    }
    search_paths = (
        [value for value in raw_search if isinstance(value, str) and value]
        if isinstance(raw_search, list)
        else []
    )
    return {"toolPaths": tool_paths, "searchPaths": search_paths}


def save_desktop_tool_paths(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_tools = payload.get("toolPaths", {})
    raw_search = payload.get("searchPaths", [])
    if not isinstance(raw_tools, Mapping) or not isinstance(raw_search, list):
        raise ValueError("toolPaths must be an object and searchPaths must be a list")
    unknown = sorted(set(raw_tools) - set(TOOLS))
    if unknown:
        raise ValueError("unsupported Desktop tool: {}".format(", ".join(str(value) for value in unknown)))

    tool_paths: dict[str, str] = {}
    for name in TOOLS:
        raw_value = raw_tools.get(name)
        if raw_value in (None, ""):
            continue
        if not isinstance(raw_value, str):
            raise ValueError("Desktop tool paths must be strings")
        path = Path(raw_value).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Desktop tool paths must be absolute existing files")
        if path.name.casefold() not in {name.casefold(), "{}.exe".format(name).casefold()}:
            raise ValueError("Desktop tool path filename does not match its tool key")
        # Preserve the user-facing executable basename. Resolving a symlink
        # such as ``npm -> npm-cli.js`` would make the Host reject this valid
        # key/path pair on its next startup.
        tool_paths[name] = str(path.absolute())

    search_paths: list[str] = []
    for raw_value in raw_search:
        if not isinstance(raw_value, str):
            raise ValueError("Desktop search paths must be strings")
        value = raw_value.strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Desktop search paths must be absolute existing directories")
        normalized = str(path.resolve())
        if normalized not in search_paths:
            search_paths.append(normalized)

    settings_path = get_settings_path()
    settings = _load_yaml(settings_path)
    desktop = settings.get("desktop")
    if not isinstance(desktop, dict):
        desktop = {}
    desktop["toolPaths"] = tool_paths
    desktop["searchPaths"] = search_paths
    settings["desktop"] = desktop
    _save_yaml(settings_path, settings)
    return {"toolPaths": tool_paths, "searchPaths": search_paths}
