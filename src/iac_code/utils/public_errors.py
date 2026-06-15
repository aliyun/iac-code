"""Helpers for public error payloads."""

from __future__ import annotations

import hashlib
import ntpath
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from iac_code.i18n import _

_SECRET_VALUE_PATTERN = r"""(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}|"[^"]*"|'[^']*'|[^\s,;}]+)"""

_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    \b(
        [A-Za-z0-9_.-]*
        (?:
            auth|authorization|cookie|credential|credentials|passphrase|password|passwd|private[_-]?key|pwd|secret|
            session|signature|token|api[_-]?key|access[_-]?key(?:[_-]?(?:id|secret))?
        )
        [A-Za-z0-9_.-]*
    )
    (\s*[:=]\s*)
    """
    + _SECRET_VALUE_PATTERN
)

_QUOTED_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    (["'])
    (
        [A-Za-z0-9_.-]*
        (?:
            auth|authorization|cookie|credential|credentials|passphrase|password|passwd|private[_-]?key|pwd|secret|
            session|signature|token|api[_-]?key|access[_-]?key(?:[_-]?(?:id|secret))?
        )
        [A-Za-z0-9_.-]*
    )
    \1
    (\s*:\s*)
    """
    + _SECRET_VALUE_PATTERN
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*[:=]\s*[^\r\n]+"),
    re.compile(r"(?i)\b(?:cookie|set-cookie)\s*[:=]\s*[^\r\n]+"),
    re.compile(r"(?i)\b(?:x-acs-security-token|x-acs-signature|x-ca-signature)\s*[:=]\s*[^\s,;}]+"),
    re.compile(
        r"""(?ix)
        ([?&]
            (?:
                Signature|X-Amz-Signature|OSSAccessKeyId|AccessKeyId|AccessKeySecret|
                security-token|x-oss-security-token|x-acs-security-token|x-acs-signature|x-ca-signature
            )
        =)[^&\s]+
        """
    ),
    re.compile(r"(?i)\b(AKIA[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,})\b"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9][A-Za-z0-9_-]{7,}\b"),
)

_CONNECTOR_TOKEN_END_PATTERN = r"""\s+(?:and|at|because|for|from|in|on|to|with)\b(?=\s+[A-Za-z0-9_.-]+\s*[:=])"""
_URI_TOKEN_END_PATTERN = r"""(?=""" + _CONNECTOR_TOKEN_END_PATTERN + r"""|$|[\r\n,;)"'\]}]|:(?![\\/]|%5[Cc]|%2[Ff]))"""
_FILE_URI_TEXT_PATTERN = re.compile(r"""file://[^\r\n,;)"'\]}]*?""" + _URI_TOKEN_END_PATTERN, re.IGNORECASE)
_PUBLIC_ARTIFACT_URI_PREFIX = "iac-code-artifact://"
_PUBLIC_ARTIFACT_URI_SCHEME = "iac-code-artifact"
_PUBLIC_ARTIFACT_URI_TEXT_PATTERN = re.compile(r"""iac-code-artifact://[^\r\n,;)"'\]}]*?""" + _URI_TOKEN_END_PATTERN)
_WINDOWS_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_FILENAME_CHARS = set('<>:"/\\|?*')
_WINDOWS_RESERVED_DIGIT_TRANSLATION = str.maketrans(
    {
        "\N{SUPERSCRIPT ONE}": "1",
        "\N{SUPERSCRIPT TWO}": "2",
        "\N{SUPERSCRIPT THREE}": "3",
    }
)
_PATH_END_PATTERN = r"""(?=""" + _CONNECTOR_TOKEN_END_PATTERN + r"""|$|[\r\n,;:)"'])"""
_PATH_PATTERNS = (
    re.compile(r"~[/\\]\.iac-code[/\\]?[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"\$HOME[/\\]\.iac-code[/\\]?[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/etc/iac-code/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/Users/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/home/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/root/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/tmp/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/private/var/folders/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/var/folders/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/workspace/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"/workspaces/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:/[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"\\\\[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
    re.compile(r"(?<!:)//[^\r\n,;:)\"']*?" + _PATH_END_PATTERN),
)


@dataclass(frozen=True)
class PublicError:
    summary: str
    details: dict[str, Any]
    error_id: str


def public_error_from_exception(exc: BaseException, *, fallback_summary: str | None = None) -> PublicError:
    return public_error(
        message=str(exc),
        error_type=type(exc).__name__,
        fallback_summary=fallback_summary,
    )


def public_exception_summary(exc: BaseException, *, max_chars: int | None = None) -> str:
    message = str(exc)
    raw_summary = type(exc).__name__ if not message else f"{type(exc).__name__}: {message}"
    summary = sanitize_public_text(raw_summary)
    return summary[:max_chars] if max_chars is not None else summary


def public_error(
    *,
    message: Any,
    error_type: str,
    fallback_summary: str | None = None,
    extra_details: dict[str, Any] | None = None,
) -> PublicError:
    if fallback_summary is None:
        fallback_summary = _("Unknown error")
    summary = sanitize_public_text(message, fallback_summary=fallback_summary)
    error_id = _error_id(error_type=error_type, summary=summary)
    details: dict[str, Any] = {
        "type": error_type,
        "error_id": error_id,
        "traceback": _("Stack trace omitted from public event; see error_id."),
    }
    for key, value in (extra_details or {}).items():
        if value is None:
            continue
        details[str(key)] = _sanitize_public_value(value)
    return PublicError(summary=summary, details=details, error_id=error_id)


def sanitize_public_text(value: Any, *, fallback_summary: str | None = None) -> str:
    if fallback_summary is None:
        fallback_summary = _("Unknown error")
    raw_summary = str(value) if value is not None else ""
    return _sanitize_public_summary(raw_summary) or fallback_summary


def _error_id(*, error_type: str, summary: str) -> str:
    digest = hashlib.sha256(f"{error_type}\0{summary}".encode("utf-8", errors="replace")).hexdigest()
    return digest[:12]


def _sanitize_public_summary(value: str) -> str:
    protected, placeholders = _replace_public_artifact_uri_tokens(value)
    protected = _replace_file_uri_tokens(protected)
    sanitized = _apply_public_patterns(protected)

    decoded_raw = unquote(protected)
    decoded, decoded_placeholders = _replace_public_artifact_uri_tokens(
        decoded_raw, prefix="__IAC_CODE_PUBLIC_DECODED_URI_"
    )
    decoded = _replace_file_uri_tokens(decoded)
    if decoded_raw != protected:
        decoded_sanitized = _apply_public_patterns(decoded)
        if decoded != decoded_raw or decoded_sanitized != decoded:
            sanitized = decoded_sanitized
            placeholders.update(decoded_placeholders)

    for placeholder, uri in placeholders.items():
        sanitized = sanitized.replace(placeholder, uri)
    return sanitized


def _apply_public_patterns(value: str) -> str:
    sanitized = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            sanitized = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", sanitized)
        else:
            sanitized = pattern.sub("[REDACTED]", sanitized)
    sanitized = _QUOTED_SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(1)}{match.group(3)}[REDACTED]",
        sanitized,
    )
    sanitized = _SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", sanitized)
    for pattern in _PATH_PATTERNS:
        sanitized = pattern.sub("[PATH]", sanitized)
    return sanitized


def _replace_file_uri_tokens(value: str) -> str:
    def replace_file_uri(match: re.Match[str]) -> str:
        uri = match.group(0)
        trailing = ""
        while uri and uri[-1] in ".,:!?":
            trailing = uri[-1] + trailing
            uri = uri[:-1]
        return f"[PATH]{trailing}"

    return _FILE_URI_TEXT_PATTERN.sub(replace_file_uri, value)


def _replace_public_artifact_uri_tokens(
    value: str, *, prefix: str = "__IAC_CODE_PUBLIC_ARTIFACT_URI_"
) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def preserve_uri(uri: str, trailing: str = "") -> str:
        placeholder = f"{prefix}{len(placeholders)}__"
        placeholders[placeholder] = uri
        return f"{placeholder}{trailing}"

    def replace_artifact_uri(match: re.Match[str]) -> str:
        uri = match.group(0)
        if _is_valid_public_artifact_uri(uri):
            return preserve_uri(uri)
        whitespace = re.search(r"\s", uri)
        if whitespace is not None:
            candidate = uri[: whitespace.start()]
            rest = uri[whitespace.start() :]
            candidate_trailing = ""
            while candidate and candidate[-1] in ".,:!?":
                candidate_trailing = candidate[-1] + candidate_trailing
                candidate = candidate[:-1]
            if _is_valid_public_artifact_uri(candidate):
                return preserve_uri(candidate, f"{candidate_trailing}{rest}")
        trailing = ""
        while uri and uri[-1] in ".,:!?":
            trailing = uri[-1] + trailing
            uri = uri[:-1]
        if not _is_valid_public_artifact_uri(uri):
            return "[PATH]"
        placeholder = f"{prefix}{len(placeholders)}__"
        placeholders[placeholder] = uri
        return f"{placeholder}{trailing}"

    return _PUBLIC_ARTIFACT_URI_TEXT_PATTERN.sub(replace_artifact_uri, value), placeholders


def _is_valid_public_artifact_uri(uri: str) -> bool:
    if not uri.startswith(_PUBLIC_ARTIFACT_URI_PREFIX) or "\\" in uri:
        return False
    parts = urlsplit(uri)
    if parts.scheme != _PUBLIC_ARTIFACT_URI_SCHEME or not parts.netloc or parts.query or parts.fragment:
        return False
    if not _is_safe_artifact_id(parts.netloc):
        return False
    if not parts.path.startswith("/") or parts.path.count("/") != 1:
        return False
    filename_segment = parts.path[1:]
    if not filename_segment:
        return False
    filename = unquote(filename_segment)
    if not _is_safe_public_artifact_filename(filename):
        return False
    return quote(filename, safe="") == filename_segment


def _is_safe_artifact_id(value: str) -> bool:
    return bool(value) and all(char.isascii() and (char.isalnum() or char in "_-") for char in value)


def _is_safe_public_artifact_filename(filename: str) -> bool:
    if not filename or filename in {".", ".."}:
        return False
    if "/" in filename or "\\" in filename or ntpath.splitdrive(filename)[0] or filename.startswith("\\\\"):
        return False
    sanitized = "".join(
        "_" if ord(char) < 32 or char in _WINDOWS_FORBIDDEN_FILENAME_CHARS else char for char in filename
    ).rstrip(" .")
    if sanitized != filename or sanitized in {"", ".", ".."}:
        return False
    reserved_check = sanitized.split(".", 1)[0].upper().translate(_WINDOWS_RESERVED_DIGIT_TRANSLATION)
    return reserved_check not in _WINDOWS_RESERVED_BASENAMES


def _sanitize_public_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_public_summary(value)
    if isinstance(value, list):
        return [_sanitize_public_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_public_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_public_value(item) for key, item in value.items()}
    return value
