"""Permission audit logging and telemetry."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from iac_code.config import get_config_dir
from iac_code.services.telemetry import log_event
from iac_code.services.telemetry.names import Events
from iac_code.types.permissions import MAX_PERMISSION_AUDIT_FILES, PermissionAuditSettings
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.public_errors import sanitize_public_text
from iac_code.utils.state_io import append_jsonl_rotating_locked

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_RULE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.*:-]{0,127}$")
_SAFE_WRAPPED_RULE_TEXT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(([A-Za-z0-9][A-Za-z0-9_.*:-]{0,127})\)$")
_SAFE_PATHNAME_SHAPE = re.compile(r"^/(?:\{segment\}(?:/\{segment\})*)?$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{16}$")
_SECRET_KEY_PARTS = (
    "accesskey",
    "apikey",
    "auth",
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "privatekey",
    "pwd",
    "secret",
    "session",
    "signature",
    "ststoken",
    "token",
    "password",
    "passwd",
)
_TEXT_MAX_CHARS = 160
_REDACTED_MAX_DEPTH = 16
_REDACTED_MAX_FIELDS = 64
_REDACTED_MAX_NODES = 512
_TRUNCATED_FIELD = "_truncated"
_DISPLAY_TEXT_EDGE_CHARS = 160
_DISPLAY_PRIORITY_FIELD_NAMES = (
    "command",
    "cmd",
    "path",
    "file_path",
    "action",
    "product",
    "params",
    "body",
    "method",
    "pathname",
    "region",
    "region_id",
    "cwd",
    "timeout",
    "content",
    "input",
    "query",
    "url",
)
_PROMPT_RAW_FIELD_NAMES = frozenset(("command", "cmd", "path", "file_path"))
_SAFE_SHAPE_FIELD_NAMES = frozenset(
    {
        *_DISPLAY_PRIORITY_FIELD_NAMES,
        _TRUNCATED_FIELD,
        "fields",
        "fields_truncated",
        "field_count",
        "tool_name",
        "type",
        "length",
        "fingerprint",
        "redacted",
        "truncated",
        "prefix",
        "suffix",
        "is_read_only",
        "headers",
        "payload",
        "params_fields",
        "params_field_count",
        "params_field_shapes",
        "params_fields_truncated",
        "params_shape",
        "body_fields",
        "body_field_count",
        "body_field_shapes",
        "body_fields_truncated",
        "body_shape",
        "pathname_shape",
        "pathname_fingerprint",
        "product_fingerprint",
        "action_fingerprint",
        "region_fingerprint",
        "style",
    }
)
_OPERATION_ID_KEYS = ("product", "action", "region")
_OPERATION_FINGERPRINT_KEYS = ("product_fingerprint", "action_fingerprint", "region_fingerprint")
_SECRET_VALUE_PATTERN = r"""(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}|"(?:(?:\\.)|[^"\\])*"|'(?:(?:\\.)|[^'\\])*'|[^\s,;}]+)"""
_SECRET_KEY_PATTERN = (
    r"auth|authorization|cookie|credential|credentials|passphrase|password|passwd|private[-_]?key|pwd|"
    r"secret|session|signature|token|x[-_]?api[-_]?key|api[-_]?key|"
    r"sts[-_]?token|access[-_]?key(?:[-_]?(?:id|secret))?"
)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>
        [A-Za-z0-9_.-]*
        (?:"""
    + _SECRET_KEY_PATTERN
    + r""")
        [A-Za-z0-9_.-]*
    )
    (?P<separator>\s*[:=]\s*)
    """
    + _SECRET_VALUE_PATTERN
)
_QUOTED_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<quote>["'])
    (?P<key>
        [A-Za-z0-9_.-]*
        (?:"""
    + _SECRET_KEY_PATTERN
    + r""")
        [A-Za-z0-9_.-]*
    )
    (?P=quote)
    (?P<separator>\s*:\s*)
    """
    + _SECRET_VALUE_PATTERN
)
_SECRET_CLI_FLAG = re.compile(
    r"""(?ix)
    (?P<prefix>^|[\s;&|([{])
    (?P<flag>
        --?[A-Za-z0-9_.-]*
        (?:"""
    + _SECRET_KEY_PATTERN
    + r""")
        [A-Za-z0-9_.-]*
    )
    (?P<separator>\s+|=)
    """
    + _SECRET_VALUE_PATTERN
    + r"""
    """
)
_SECRET_LABEL = re.compile(
    r"(?i)\b("
    r"access[-_ ]?key(?:[-_ ]?(?:id|secret))?|"
    r"x[-_ ]?api[-_ ]?key|api[-_ ]?key|"
    r"secret|signature|token|password|authorization|sts[-_ ]?token|credential|cookie|private[-_ ]?key"
    r")\b"
)
_PEM_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class PermissionAuditRecord:
    """A single sanitized permission audit event."""

    session_id: str
    tool_name: str
    tool_use_id: str
    decision: Literal["allow", "deny"]
    scope: str
    source: str
    rule_source: str | None = None
    reason_type: str | None = None
    reason_detail: str | None = None
    trigger_reason_type: str | None = None
    rule: str | None = None
    rule_fingerprint: str | None = None
    operation: dict[str, Any] = field(default_factory=dict)
    input_summary: dict[str, Any] = field(default_factory=dict)
    tool_input_redacted: dict[str, Any] | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def fingerprint_text(value: str) -> str:
    """Return a short stable fingerprint for sensitive text."""

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _is_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and bool(_FINGERPRINT.fullmatch(value))


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _is_redacted_shape(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value == {"redacted": True}:
        return True
    value_type = value.get("type")
    if not isinstance(value_type, str):
        return False
    keys = set(value)
    if value_type == "str":
        return (
            keys == {"type", "length", "fingerprint"}
            and isinstance(value.get("length"), int)
            and _is_fingerprint(value.get("fingerprint"))
        )
    if value_type == "array":
        return keys == {"type", "length"} and isinstance(value.get("length"), int)
    if value_type == "object":
        return keys == {"type", "truncated"} and value.get("truncated") is True
    if value_type == "null":
        return keys == {"type"}
    return keys == {"type"} and value_type in {"bool", "int", "float", "complex", "bytes"}


def _truncated_object_shape() -> dict[str, Any]:
    return {"type": "object", "truncated": True}


def _redacted_scalar_shape(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"type": "str", "length": len(value), "fingerprint": fingerprint_text(value)}
    if isinstance(value, list | tuple):
        return {"type": "array", "length": len(value)}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__}


def redact_value(
    key: str,
    value: Any,
    *,
    _depth: int = 0,
    _budget: dict[str, int] | None = None,
) -> Any:
    """Redact sensitive values while preserving non-sensitive container shape."""

    if _budget is None:
        _budget = {"nodes": _REDACTED_MAX_NODES}
    if _budget["nodes"] <= 0:
        return _truncated_object_shape() if isinstance(value, dict) else _redacted_scalar_shape(value)
    _budget["nodes"] -= 1

    if _is_secret_key(key):
        return {"redacted": True}
    if _is_redacted_shape(value):
        return value
    if isinstance(value, dict):
        if _depth >= _REDACTED_MAX_DEPTH:
            return _truncated_object_shape()
        redacted: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= _REDACTED_MAX_FIELDS or _budget["nodes"] <= 0:
                redacted[_TRUNCATED_FIELD] = _truncated_object_shape()
                break
            redacted[_safe_field_name(child_key)] = redact_value(
                str(child_key),
                child_value,
                _depth=_depth + 1,
                _budget=_budget,
            )
        return redacted
    return _redacted_scalar_shape(value)


def _display_value(
    key: str,
    value: Any,
    *,
    text_sanitizer=None,
    _depth: int = 0,
    _budget: dict[str, int] | None = None,
    _seen: set[int] | None = None,
) -> Any:
    if text_sanitizer is None:
        text_sanitizer = sanitize_free_text
    if _budget is None:
        _budget = _shape_budget()
    if _seen is None:
        _seen = set()
    if _budget["nodes"] <= 0:
        return _truncated_object_shape() if isinstance(value, dict | list | tuple) else value
    _budget["nodes"] -= 1
    if _is_secret_key(key):
        return {"redacted": True}
    if isinstance(value, dict):
        if _depth >= _REDACTED_MAX_DEPTH or id(value) in _seen:
            return _truncated_object_shape()
        _seen.add(id(value))
        try:
            displayed: dict[str, Any] = {}
            for index, (child_key, child_value) in enumerate(_display_ordered_items(value)):
                if index >= _REDACTED_MAX_FIELDS or _budget["nodes"] <= 0:
                    displayed[_TRUNCATED_FIELD] = _truncated_object_shape()
                    break
                displayed[_display_field_name(child_key)] = _display_value(
                    str(child_key),
                    child_value,
                    text_sanitizer=text_sanitizer,
                    _depth=_depth + 1,
                    _budget=_budget,
                    _seen=_seen,
                )
            return displayed
        finally:
            _seen.remove(id(value))
    if isinstance(value, list | tuple):
        if _depth >= _REDACTED_MAX_DEPTH or id(value) in _seen:
            return _truncated_object_shape()
        _seen.add(id(value))
        try:
            displayed_items: list[Any] = []
            for index, item in enumerate(value):
                if index >= _REDACTED_MAX_FIELDS or _budget["nodes"] <= 0:
                    displayed_items.append(_truncated_object_shape())
                    break
                displayed_items.append(
                    _display_value(
                        key,
                        item,
                        text_sanitizer=text_sanitizer,
                        _depth=_depth + 1,
                        _budget=_budget,
                        _seen=_seen,
                    )
                )
            return displayed_items
        finally:
            _seen.remove(id(value))
    if isinstance(value, str):
        return _display_text(value, text_sanitizer=text_sanitizer)
    return value


def _display_ordered_items(value: dict[Any, Any]) -> list[tuple[Any, Any]]:
    items: list[tuple[Any, Any]] = []
    emitted: set[Any] = set()
    for key in _DISPLAY_PRIORITY_FIELD_NAMES:
        if key in value:
            items.append((key, value[key]))
            emitted.add(key)
    for item in value.items():
        if item[0] not in emitted:
            items.append(item)
    return items


def _display_text(text: str, *, text_sanitizer=None) -> str | dict[str, Any] | None:
    if text_sanitizer is None:
        text_sanitizer = sanitize_free_text
    if len(text) <= _DISPLAY_TEXT_EDGE_CHARS:
        return text_sanitizer(text)
    sanitized = text_sanitizer(text, max_chars=max(len(text), _DISPLAY_TEXT_EDGE_CHARS * 2))
    if sanitized is None:
        return None
    return {
        "type": "str",
        "length": len(text),
        "prefix": sanitized[:_DISPLAY_TEXT_EDGE_CHARS],
        "suffix": sanitized[-_DISPLAY_TEXT_EDGE_CHARS:],
        "truncated": True,
    }


def build_display_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return tool input sanitized for permission prompts, preserving decision-critical values."""

    display_value = _display_value("tool_input", tool_input)
    return display_value if isinstance(display_value, dict) else {}


def build_prompt_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return protocol-safe prompt input while preserving critical decision fields."""

    redacted = build_redacted_tool_input(tool_input)
    prompt: dict[str, Any] = {}
    for key in _DISPLAY_PRIORITY_FIELD_NAMES:
        if key not in _PROMPT_RAW_FIELD_NAMES or key not in tool_input or _is_secret_key(key):
            continue
        prompt[key] = _display_value(key, tool_input[key], text_sanitizer=sanitize_prompt_text)
    for key, value in redacted.items():
        if key in prompt:
            continue
        if len(prompt) >= _REDACTED_MAX_FIELDS and _TRUNCATED_FIELD not in prompt:
            prompt[_TRUNCATED_FIELD] = _truncated_object_shape()
            break
        prompt[key] = value
    return prompt


def build_redacted_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return tool input with sensitive values redacted."""

    redacted = redact_value("tool_input", tool_input)
    return redacted if isinstance(redacted, dict) else {}


def redacted_tool_input_for_settings(
    tool_input: dict[str, Any],
    settings: PermissionAuditSettings | None,
) -> dict[str, Any] | None:
    """Return redacted tool input only when audit settings opt into it."""

    if settings is None or not settings.include_tool_input:
        return None
    return build_redacted_tool_input(tool_input)


def sanitize_free_text(text: str | None, *, max_chars: int = _TEXT_MAX_CHARS) -> str | None:
    """Redact obvious secrets from free text and cap its length."""

    if text is None:
        return None
    sanitized = _PEM_PRIVATE_KEY_BLOCK.sub("[REDACTED]", text)
    sanitized = sanitize_public_text(sanitized)
    sanitized = re.sub(r"(?i)\bbearer\s+\S+", "bearer [REDACTED]", sanitized)
    sanitized = _QUOTED_SECRET_ASSIGNMENT.sub(_redact_quoted_secret_assignment, sanitized)
    sanitized = _SECRET_ASSIGNMENT.sub(_redact_secret_assignment, sanitized)
    sanitized = _SECRET_LABEL.sub("[REDACTED]", sanitized)
    return sanitized[:max_chars]


def sanitize_prompt_text(text: str | None, *, max_chars: int = _TEXT_MAX_CHARS) -> str | None:
    """Redact secrets for permission prompts while preserving path context."""

    if text is None:
        return None
    sanitized = _PEM_PRIVATE_KEY_BLOCK.sub("[REDACTED]", text)
    sanitized = re.sub(r"(?i)\bbearer\s+\S+", "bearer [REDACTED]", sanitized)
    sanitized = _SECRET_CLI_FLAG.sub(_redact_secret_cli_flag, sanitized)
    sanitized = _QUOTED_SECRET_ASSIGNMENT.sub(_redact_quoted_secret_assignment, sanitized)
    sanitized = _SECRET_ASSIGNMENT.sub(_redact_secret_assignment, sanitized)
    return sanitized[:max_chars]


def _redact_secret_assignment(match: re.Match[str]) -> str:
    return f"{match.group('key')}{match.group('separator')}[REDACTED]"


def _redact_quoted_secret_assignment(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{quote}{match.group('key')}{quote}{match.group('separator')}[REDACTED]"


def _redact_secret_cli_flag(match: re.Match[str]) -> str:
    flag = match.group("flag")
    separator = match.group("separator")
    if separator.strip() == "" and not flag.startswith("-"):
        return match.group(0)
    return f"{match.group('prefix')}{flag}{separator}[REDACTED]"


def build_input_summary(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Build a non-secret summary of tool input."""

    if tool_name == "aliyun_api":
        return _build_aliyun_api_summary(tool_input)
    fields, truncated = _field_value_shapes(tool_input, _depth=0, _budget=_shape_budget())
    summary: dict[str, Any] = {
        "tool_name": _safe_summary_text("tool_name", tool_name),
        "fields": fields,
    }
    if truncated:
        summary["fields_truncated"] = True
    return {key: value for key, value in summary.items() if value is not None}


def is_permission_audit_non_read_only(event: Any) -> bool:
    """Return whether a permission event metadata represents a non-read-only operation."""

    metadata = _permission_audit_metadata(event)
    return metadata is None or getattr(metadata, "is_read_only", None) is not True


def is_aliyun_api_non_read_only_metadata(tool_name: str, metadata: Any | None) -> bool:
    """Return whether metadata represents a non-read-only Aliyun API operation."""

    return tool_name == "aliyun_api" and (metadata is None or getattr(metadata, "is_read_only", None) is not True)


def is_aliyun_api_non_read_only_permission_event(event: Any) -> bool:
    """Return whether a permission event is an unresolved non-read-only Aliyun API operation."""

    return is_aliyun_api_non_read_only_metadata(str(getattr(event, "tool_name", "")), _permission_audit_metadata(event))


def should_fail_closed_permission_audit(event: Any, decision: Literal["allow", "deny"]) -> bool:
    """Return whether an audit failure should prevent the permission decision."""

    return decision == "allow"


def is_routine_read_only_allow(decision: Literal["allow", "deny"], metadata: Any | None) -> bool:
    """Return True for automatic read-only allow decisions that do not need audit rows."""

    return (
        decision == "allow"
        and metadata is not None
        and getattr(metadata, "is_read_only", None) is True
        and getattr(metadata, "scope", None) == "read_only"
    )


def emit_permission_boundary_audit(
    event: Any,
    *,
    session_id: str = "",
    decision: Literal["allow", "deny"],
    scope: str,
    source: str,
    rule_source: str | None = None,
    reason_type: str | None = None,
    reason_detail: str | None = None,
    rule: str | None = None,
) -> bool:
    """Emit a prompt/cache boundary audit record for a permission request event.

    Prompt/cache boundaries represent explicit user or cached policy decisions,
    so read-only events are emitted too; only automatic read-only allows are
    skipped by no-prompt audit callers.
    """

    metadata = _permission_audit_metadata(event)
    settings = _permission_audit_settings(event)
    result = emit_permission_audit(
        PermissionAuditRecord(
            session_id=_permission_audit_session_id(event, fallback=session_id),
            tool_name=event.tool_name,
            tool_use_id=event.tool_use_id,
            decision=decision,
            scope=scope,
            source=source,
            rule_source=_boundary_rule_source(scope, rule_source=rule_source, metadata=metadata),
            reason_type=reason_type if reason_type is not None else getattr(metadata, "reason_type", None),
            reason_detail=reason_detail if reason_detail is not None else getattr(metadata, "reason_detail", None),
            trigger_reason_type=_boundary_trigger_reason_type(reason_type=reason_type, metadata=metadata),
            rule=rule if rule is not None else getattr(metadata, "rule", None),
            operation=permission_audit_operation(metadata),
            input_summary=build_input_summary(event.tool_name, event.tool_input),
            tool_input_redacted=redacted_tool_input_for_settings(event.tool_input, settings),
        ),
        settings=settings,
    )
    return result is not False


def _boundary_rule_source(scope: str, *, rule_source: str | None, metadata: Any | None) -> str | None:
    if rule_source is not None:
        return rule_source
    if scope == "session_rule":
        return "session"
    if scope == "tool_cache":
        return "tool_cache"
    return getattr(metadata, "rule_source", None)


def _boundary_trigger_reason_type(*, reason_type: str | None, metadata: Any | None) -> str | None:
    metadata_reason_type = getattr(metadata, "reason_type", None)
    if reason_type is not None and reason_type != metadata_reason_type and isinstance(metadata_reason_type, str):
        return metadata_reason_type
    return None


def emit_auto_permission_audit(
    event: Any,
    *,
    decision: Literal["allow", "deny"],
    scope: str,
    source: str,
    session_id: str = "",
) -> bool:
    """Emit an automatic boundary decision while preserving permission metadata."""

    metadata = _permission_audit_metadata(event)
    settings = _permission_audit_settings(event)
    result = emit_permission_audit(
        PermissionAuditRecord(
            session_id=_permission_audit_session_id(event, fallback=session_id),
            tool_name=event.tool_name,
            tool_use_id=event.tool_use_id,
            decision=decision,
            scope=scope,
            source=source,
            rule_source=getattr(metadata, "rule_source", None),
            reason_type=getattr(metadata, "reason_type", None) or source,
            reason_detail=getattr(metadata, "reason_detail", None),
            rule=getattr(metadata, "rule", None),
            operation=permission_audit_operation(metadata),
            input_summary=build_input_summary(event.tool_name, event.tool_input),
            tool_input_redacted=redacted_tool_input_for_settings(event.tool_input, settings),
        ),
        settings=settings,
    )
    return result is not False


def _permission_audit_context(event: Any) -> dict[str, Any]:
    audit_context = getattr(event, "audit_context", None) or {}
    return audit_context if isinstance(audit_context, dict) else {}


def _permission_audit_metadata(event: Any) -> Any | None:
    return _permission_audit_context(event).get("metadata")


def permission_audit_operation(metadata: Any | None) -> dict[str, Any]:
    """Return sanitized-ready operation metadata with read/write classification folded in."""

    operation = dict(getattr(metadata, "operation", {}) or {})
    is_read_only = getattr(metadata, "is_read_only", None)
    if isinstance(is_read_only, bool):
        operation.setdefault("is_read_only", is_read_only)
    return operation


def _permission_audit_settings(event: Any) -> PermissionAuditSettings | None:
    settings = _permission_audit_context(event).get("settings")
    return settings if isinstance(settings, PermissionAuditSettings) else None


def _permission_audit_session_id(event: Any, *, fallback: str) -> str:
    session_id = _permission_audit_context(event).get("session_id")
    return session_id if isinstance(session_id, str) else fallback


def emit_permission_audit(record: PermissionAuditRecord, settings: PermissionAuditSettings | None = None) -> bool:
    """Write a permission audit row and emit a telemetry event."""

    audit_settings = settings or PermissionAuditSettings()
    max_files = _bounded_max_files(audit_settings.max_files)
    row = _audit_row(record, include_tool_input=audit_settings.include_tool_input)
    log_written = False

    try:
        log_path = _log_path()
        _ensure_existing_audit_log_files_private(log_path, max_files=max_files)
        append_jsonl_rotating_locked(
            log_path,
            [row],
            max_file_bytes=audit_settings.max_file_bytes,
            max_files=max_files,
            durable=True,
            create_mode=0o600,
        )
        _ensure_audit_log_files_private(log_path, max_files=max_files)
        log_written = True
    except Exception as exc:  # pragma: no cover - defensive logging only
        logger.warning("Failed to write permission audit log: {}", exc)

    if log_written or record.decision == "deny":
        try:
            log_event(_event_name(record), _telemetry_metadata(record))
        except Exception as exc:  # pragma: no cover - defensive logging only
            logger.warning("Failed to emit permission audit telemetry: {}", exc)

    return log_written


def _bounded_max_files(max_files: int) -> int:
    if max_files < 1:
        return 1
    return min(max_files, MAX_PERMISSION_AUDIT_FILES)


def _audit_row(record: PermissionAuditRecord, *, include_tool_input: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {
        "session_id": record.session_id,
        "tool_name": record.tool_name,
        "tool_use_id": record.tool_use_id,
        "decision": record.decision,
        "scope": record.scope,
        "source": record.source,
        "rule_source": _safe_reason_token(record.rule_source),
        "reason_type": _safe_reason_token(record.reason_type),
        "reason_detail": _safe_reason_detail(record),
        "operation": _sanitize_operation_metadata(record.operation),
        "input_summary": _sanitize_input_summary(record.input_summary),
        "timestamp": record.timestamp,
    }
    safe_trigger_reason_type = _safe_reason_token(record.trigger_reason_type)
    if safe_trigger_reason_type is not None:
        row["trigger_reason_type"] = safe_trigger_reason_type
    else:
        row.pop("trigger_reason_type", None)
    rule_fingerprint = _safe_rule_fingerprint(record)
    if rule_fingerprint is not None:
        row["rule_fingerprint"] = rule_fingerprint
    else:
        row.pop("rule_fingerprint", None)
    safe_rule = _safe_rule_text(record.rule)
    if safe_rule is not None:
        row["rule"] = safe_rule
    else:
        row.pop("rule", None)
    if include_tool_input and record.tool_input_redacted is not None:
        row["tool_input_redacted"] = redact_value("tool_input", record.tool_input_redacted)
    else:
        row.pop("tool_input_redacted", None)
    return {key: value for key, value in row.items() if value is not None}


def _safe_reason_detail(record: PermissionAuditRecord) -> str | None:
    if record.reason_type == "rule":
        return "matched permission rule"
    return sanitize_free_text(record.reason_detail)


def _safe_rule_text(rule: str | None) -> str | None:
    if rule is None:
        return None
    if _SAFE_RULE_TEXT.fullmatch(rule):
        return rule
    if ", " in rule:
        parts = rule.split(", ")
        if all(_safe_rule_text(part) == part for part in parts):
            return rule
    wrapped = _SAFE_WRAPPED_RULE_TEXT.fullmatch(rule)
    if wrapped is not None:
        return rule
    return None


def _sanitize_operation_metadata(operation: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key in _OPERATION_ID_KEYS:
        value = operation.get(key)
        if isinstance(value, str):
            if _SAFE_ID.fullmatch(value):
                sanitized[key] = value
            else:
                sanitized[f"{key}_fingerprint"] = fingerprint_text(value)

    for key in _OPERATION_FINGERPRINT_KEYS:
        value = operation.get(key)
        if _is_fingerprint(value):
            sanitized[key] = value

    is_read_only = operation.get("is_read_only")
    if isinstance(is_read_only, bool):
        sanitized["is_read_only"] = is_read_only

    return sanitized


def _telemetry_metadata(record: PermissionAuditRecord) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tool_name": record.tool_name,
        "decision": record.decision,
        "scope": record.scope,
        "source": record.source,
    }
    for key in ("rule_source", "reason_type"):
        value = getattr(record, key)
        safe_value = _safe_reason_token(value)
        if safe_value is not None:
            metadata[key] = safe_value

    trigger_reason_type = _safe_reason_token(record.trigger_reason_type)
    if trigger_reason_type is not None:
        metadata["trigger_reason_type"] = trigger_reason_type

    for key in ("product", "action", "region"):
        value = record.operation.get(key)
        if isinstance(value, str):
            if _SAFE_ID.fullmatch(value):
                metadata[key] = value
            else:
                metadata[f"{key}_fingerprint"] = fingerprint_text(value)

    for key in ("product_fingerprint", "action_fingerprint", "region_fingerprint"):
        value = record.operation.get(key)
        if _is_fingerprint(value):
            metadata[key] = value

    is_read_only = record.operation.get("is_read_only")
    if isinstance(is_read_only, bool):
        metadata["is_read_only"] = is_read_only

    rule_fingerprint = _safe_rule_fingerprint(record)
    if rule_fingerprint is not None:
        metadata["rule_fingerprint"] = rule_fingerprint

    return metadata


def _safe_rule_fingerprint(record: PermissionAuditRecord) -> str | None:
    if _is_fingerprint(record.rule_fingerprint):
        return record.rule_fingerprint
    if record.rule:
        return fingerprint_text(record.rule)
    return None


def _build_aliyun_api_summary(tool_input: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"tool_name": "aliyun_api"}
    for input_key, output_key in (
        ("product", "product"),
        ("action", "action"),
        ("region_id", "region"),
        ("region", "region"),
        ("style", "style"),
        ("method", "method"),
    ):
        if input_key in tool_input and output_key not in summary:
            _add_safe_or_fingerprint(summary, output_key, tool_input[input_key])

    pathname = tool_input.get("pathname")
    if isinstance(pathname, str):
        summary["pathname_shape"] = _pathname_shape(pathname)
        summary["pathname_fingerprint"] = fingerprint_text(pathname)

    for key in ("params", "body"):
        value = tool_input.get(key)
        if isinstance(value, dict):
            field_shapes, truncated = _field_value_shapes(
                value,
                _depth=0,
                _budget=_shape_budget(),
                fingerprint_keys=True,
            )
            summary[f"{key}_field_count"] = len(value)
            summary[f"{key}_fields"] = sorted(field_shapes)
            summary[f"{key}_field_shapes"] = field_shapes
            if truncated:
                summary[f"{key}_fields_truncated"] = True
        elif value is not None:
            summary[f"{key}_shape"] = _value_shape(value, _budget=_shape_budget())

    return summary


def _add_safe_or_fingerprint(summary: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, str) and _SAFE_ID.fullmatch(value):
        summary[key] = value
    elif value is not None:
        summary[f"{key}_fingerprint"] = fingerprint_text(str(value))


def _pathname_shape(pathname: str) -> str:
    parts = [part for part in pathname.split("/") if part]
    return "/" + "/".join("{segment}" for _part in parts)


def _shape_budget() -> dict[str, int]:
    return {"nodes": _REDACTED_MAX_NODES}


def _field_value_shapes(
    value: dict[Any, Any],
    *,
    _depth: int,
    _budget: dict[str, int],
    fingerprint_keys: bool = False,
) -> tuple[dict[str, Any], bool]:
    fields: dict[str, Any] = {}
    truncated = False
    for index, (field_name, field_value) in enumerate(value.items()):
        if index >= _REDACTED_MAX_FIELDS or _budget["nodes"] <= 0:
            truncated = True
            break
        safe_name = fingerprint_text(str(field_name)) if fingerprint_keys else _safe_field_name(field_name)
        fields[safe_name] = _value_shape(
            field_value,
            _depth=_depth + 1,
            _budget=_budget,
            fingerprint_keys=fingerprint_keys,
        )
    return fields, truncated


def _summary_scalar_shape(value: Any) -> dict[str, Any]:
    if isinstance(value, list | tuple):
        return {"type": "array", "length": len(value)}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__}


def _value_shape(
    value: Any,
    *,
    _depth: int = 0,
    _budget: dict[str, int] | None = None,
    fingerprint_keys: bool = False,
) -> dict[str, Any]:
    if _budget is None:
        _budget = _shape_budget()
    if _budget["nodes"] <= 0:
        return _truncated_object_shape() if isinstance(value, dict) else _summary_scalar_shape(value)
    _budget["nodes"] -= 1
    if isinstance(value, dict):
        if _depth >= _REDACTED_MAX_DEPTH:
            return _truncated_object_shape()
        fields, truncated = _field_value_shapes(
            value,
            _depth=_depth,
            _budget=_budget,
            fingerprint_keys=fingerprint_keys,
        )
        shape: dict[str, Any] = {"type": "object", "field_count": len(value)}
        if fields:
            shape["fields"] = fields
        if truncated:
            shape[_TRUNCATED_FIELD] = _truncated_object_shape()
        return shape
    if isinstance(value, list | tuple):
        return {"type": "array", "length": len(value)}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__}


def _sanitize_input_summary(summary: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_summary_value("input_summary", summary, _budget=_shape_budget())
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_summary_value(
    key: str,
    value: Any,
    *,
    _depth: int = 0,
    _budget: dict[str, int],
) -> Any:
    if _budget["nodes"] <= 0:
        return _truncated_object_shape() if isinstance(value, dict) else _summary_scalar_shape(value)
    _budget["nodes"] -= 1
    if isinstance(value, dict):
        if _depth >= _REDACTED_MAX_DEPTH:
            return _truncated_object_shape()
        sanitized: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= _REDACTED_MAX_FIELDS or _budget["nodes"] <= 0:
                sanitized[_TRUNCATED_FIELD] = _truncated_object_shape()
                break
            safe_child_key = _safe_summary_child_key(key, child_key)
            sanitized[safe_child_key] = _sanitize_summary_value(
                str(child_key),
                child_value,
                _depth=_depth + 1,
                _budget=_budget,
            )
        return sanitized
    if isinstance(value, list | tuple):
        sanitized_items: list[Any] = []
        for index, item in enumerate(value):
            if index >= _REDACTED_MAX_FIELDS or _budget["nodes"] <= 0:
                sanitized_items.append(_truncated_object_shape())
                break
            sanitized_items.append(
                _sanitize_summary_value(
                    key,
                    item,
                    _depth=_depth + 1,
                    _budget=_budget,
                )
            )
        return sanitized_items
    if isinstance(value, str):
        return _safe_summary_text(key, value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in {float("inf"), float("-inf")} else {"type": "float"}
    return {"type": type(value).__name__}


def _safe_summary_text(key: str, value: str) -> str:
    if _is_fingerprint(value):
        return value
    if key.endswith("_fields") or key.endswith("_fingerprint"):
        return fingerprint_text(value)
    if key == "pathname_shape" and _SAFE_PATHNAME_SHAPE.fullmatch(value):
        return value
    if key in {"tool_name", "product", "action", "region", "style", "method", "type"} and _SAFE_ID.fullmatch(value):
        return value
    return fingerprint_text(value)


def _safe_summary_child_key(parent_key: str, child_key: Any) -> str:
    text = str(child_key)
    if parent_key.endswith("_field_shapes"):
        return text if _is_fingerprint(text) else fingerprint_text(text)
    return _safe_field_name(child_key)


def _display_field_name(key: Any) -> str:
    text = str(key)
    if _is_fingerprint(text):
        return text
    if _SAFE_ID.fullmatch(text) and not _is_secret_key(text):
        return text
    return fingerprint_text(text)


def _safe_field_name(key: Any) -> str:
    text = str(key)
    if _is_fingerprint(text):
        return text
    if text in _SAFE_SHAPE_FIELD_NAMES and not _is_secret_key(text):
        return text
    return fingerprint_text(text)


def _safe_reason_token(value: str | None) -> str | None:
    if value is None:
        return None
    if _SAFE_ID.fullmatch(value):
        return value
    return fingerprint_text(value)


def _event_name(record: PermissionAuditRecord) -> str:
    if record.source in {"repl_prompt", "acp_prompt"}:
        if record.decision == "allow":
            return Events.TOOL_USE_GRANTED_IN_PROMPT
        return Events.TOOL_USE_REJECTED_IN_PROMPT
    if record.decision == "allow":
        return Events.TOOL_PERMISSION_GRANTED
    return Events.TOOL_PERMISSION_REJECTED


def _log_path() -> Path:
    log_dir = ensure_private_dir(get_config_dir() / "logs")
    return log_dir / "permission-audit.jsonl"


def _ensure_audit_log_files_private(path: Path, *, max_files: int) -> None:
    ensure_private_file(path)
    _assert_private_audit_file(path)
    for index in range(1, max_files + 1):
        rotated = path.with_name(f"{path.name}.{index}")
        ensure_private_file(rotated)
        _assert_private_audit_file(rotated)


def _ensure_existing_audit_log_files_private(path: Path, *, max_files: int) -> None:
    if path.exists():
        ensure_private_file(path)
        _assert_private_audit_file(path)
    for index in range(1, max_files + 1):
        rotated = path.with_name(f"{path.name}.{index}")
        if rotated.exists():
            ensure_private_file(rotated)
            _assert_private_audit_file(rotated)


def _assert_private_audit_file(path: Path) -> None:
    if os.name == "nt" or not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"Permission audit log file is not private: {path}")
