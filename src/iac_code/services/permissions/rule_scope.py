"""Shared permission rule source to audit scope mapping."""

from __future__ import annotations


def scope_for_rule_source(source: str) -> str:
    if source == "cli_arg":
        return "cli_rule"
    if source == "session":
        return "session_rule"
    if source.endswith("_settings"):
        return "settings_rule"
    return source
