"""Public path rendering helpers for external event payloads."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_CONNECTOR_TOKEN_END_PATTERN = r"""\s+(?:and|at|because|for|from|in|on|to|with)\b(?=\s+[A-Za-z0-9_.-]+\s*[:=])"""
_PATH_END_PATTERN = r"""(?=""" + _CONNECTOR_TOKEN_END_PATTERN + r"""|$|[\r\n,;:)"'])"""
_POSIX_PATH_TEXT_PATTERN = re.compile(r"""(?<![A-Za-z0-9._~%:/\]-])/(?!/)[^\r\n,;:)\"']*?""" + _PATH_END_PATTERN)
_WINDOWS_UNICODE_LIKE_UNC_PATH_TEXT_PATTERN = re.compile(
    r"""(?<![A-Za-z0-9])\\\\u[0-9A-Fa-f]{4}\\(?!\\u[0-9A-Fa-f]{4})(?=[^\r\n,;:)"'\]}\\])"""
    r"""[^\r\n,;:)\"']*?""" + _PATH_END_PATTERN
)
_WINDOWS_PATH_TEXT_PATTERN = re.compile(
    r"""(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\(?!u[0-9A-Fa-f]{4}))[^\r\n,;:)\"']*?""" + _PATH_END_PATTERN
)
_ABSOLUTE_PATH_AFTER_SPACE_PATTERN = re.compile(r"""(\s+)(?=(?:/(?!/)|[A-Za-z]:[\\/]|\\\\))""")

_TRUSTED_ROOT_LABEL = "[trusted]"
_WORKSPACE_ROOT_LABEL = "."
_CONFIG_ROOT_LABEL = "$IAC_CODE_CONFIG_DIR"


@dataclass(frozen=True)
class _NormalizedRoot:
    norm_path: str
    label: str
    windows: bool
    order: int


def build_public_path_roots(
    *,
    cwd: str,
    additional_directories: Iterable[str] | None = None,
    trusted_read_directories: Iterable[str] | None = None,
    relative_read_directories: Iterable[str] | None = None,
    include_config_dir: bool = True,
) -> list[dict[str, str]]:
    """Build public path roots from the runtime's trusted path context."""

    roots: list[dict[str, str]] = []

    def add(path: object, label: str) -> None:
        if path is None:
            return
        text = str(path).strip()
        if text:
            roots.append({"path": text, "label": label})

    add(cwd, _WORKSPACE_ROOT_LABEL)
    if include_config_dir:
        from iac_code.config import get_config_dir

        add(get_config_dir(), _CONFIG_ROOT_LABEL)
    for root in additional_directories or []:
        add(root, _TRUSTED_ROOT_LABEL)
    for root in trusted_read_directories or []:
        add(root, _TRUSTED_ROOT_LABEL)
    for root in relative_read_directories or []:
        add(root, _TRUSTED_ROOT_LABEL)
    return roots


def sanitize_public_paths(value: str, public_path_roots: Iterable[Mapping[str, str]] | None) -> str:
    """Replace absolute paths under public roots with root-relative labels."""

    roots = _normalize_public_path_roots(public_path_roots)
    if not roots:
        return value

    def replace(match: re.Match[str]) -> str:
        return _sanitize_public_path_token(match.group(0), roots)

    sanitized = _WINDOWS_UNICODE_LIKE_UNC_PATH_TEXT_PATTERN.sub(replace, value)
    sanitized = _WINDOWS_PATH_TEXT_PATTERN.sub(replace, sanitized)
    return _POSIX_PATH_TEXT_PATTERN.sub(replace, sanitized)


def _sanitize_public_path_token(token: str, roots: list[_NormalizedRoot]) -> str:
    parts = _ABSOLUTE_PATH_AFTER_SPACE_PATTERN.split(token)
    if len(parts) == 1:
        return _relativize_public_path(token, roots) or "[PATH]"
    rendered: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.isspace():
            rendered.append(part)
            continue
        rendered.append(_relativize_public_path(part, roots) or "[PATH]")
    return "".join(rendered)


def relativize_public_file_uri(uri: str, public_path_roots: Iterable[Mapping[str, str]] | None) -> str | None:
    """Return a public relative path for a file URI when it is under a public root."""

    roots = _normalize_public_path_roots(public_path_roots)
    if not roots:
        return None
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file":
        return None
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        if _looks_like_windows_drive(parsed.netloc):
            path = parsed.netloc + path
        elif path:
            path = f"//{parsed.netloc}{path}"
    if re.match(r"^/[A-Za-z]:[\\/]", path):
        path = path[1:]
    return _relativize_public_path(path, roots)


def relativize_public_path(
    path: str,
    public_path_roots: Iterable[Mapping[str, str]] | None,
) -> str | None:
    """Return a public root-relative path, choosing the shortest matching root."""

    roots = _normalize_public_path_roots(public_path_roots)
    return _relativize_public_path(path, roots)


def _relativize_public_path(path: str, roots: list[_NormalizedRoot]) -> str | None:
    if not roots:
        return None
    windows = _is_windows_path(path)
    candidates = _candidate_norm_paths(path, windows=windows)
    matches: list[tuple[int, int, _NormalizedRoot, str]] = []
    for token_norm in candidates:
        for root in roots:
            if root.windows != windows:
                continue
            if _path_is_under_root(token_norm, root.norm_path, windows=windows):
                matches.append((len(root.norm_path), root.order, root, token_norm))
    if not matches:
        return None

    _, _, root, token_norm = min(matches, key=lambda item: (item[0], item[1]))
    module = ntpath if windows else posixpath
    rel = module.relpath(token_norm, root.norm_path)
    rel = "" if rel == "." else rel.replace("\\", "/")
    if root.label == _WORKSPACE_ROOT_LABEL:
        return "." if not rel else f"./{rel}"
    return root.label if not rel else f"{root.label}/{rel}"


def _normalize_public_path_roots(public_path_roots: Iterable[Mapping[str, str]] | None) -> list[_NormalizedRoot]:
    roots: list[_NormalizedRoot] = []
    seen: set[tuple[str, str, bool]] = set()
    for order, root in enumerate(public_path_roots or []):
        raw_path = str(root.get("path") or "").strip()
        if not raw_path:
            continue
        label = str(root.get("label") or _TRUSTED_ROOT_LABEL).strip() or _TRUSTED_ROOT_LABEL
        windows = _is_windows_path(raw_path)
        for norm_path in _candidate_norm_paths(raw_path, windows=windows):
            key = (norm_path, label, windows)
            if key in seen:
                continue
            seen.add(key)
            roots.append(_NormalizedRoot(norm_path=norm_path, label=label, windows=windows, order=order))
    return roots


def _candidate_norm_paths(path: str, *, windows: bool) -> list[str]:
    expanded = os.path.expandvars(os.path.expanduser(path))
    if windows:
        norm = _normalize_windows_path(expanded)
        return [norm] if norm else []

    absolute = os.path.abspath(expanded)
    candidates = [_normalize_posix_path(absolute)]
    real = _normalize_posix_path(os.path.realpath(absolute))
    if real not in candidates:
        candidates.append(real)
    return [candidate for candidate in candidates if candidate]


def _normalize_windows_path(path: str) -> str:
    normalized = ntpath.normcase(ntpath.normpath(path.replace("/", "\\")))
    if normalized not in {"\\", "\\\\"} and not re.match(r"^[a-z]:\\$", normalized):
        normalized = normalized.rstrip("\\")
    return normalized


def _normalize_posix_path(path: str) -> str:
    normalized = posixpath.normpath(path)
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized


def _path_is_under_root(path: str, root: str, *, windows: bool) -> bool:
    if path == root:
        return True
    sep = "\\" if windows else "/"
    if root == sep or re.match(r"^[a-z]:\\$", root):
        return path.startswith(root)
    return path.startswith(root + sep)


def _is_windows_path(path: str) -> bool:
    return bool(ntpath.splitdrive(path)[0] or path.startswith("\\\\"))


def _looks_like_windows_drive(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:$", value))
