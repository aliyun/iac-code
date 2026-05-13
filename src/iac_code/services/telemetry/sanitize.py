"""Sanitization helpers for telemetry text fields."""

from __future__ import annotations

import re
from typing import Literal

from iac_code.services.telemetry.config import is_essential_traffic_only
from iac_code.services.telemetry.constants import (
    BUNDLED_SKILLS,
    CUSTOM_ROS_RESOURCE_PLACEHOLDER,
    CUSTOM_SKILL_PLACEHOLDER,
    CUSTOM_TF_PROVIDER_PLACEHOLDER,
    CUSTOM_TF_RESOURCE_PLACEHOLDER,
    KNOWN_MODELS,
    MCP_TOOL_PLACEHOLDER,
    OTHER_MODEL_PLACEHOLDER,
    ROS_ALLOWED_PREFIXES,
    TERRAFORM_OFFICIAL_PROVIDERS,
)

_CONTROL_CHARS_RE = re.compile(r"[\n\r\t\x00-\x1f]+")
_MAX_ERROR_MSG_BYTES = 512
_TRUNCATION_MARKER = "... (truncated)"
_DEV_VERSION_SUFFIX_RE = re.compile(r"-\d{8}$")


def sanitize_error_message(raw: str | None) -> str | None:
    """Clean and truncate an error message. None under essential-traffic mode."""
    if raw is None:
        return None
    if is_essential_traffic_only():
        return None
    cleaned = _CONTROL_CHARS_RE.sub(" ", raw).strip()
    encoded = cleaned.encode("utf-8")
    if len(encoded) > _MAX_ERROR_MSG_BYTES:
        keep = _MAX_ERROR_MSG_BYTES - len(_TRUNCATION_MARKER.encode("utf-8"))
        cleaned = encoded[:keep].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
    return cleaned


def sanitize_skill_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    return raw if raw in BUNDLED_SKILLS else CUSTOM_SKILL_PLACEHOLDER


def sanitize_resource_type(raw: str, kind: Literal["ros", "terraform"]) -> str:
    """Keep official resource types; replace custom/unknown with placeholder."""
    if kind == "ros":
        if raw.startswith(ROS_ALLOWED_PREFIXES):
            return raw
        return CUSTOM_ROS_RESOURCE_PLACEHOLDER
    # Terraform: resource types are of the form `<provider>_<resource>`.
    if "_" not in raw:
        return CUSTOM_TF_RESOURCE_PLACEHOLDER
    provider = raw.split("_", 1)[0]
    if provider in TERRAFORM_OFFICIAL_PROVIDERS:
        return raw
    return CUSTOM_TF_RESOURCE_PLACEHOLDER


def sanitize_terraform_provider(raw: str) -> str:
    return raw if raw in TERRAFORM_OFFICIAL_PROVIDERS else CUSTOM_TF_PROVIDER_PLACEHOLDER


def sanitize_model_name(raw: str) -> str:
    """Trim dev suffix (-YYYYMMDD) and map to known set or 'other'."""
    base = _DEV_VERSION_SUFFIX_RE.sub("", raw)
    return base if base in KNOWN_MODELS else OTHER_MODEL_PLACEHOLDER


def sanitize_tool_name(raw: str) -> str:
    """MCP tools are collapsed to a single placeholder."""
    if raw.startswith("mcp__"):
        return MCP_TOOL_PLACEHOLDER
    return raw


def bucket_resource_count(n: int) -> str:
    """bucket for iac.deployment.duration histogram."""
    if n <= 5:
        return "1-5"
    if n <= 20:
        return "6-20"
    if n <= 50:
        return "21-50"
    return "50+"
