"""Shared read path safety checks for tool access."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

from iac_code.types.permissions import PermissionDecisionReason, PermissionResult
from iac_code.utils.platform import normalize_user_path

_BASE_SENSITIVE_PATHS = [
    ".git/",
    ".git",
    ".iac-code/",
    ".iac-code",
    ".iac-code/.credentials.yml",
    ".iac-code/.cloud-credentials.yml",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".ssh/",
    ".ssh",
    ".env",
    ".aliyun/",
    ".aliyun",
    ".alibabacloud/",
    ".alibabacloud",
    ".aws/credentials",
]

_WINDOWS_SENSITIVE_PATHS = [
    "AppData/Roaming/Microsoft/Windows/PowerShell",
    "AppData/Local/Microsoft/Credentials",
    "ntuser.dat",
]


class _SensitivePaths(Sequence[str]):
    """Platform-sensitive sequence used by bash safety checks and read checks."""

    def _paths(self) -> list[str]:
        paths = list(_BASE_SENSITIVE_PATHS)
        if sys.platform == "win32":
            paths.extend(_WINDOWS_SENSITIVE_PATHS)
        return paths

    def __contains__(self, value: object) -> bool:
        return value in self._paths()

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[str]: ...

    def __getitem__(self, index: int | slice) -> str | Sequence[str]:
        return self._paths()[index]

    def __iter__(self) -> Iterator[str]:
        return iter(self._paths())

    def __len__(self) -> int:
        return len(self._paths())

    def __repr__(self) -> str:
        return repr(self._paths())


SENSITIVE_PATHS: Sequence[str] = _SensitivePaths()


def _normalize_for_platform(path: str, *, case_insensitive: bool | None = None) -> str:
    normalized = path.replace("\\", "/")
    if case_insensitive is None:
        case_insensitive = sys.platform == "win32"
    if case_insensitive:
        return normalized.casefold()
    return normalized


@dataclass(frozen=True)
class ReadPathDecision:
    """Read path permission decision."""

    behavior: Literal["allow", "ask"]
    reason_type: str = ""
    detail: str = ""

    def to_permission_result(self) -> PermissionResult:
        if self.behavior == "allow":
            return PermissionResult(behavior="passthrough")
        return PermissionResult(
            behavior="ask",
            message=self.detail,
            reason=PermissionDecisionReason(type=self.reason_type, detail=self.detail),
        )


def _build_sensitive_lookups(
    sensitive_paths: Sequence[str] = SENSITIVE_PATHS,
    *,
    case_insensitive: bool | None = None,
) -> tuple[frozenset[str], tuple[str, ...]]:
    single: set[str] = set()
    multi: list[str] = []
    for entry in sensitive_paths:
        cleaned = _normalize_for_platform(entry.rstrip("/"), case_insensitive=case_insensitive)
        if not cleaned:
            continue
        if "/" in cleaned:
            multi.append(cleaned)
        else:
            single.add(cleaned)
    return frozenset(single), tuple(multi)


def _path_hits_sensitive(
    abs_norm: str,
    sensitive_paths: Sequence[str] = SENSITIVE_PATHS,
    *,
    case_insensitive: bool | None = None,
) -> bool:
    """Return True when a normalized absolute path touches a sensitive path."""
    if case_insensitive is None:
        case_insensitive = sys.platform in {"win32", "darwin"}
    sensitive_single, sensitive_multi = _build_sensitive_lookups(
        sensitive_paths,
        case_insensitive=case_insensitive,
    )
    normalized = _normalize_for_platform(abs_norm, case_insensitive=case_insensitive)
    parts = normalized.split("/")
    if any(part in sensitive_single for part in parts):
        return True
    return any(sub in normalized for sub in sensitive_multi)


def is_sensitive_path(path: str) -> bool:
    """Return True when a path matches a sensitive path pattern."""
    return _path_hits_sensitive(resolve_candidate(path, os.getcwd()))


def resolve_candidate(path: str, cwd: str) -> str:
    """Resolve a user-supplied path relative to cwd for safety checks."""
    normalized_path = os.path.expanduser(normalize_user_path(path))
    if os.path.isabs(normalized_path):
        return os.path.realpath(normalized_path)
    return os.path.realpath(os.path.join(cwd, normalized_path))


def get_iac_code_application_root() -> Path:
    """Return the installed iac_code package root."""
    return Path(__file__).resolve().parents[1]


def _path_is_under(path: str, root: str) -> bool:
    root_real_raw = os.path.realpath(root)
    case_insensitive = _should_casefold_for_under_check(root_real_raw)
    path_r = _normalize_for_platform(os.path.realpath(path), case_insensitive=case_insensitive)
    root_r = _normalize_for_platform(root_real_raw, case_insensitive=case_insensitive)
    if path_r == root_r:
        return True
    return path_r.startswith(root_r.rstrip("/") + "/")


def _should_casefold_for_under_check(root: str) -> bool:
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return not _path_case_sensitive(root)
    return False


def _path_case_sensitive(root: str) -> bool:
    probe_dir = _existing_probe_dir(root)
    if probe_dir is None:
        return True
    try:
        fd, probe_path = tempfile.mkstemp(prefix=".iac-code-case-", dir=probe_dir)
    except OSError:
        return True
    os.close(fd)
    alternate = os.path.join(probe_dir, os.path.basename(probe_path).swapcase())
    try:
        return not os.path.exists(alternate)
    finally:
        try:
            os.unlink(probe_path)
        except OSError:
            pass


def _existing_probe_dir(path: str) -> str | None:
    candidate = path if os.path.isdir(path) else os.path.dirname(path)
    while candidate and not os.path.isdir(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        candidate = parent
    return candidate or None


def _is_in_allowed_roots(path: str, roots: list[str]) -> bool:
    return any(_path_is_under(path, root) for root in roots if root)


def check_read_path(
    path: str,
    *,
    cwd: str,
    additional_directories: list[str],
    trusted_read_directories: list[str],
) -> ReadPathDecision:
    """Decide whether a read path is safe or should ask for confirmation."""
    resolved = resolve_candidate(path, cwd)

    if _is_in_allowed_roots(resolved, trusted_read_directories):
        return ReadPathDecision("allow")

    if _path_hits_sensitive(resolved):
        return ReadPathDecision("ask", reason_type="safety_check", detail="read touches a sensitive path")

    allowed_roots = [
        cwd,
        *additional_directories,
        str(get_iac_code_application_root()),
    ]
    if _is_in_allowed_roots(resolved, allowed_roots):
        return ReadPathDecision("allow")

    return ReadPathDecision("ask", reason_type="path_constraint", detail="path outside allowed directories")


def check_write_path(
    path: str,
    *,
    cwd: str,
    additional_directories: list[str],
) -> ReadPathDecision:
    """Decide whether a write path is safe or should ask for confirmation."""
    resolved = resolve_candidate(path, cwd)

    if _path_hits_sensitive(resolved):
        return ReadPathDecision("ask", reason_type="safety_check", detail="write touches a sensitive path")

    allowed_roots = [
        cwd,
        *additional_directories,
    ]
    if _is_in_allowed_roots(resolved, allowed_roots):
        return ReadPathDecision("allow")

    return ReadPathDecision("ask", reason_type="path_constraint", detail="path outside allowed directories")
