from __future__ import annotations

import os
import re
from typing import Any, Mapping

from iac_code.i18n import _
from iac_code.mcp.types import MCPConfigWarning

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def expand_env(
    value: Any,
    *,
    env: Mapping[str, str] | None = None,
    source: str,
    server_name: str | None = None,
) -> tuple[Any, list[MCPConfigWarning]]:
    """Recursively expand MCP config environment references."""

    warnings: list[MCPConfigWarning] = []
    env_values = os.environ if env is None else env

    def expand_one(item: Any) -> Any:
        if isinstance(item, str):
            return _expand_string(item, env=env_values, source=source, server_name=server_name, warnings=warnings)
        if isinstance(item, list):
            return [expand_one(child) for child in item]
        if isinstance(item, tuple):
            return tuple(expand_one(child) for child in item)
        if isinstance(item, dict):
            return {key: expand_one(child) for key, child in item.items()}
        return item

    return expand_one(value), warnings


def _expand_string(
    value: str,
    *,
    env: Mapping[str, str],
    source: str,
    server_name: str | None,
    warnings: list[MCPConfigWarning],
) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        current = env.get(name)
        if current is not None and (current != "" or default is None):
            return current
        if default is not None:
            return default

        warnings.append(
            MCPConfigWarning(
                source=source,
                server_name=server_name,
                code="missing_env",
                message=_("Environment variable {name!r} is not set for MCP config.").format(name=name),
            )
        )
        return match.group(0)

    return _ENV_REF_RE.sub(replace, value)
