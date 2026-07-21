"""Permission types for the tool system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

MAX_PERMISSION_AUDIT_FILES = 100
MAX_PERMISSION_AUDIT_ITEMS = 8

ExecutionClass = Literal["concurrent", "serial"]


@dataclass(frozen=True)
class InvocationBinding:
    """Immutable identity binding for one model-originated tool invocation."""

    runtime_nonce: str
    session_id: str
    tool_use_id: str
    tool_name: str
    canonical_input_sha256: str


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
    display_text: str | None = None

    def display_label(self) -> str:
        """Return the user-facing label for this rule."""
        return self.display_text or self.rule_content


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
    reasons: list[PermissionDecisionReason] | None = None
    suggestions: list[PermissionRuleValue] | None = None
    audit: PermissionAuditMetadata | None = None
    audit_items: tuple[PermissionAuditMetadata, ...] = ()
    invocation_binding: InvocationBinding | None = None
    snapshot_id: str | None = None
    security_digest: str | None = None
    execution_class: ExecutionClass | None = None


@dataclass
class ToolPermissionContext:
    """Resolved permission rules and workspace constraints for tool checks."""

    mode: PermissionMode = PermissionMode.DEFAULT
    cwd: str = ""
    invocation_binding: InvocationBinding | None = None
    pipeline_mode: bool = False
    allow_rules: dict[str, list[str]] = field(default_factory=dict)
    deny_rules: dict[str, list[str]] = field(default_factory=dict)
    ask_rules: dict[str, list[str]] = field(default_factory=dict)
    additional_directories: list[str] = field(default_factory=list)
    trusted_read_directories: list[str] = field(default_factory=list)
    relative_read_directories: list[str] = field(default_factory=list)
    strict_read_directories: list[str] = field(default_factory=list)
    read_path_violation_behavior: Literal["ask", "deny"] = "ask"
    audit_settings: PermissionAuditSettings = field(default_factory=PermissionAuditSettings)


PermissionDecision = Literal["always_allow", "always_deny"]
