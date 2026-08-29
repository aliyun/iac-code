from __future__ import annotations

import copy
import hashlib
import os
import tempfile
from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

from google.protobuf.json_format import MessageToDict, ParseDict

from iac_code.i18n import _
from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.public_errors import PublicError
from iac_code.utils.public_paths import PublicPathRedactor, build_public_path_roots

IAC_CODE_A2A_SAFE_MODE_ENV = "IAC_CODE_A2A_SAFE_MODE"
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_TASK_SCOPED_JSONRPC_METHODS = {
    "CancelTask",
    "CreateTaskPushNotificationConfig",
    "DeleteTaskPushNotificationConfig",
    "GetTask",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "SubscribeToTask",
    "tasks/cancel",
    "tasks/get",
    "tasks/pushNotificationConfig/delete",
    "tasks/pushNotificationConfig/get",
    "tasks/pushNotificationConfig/list",
    "tasks/pushNotificationConfig/set",
    "tasks/resubscribe",
}
_ProtoMessageT = TypeVar("_ProtoMessageT")


def a2a_safe_mode_enabled() -> bool:
    """Return the process-wide A2A delivery policy at the current boundary."""

    return os.environ.get(IAC_CODE_A2A_SAFE_MODE_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


def build_a2a_public_path_roots(
    *,
    cwd: str,
    session_id: str | None = None,
    additional_directories: Iterable[str] | None = None,
    trusted_read_directories: Iterable[str] | None = None,
    relative_read_directories: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    """Build the shared A2A roots from current, reproducible runtime state."""

    from iac_code.tools.path_safety import get_iac_code_application_root

    additional = [tempfile.gettempdir(), str(get_iac_code_application_root()), *(additional_directories or [])]
    trusted = list(trusted_read_directories or [])
    if session_id:
        session_dir = SessionStorage().session_dir(cwd, session_id)
        additional.append(str(session_dir))
        trusted.extend(build_session_trusted_read_directories(session_id, session_dir=session_dir))
    return build_public_path_roots(
        cwd=cwd,
        additional_directories=additional,
        trusted_read_directories=trusted,
        relative_read_directories=relative_read_directories,
    )


def project_a2a_text(
    value: str,
    *,
    public_path_roots: Iterable[Mapping[str, str]] | None = None,
    safe_mode: bool | None = None,
) -> str:
    """Apply the A2A path-only policy without credential redaction."""

    enabled = a2a_safe_mode_enabled() if safe_mode is None else safe_mode
    if not enabled:
        return value
    return PublicPathRedactor(public_path_roots).redact(value)


def project_a2a_data(
    value: Any,
    *,
    public_path_roots: Iterable[Mapping[str, str]] | None = None,
    safe_mode: bool | None = None,
) -> Any:
    """Return an A2A delivery copy selected by the current safe-mode policy.

    Canonical input is never modified. Safe mode only replaces paths proven to
    be under ``public_path_roots``; it never applies secret-key or credential
    patterns. Mapping-key collisions use deterministic placeholder keys and do
    not discard values.
    """

    enabled = a2a_safe_mode_enabled() if safe_mode is None else safe_mode
    if not enabled:
        return copy.deepcopy(value)
    return _project_path_only(value, redactor=PublicPathRedactor(public_path_roots))


def project_a2a_proto(
    message: _ProtoMessageT,
    *,
    public_path_roots: Iterable[Mapping[str, str]] | None = None,
    safe_mode: bool | None = None,
) -> _ProtoMessageT:
    """Project an A2A protobuf message without changing its protocol schema."""

    data = MessageToDict(message, preserving_proto_field_name=False)
    projected = project_a2a_data(data, public_path_roots=public_path_roots, safe_mode=safe_mode)
    result = type(message)()
    ParseDict(projected, result)
    return result


async def resolve_a2a_public_path_roots(
    task_store: Any,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
    call_context: Any | None = None,
    fallback_cwd: str | None = None,
    request_data: Any = None,
) -> list[dict[str, str]]:
    """Rebuild roots from current task/context state without persisting roots."""

    resolved_context_id = context_id
    if task_id and task_store is not None:
        try:
            get_task_record = getattr(task_store, "get_task_record", None)
            if get_task_record is not None:
                task = await get_task_record(task_id)
            else:
                task = await task_store.get(task_id, context=call_context)
        except (AttributeError, KeyError, RuntimeError, ValueError):
            task = None
        candidate = getattr(task, "context_id", None)
        if isinstance(candidate, str) and candidate:
            resolved_context_id = candidate
    if resolved_context_id and task_store is not None:
        try:
            context = await task_store.get_context_record(resolved_context_id)
        except (AttributeError, KeyError, RuntimeError, ValueError):
            context = None
        cwd = getattr(context, "cwd", None)
        if isinstance(cwd, str) and cwd:
            session_id = getattr(context, "session_id", None)
            additional_directories: list[str] = []
            trusted_read_directories: list[str] = []
            relative_read_directories: list[str] = []
            get_runtime_directories = getattr(task_store, "get_context_runtime_path_directories", None)
            if get_runtime_directories is not None:
                try:
                    runtime_directories = await get_runtime_directories(resolved_context_id)
                except (AttributeError, KeyError, RuntimeError, ValueError):
                    runtime_directories = None
                if isinstance(runtime_directories, tuple) and len(runtime_directories) == 3:
                    additional_directories = list(runtime_directories[0])
                    trusted_read_directories = list(runtime_directories[1])
                    relative_read_directories = list(runtime_directories[2])
            return build_a2a_public_path_roots(
                cwd=cwd,
                session_id=session_id if isinstance(session_id, str) else None,
                additional_directories=additional_directories,
                trusted_read_directories=trusted_read_directories,
                relative_read_directories=relative_read_directories,
            )
    request_cwd, request_session_id = a2a_runtime_path_context_from_data(request_data)
    return build_a2a_public_path_roots(
        cwd=request_cwd or fallback_cwd or os.getcwd(),
        session_id=request_session_id,
    )


async def resolve_a2a_public_path_roots_for_data(
    task_store: Any,
    *,
    response_data: Any = None,
    request_data: Any = None,
    request_bare_id_is_task_id: bool = False,
    call_context: Any | None = None,
    fallback_cwd: str | None = None,
) -> list[dict[str, str]]:
    """Aggregate roots for every task/context represented at a wire boundary."""

    identities = a2a_identities_from_data(response_data)
    identities.extend(
        identity
        for identity in a2a_identities_from_data(
            request_data,
            bare_id_is_task_id=request_bare_id_is_task_id,
        )
        if identity not in identities
    )
    if not identities:
        return await resolve_a2a_public_path_roots(
            task_store,
            call_context=call_context,
            fallback_cwd=fallback_cwd,
            request_data=request_data,
        )

    roots: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for task_id, context_id in identities:
        resolved = await resolve_a2a_public_path_roots(
            task_store,
            task_id=task_id,
            context_id=context_id,
            call_context=call_context,
            fallback_cwd=fallback_cwd,
            request_data=request_data,
        )
        for root in resolved:
            key = (root.get("path", ""), root.get("label", ""))
            if key not in seen:
                roots.append(root)
                seen.add(key)
    return roots


def a2a_runtime_path_context_from_data(value: Any, *, _depth: int = 0) -> tuple[str | None, str | None]:
    """Extract the existing runtime cwd/session metadata from an A2A payload."""

    if _depth >= 16:
        return None, None
    if isinstance(value, Mapping):
        cwd: str | None = None
        session_id: str | None = None
        iac_code = value.get("iac_code")
        if isinstance(iac_code, Mapping):
            candidate_cwd = iac_code.get("cwd")
            if isinstance(candidate_cwd, str) and candidate_cwd:
                cwd = candidate_cwd
            session_id = _first_nonempty_string(
                iac_code,
                ("iacCodeSessionId", "iac_code_session_id", "sessionId", "session_id"),
            )
        if cwd and session_id:
            return cwd, session_id
        for item in value.values():
            nested_cwd, nested_session_id = a2a_runtime_path_context_from_data(item, _depth=_depth + 1)
            cwd = cwd or nested_cwd
            session_id = session_id or nested_session_id
            if cwd and session_id:
                break
        return cwd, session_id
    if isinstance(value, (list, tuple)):
        cwd: str | None = None
        session_id: str | None = None
        for item in value:
            nested_cwd, nested_session_id = a2a_runtime_path_context_from_data(item, _depth=_depth + 1)
            cwd = cwd or nested_cwd
            session_id = session_id or nested_session_id
            if cwd and session_id:
                break
        return cwd, session_id
    return None, None


def a2a_identity_from_data(value: Any, *, _depth: int = 0) -> tuple[str | None, str | None]:
    """Find the first existing A2A task/context identity in request or response data."""

    identities = a2a_identities_from_data(value, _depth=_depth)
    return identities[0] if identities else (None, None)


def a2a_identities_from_data(
    value: Any,
    *,
    bare_id_is_task_id: bool = False,
    _depth: int = 0,
) -> list[tuple[str | None, str | None]]:
    """Find all A2A identities, preserving JSON-RPC request-id semantics."""

    identities: list[tuple[str | None, str | None]] = []

    def add(task_id: str | None, context_id: str | None) -> None:
        identity = (task_id, context_id)
        if (task_id or context_id) and identity not in identities:
            identities.append(identity)

    def visit(item: Any, depth: int, *, allow_bare_id: bool = False) -> None:
        if depth >= 16:
            return
        if isinstance(item, Mapping):
            task_id = _first_nonempty_string(item, ("taskId", "task_id"))
            context_id = _first_nonempty_string(item, ("contextId", "context_id"))
            if task_id is None and context_id is not None:
                task_id = _first_nonempty_string(item, ("id",))
            if task_id is None:
                task_id = _task_id_from_resource_name(item)
            add(task_id, context_id)

            method = item.get("method")
            params = item.get("params")
            if method in _TASK_SCOPED_JSONRPC_METHODS and isinstance(params, Mapping):
                add(_first_nonempty_string(params, ("taskId", "task_id", "id")), None)
            elif allow_bare_id and task_id is None and context_id is None:
                add(_first_nonempty_string(item, ("id",)), None)

            for nested in item.values():
                visit(nested, depth + 1)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested, depth + 1)

    visit(value, _depth, allow_bare_id=bare_id_is_task_id)
    return identities


def _first_nonempty_string(value: Mapping[Any, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _task_id_from_resource_name(value: Mapping[Any, Any]) -> str | None:
    for key in ("name", "parent"):
        resource_name = value.get(key)
        if not isinstance(resource_name, str):
            continue
        parts = resource_name.split("/")
        if len(parts) >= 2 and parts[0] == "tasks" and parts[1]:
            return parts[1]
    return None


async def project_a2a_exception(
    exc: BaseException,
    *,
    task_store: Any,
    request_data: Any = None,
    fallback_cwd: str | None = None,
) -> PublicError:
    """Build the existing public-error shape with A2A path-only projection."""

    roots = await resolve_a2a_public_path_roots_for_data(
        task_store,
        fallback_cwd=fallback_cwd,
        request_data=request_data,
    )
    error_type = type(exc).__name__
    message = str(exc)
    raw_summary = type(exc).__name__ if not message else f"{type(exc).__name__}: {message}"
    digest = hashlib.sha256(f"{error_type}\0{raw_summary}".encode("utf-8", errors="replace")).hexdigest()
    error_id = digest[:12]
    raw_details = {
        "type": error_type,
        "error_id": error_id,
        "traceback": _("Stack trace omitted from public event; see error_id."),
    }
    details = project_a2a_data(raw_details, public_path_roots=roots)
    return PublicError(
        summary=project_a2a_text(raw_summary, public_path_roots=roots),
        details=details,
        error_id=error_id,
    )


def _project_path_only(value: Any, *, redactor: PublicPathRedactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, Mapping):
        return _project_mapping(value, redactor=redactor)
    if isinstance(value, list):
        return [_project_path_only(item, redactor=redactor) for item in value]
    if isinstance(value, tuple):
        return [_project_path_only(item, redactor=redactor) for item in value]
    return copy.deepcopy(value)


def _project_mapping(value: Mapping[Any, Any], *, redactor: PublicPathRedactor) -> dict[Any, Any]:
    redacted_keys = {key: redactor.redact(key) for key in value if isinstance(key, str)}
    unchanged_keys = {key for key in value if not isinstance(key, str) or redacted_keys[key] == key}
    used_keys: set[Any] = set()
    projected: dict[Any, Any] = {}
    next_path_index = 1

    for key, item in value.items():
        output_key: Any = key
        if isinstance(key, str) and redacted_keys[key] != key:
            while True:
                candidate = "[PATH]" if next_path_index == 1 else f"[PATH#{next_path_index}]"
                next_path_index += 1
                if candidate not in unchanged_keys and candidate not in used_keys:
                    output_key = candidate
                    break
        used_keys.add(output_key)
        projected[output_key] = _project_path_only(item, redactor=redactor)
    return projected
