"""Permission types for the tool system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

MAX_PERMISSION_AUDIT_FILES = 100


class PermissionMode(str, Enum):
    """Permission mode."""

    DEFAULT = "default"  # Write operations require user confirmation
    ACCEPT_EDITS = "accept_edits"
    BYPASS_PERMISSIONS = "bypass_permissions"
    DONT_ASK = "dont_ask"


class PermissionRuleSource(str, Enum):
    """Where a permission rule was loaded from."""

    USER_SETTINGS = "user_settings"
    PROJECT_SETTINGS = "project_settings"
    LOCAL_SETTINGS = "local_settings"
    CLI_ARG = "cli_arg"
    SESSION = "session"


@dataclass
class PermissionRuleValue:
    """A concrete permission rule entry for a tool."""

    tool_name: str
    rule_content: str


@dataclass
class PermissionRule:
    """A permission rule with provenance and effect."""

    source: PermissionRuleSource
    behavior: Literal["allow", "deny", "ask"]
    value: PermissionRuleValue


@dataclass
class PermissionDecisionReason:
    """Structured explanation for a permission outcome."""

    type: str
    detail: str


@dataclass
class PermissionAuditSettings:
    """Resolved permission audit settings."""

    include_tool_input: bool = False
    max_file_bytes: int = 10 * 1024 * 1024
    max_files: int = 5


@dataclass
class PermissionAuditMetadata:
    """Structured metadata used by the permission audit service."""

    scope: str
    source: str
    rule_source: str | None = None
    rule: str | None = None
    reason_type: str | None = None
    reason_detail: str | None = None
    is_read_only: bool | None = None
    operation: dict[str, object] = field(default_factory=dict)


@dataclass
class PermissionResult:
    """Permission check result."""

    behavior: Literal["allow", "deny", "ask", "passthrough"]
    message: str = ""
    reason: PermissionDecisionReason | None = None
    suggestions: list[PermissionRuleValue] | None = None
    audit: PermissionAuditMetadata | None = None


@dataclass
class ToolPermissionContext:
    """Resolved permission rules and workspace constraints for tool checks."""

    mode: PermissionMode = PermissionMode.DEFAULT
    cwd: str = ""
    allow_rules: dict[str, list[str]] = field(default_factory=dict)
    deny_rules: dict[str, list[str]] = field(default_factory=dict)
    ask_rules: dict[str, list[str]] = field(default_factory=dict)
    additional_directories: list[str] = field(default_factory=list)
    trusted_read_directories: list[str] = field(default_factory=list)
    audit_settings: PermissionAuditSettings = field(default_factory=PermissionAuditSettings)


PermissionDecision = Literal["always_allow", "always_deny"]
