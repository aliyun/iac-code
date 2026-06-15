from __future__ import annotations

import base64
import hashlib
import ntpath
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from iac_code.i18n import _
from iac_code.utils.public_errors import sanitize_public_text

PUBLIC_ARTIFACT_URI_PREFIX = "iac-code-artifact://"
_PUBLIC_ARTIFACT_URI_SCHEME = "iac-code-artifact"
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
_ARTIFACT_PAYLOAD_KEYS = {"content", "bytes", "base64", "raw", "path"}
_ARTIFACT_CONTAINER_KEYS = {"artifact", "artifacts"}
_ARTIFACT_URI_KEY_SUFFIXES = ("uri", "url")
_SENSITIVE_OUTPUT_KEY_FRAGMENTS = {
    "auth",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "passphrase",
    "password",
    "passwd",
    "private_key",
    "pwd",
    "secret",
    "session",
    "signature",
    "token",
    "api_key",
    "access_key",
    "access_key_id",
    "access_key_secret",
}
_CONNECTOR_TOKEN_END_PATTERN = r"""\s+(?:and|at|because|for|from|in|on|to|with)\b(?=\s+[A-Za-z0-9_.-]+\s*[:=])"""
_URI_TOKEN_END_PATTERN = r"""(?=""" + _CONNECTOR_TOKEN_END_PATTERN + r"""|$|[\r\n,;)"'\]}]|:(?![\\/]|%5[Cc]|%2[Ff]))"""
_FILE_URI_TEXT_PATTERN = re.compile(r"""file://[^\r\n,;)"'\]}]*?""" + _URI_TOKEN_END_PATTERN, re.IGNORECASE)
_PUBLIC_ARTIFACT_URI_TEXT_PATTERN = re.compile(r"""iac-code-artifact://[^\r\n,;)"'\]}]*?""" + _URI_TOKEN_END_PATTERN)


class UnsafeArtifactNameError(ValueError):
    """Raised when an artifact filename would escape the artifact directory."""


def safe_artifact_filename(filename: str) -> str:
    if not filename:
        raise UnsafeArtifactNameError(_("Unsafe artifact filename"))

    if "\\" in filename or ntpath.splitdrive(filename)[0] or filename.startswith("\\\\"):
        safe_name = ntpath.basename(filename)
    else:
        if filename != os.path.basename(filename):
            raise UnsafeArtifactNameError(_("Unsafe artifact filename"))
        safe_name = filename

    if not safe_name or safe_name in {".", ".."} or safe_name != os.path.basename(safe_name):
        raise UnsafeArtifactNameError(_("Unsafe artifact filename"))
    safe_name = _windows_safe_filename(safe_name)
    if not safe_name or safe_name in {".", ".."} or safe_name != ntpath.basename(safe_name):
        raise UnsafeArtifactNameError(_("Unsafe artifact filename"))
    return safe_name


def artifact_filename_from_path(path: str) -> str:
    if "\\" in path or ntpath.splitdrive(path)[0] or path.startswith("\\\\"):
        filename = ntpath.basename(path)
    else:
        filename = os.path.basename(path)
    return safe_artifact_filename(filename)


def _windows_safe_filename(filename: str) -> str:
    sanitized = "".join(
        "_" if ord(char) < 32 or char in _WINDOWS_FORBIDDEN_FILENAME_CHARS else char for char in filename
    ).rstrip(" .")
    if not sanitized:
        raise UnsafeArtifactNameError(_("Unsafe artifact filename"))
    reserved_check = sanitized.split(".", 1)[0].upper().translate(_WINDOWS_RESERVED_DIGIT_TRANSLATION)
    if reserved_check in _WINDOWS_RESERVED_BASENAMES:
        sanitized = f"_{sanitized}"
    return sanitized


def sanitize_public_artifact_data(value: Any) -> Any:
    if isinstance(value, list):
        return [sanitize_public_artifact_data(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public_artifact_data(item) for item in value]
    if isinstance(value, str):
        return _sanitize_artifact_scalar_string(value)
    if not isinstance(value, dict):
        return value

    sanitized: dict[Any, Any] = {}
    for key, raw_value in value.items():
        key_name = str(key)
        if key_name.lower() in _ARTIFACT_PAYLOAD_KEYS:
            continue
        if _is_sensitive_output_key(key_name):
            sanitized[key] = "[REDACTED]"
            continue
        if _is_artifact_uri_key(key_name):
            if isinstance(raw_value, str):
                sanitized_value = _sanitize_artifact_string(key_name, raw_value)
                if sanitized_value is not None:
                    sanitized[key] = sanitized_value
            continue
        if isinstance(raw_value, str):
            sanitized_value = _sanitize_artifact_string(key_name, raw_value)
            if sanitized_value is None:
                continue
            sanitized[key] = sanitized_value
            continue
        sanitized[key] = sanitize_public_artifact_data(raw_value)
    return sanitized


def sanitize_public_tool_output_data(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_public_artifact_text(value, fallback_summary="")
    if isinstance(value, list):
        return [sanitize_public_tool_output_data(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public_tool_output_data(item) for item in value]
    if not isinstance(value, dict):
        return value
    if _looks_like_artifact_payload_dict(value):
        return sanitize_public_artifact_data(value)

    sanitized: dict[Any, Any] = {}
    for key, raw_value in value.items():
        if str(key).lower() in _ARTIFACT_CONTAINER_KEYS:
            sanitized[key] = sanitize_public_artifact_data(raw_value)
        elif _is_sensitive_output_key(key):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = sanitize_public_tool_output_data(raw_value)
    return sanitized


def _sanitize_artifact_string(key: str, value: str) -> str | None:
    if _is_artifact_uri_key(key):
        return value if is_valid_public_artifact_uri(value) else None
    if key.lower() in {"filename", "name"}:
        for candidate in _artifact_filename_candidates(value):
            try:
                return artifact_filename_from_path(candidate)
            except UnsafeArtifactNameError:
                pass
    if value.lower().startswith("file://"):
        return "[PATH]"
    return _sanitize_artifact_scalar_string(value)


def _sanitize_artifact_scalar_string(value: str) -> str:
    if value.lower().startswith("file://"):
        return "[PATH]"
    decoded = unquote(value)
    if decoded != value:
        if decoded.lower().startswith("file://"):
            return "[PATH]"
        decoded_sanitized = sanitize_public_artifact_text(decoded, fallback_summary="")
        if decoded_sanitized != decoded:
            return decoded_sanitized
    return sanitize_public_artifact_text(value, fallback_summary="")


def sanitize_public_artifact_text(value: str, *, fallback_summary: str | None = None) -> str:
    if fallback_summary is None:
        fallback_summary = _("Unknown error")
    protected, placeholders = _replace_public_artifact_uri_tokens(value)
    protected = _replace_file_uri_tokens(protected)
    sanitized = sanitize_public_text(protected, fallback_summary=fallback_summary)
    decoded_raw = unquote(protected)
    decoded, decoded_placeholders = _replace_public_artifact_uri_tokens(decoded_raw, prefix="__IAC_CODE_DECODED_URI_")
    decoded = _replace_file_uri_tokens(decoded)
    if decoded_raw != protected:
        decoded_sanitized = sanitize_public_text(decoded, fallback_summary="")
        if decoded != decoded_raw or decoded_sanitized != decoded:
            sanitized = decoded_sanitized
            placeholders.update(decoded_placeholders)
    for placeholder, uri in placeholders.items():
        sanitized = sanitized.replace(placeholder, uri)
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
    value: str, *, prefix: str = "__IAC_CODE_ARTIFACT_URI_"
) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def preserve_uri(uri: str, trailing: str = "") -> str:
        placeholder = f"{prefix}{len(placeholders)}__"
        placeholders[placeholder] = uri
        return f"{placeholder}{trailing}"

    def replace_artifact_uri(match: re.Match[str]) -> str:
        uri = match.group(0)
        if is_valid_public_artifact_uri(uri):
            return preserve_uri(uri)
        whitespace = re.search(r"\s", uri)
        if whitespace is not None:
            candidate = uri[: whitespace.start()]
            rest = uri[whitespace.start() :]
            candidate_trailing = ""
            while candidate and candidate[-1] in ".,:!?":
                candidate_trailing = candidate[-1] + candidate_trailing
                candidate = candidate[:-1]
            if is_valid_public_artifact_uri(candidate):
                return preserve_uri(candidate, f"{candidate_trailing}{rest}")
        trailing = ""
        while uri and uri[-1] in ".,:!?":
            trailing = uri[-1] + trailing
            uri = uri[:-1]
        if not is_valid_public_artifact_uri(uri):
            return "[PATH]"
        placeholder = f"{prefix}{len(placeholders)}__"
        placeholders[placeholder] = uri
        return f"{placeholder}{trailing}"

    return _PUBLIC_ARTIFACT_URI_TEXT_PATTERN.sub(replace_artifact_uri, value), placeholders


def _artifact_filename_candidates(value: str) -> tuple[str, ...]:
    decoded = unquote(value)
    if decoded != value and sanitize_public_artifact_text(decoded, fallback_summary="") != decoded:
        return (decoded, value)
    return (value,)


def _is_artifact_uri_key(key: str) -> bool:
    return key.lower().endswith(_ARTIFACT_URI_KEY_SUFFIXES)


def _is_sensitive_output_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    compact = normalized.replace("_", "")
    return any(
        fragment in normalized or fragment.replace("_", "") in compact for fragment in _SENSITIVE_OUTPUT_KEY_FRAGMENTS
    )


def _looks_like_artifact_payload_dict(value: dict[Any, Any]) -> bool:
    keys = {str(key).lower() for key in value}
    has_payload = bool(keys & _ARTIFACT_PAYLOAD_KEYS)
    has_context = bool(keys & {"filename", "name", "mediatype", "media_type"}) or any(
        _is_artifact_uri_key(str(key)) and isinstance(raw_value, str) and is_valid_public_artifact_uri(raw_value)
        for key, raw_value in value.items()
    )
    return has_payload and has_context


def is_valid_public_artifact_uri(uri: str) -> bool:
    if not uri.startswith(PUBLIC_ARTIFACT_URI_PREFIX) or "\\" in uri:
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
    try:
        return safe_artifact_filename(filename) == filename and quote(filename, safe="") == filename_segment
    except UnsafeArtifactNameError:
        return False


def _is_safe_artifact_id(value: str) -> bool:
    if not value or value in {".", ".."}:
        return False
    return all(char.isascii() and (char.isalnum() or char in "_-") for char in value)


@dataclass(frozen=True)
class A2AArtifactMetadata:
    artifact_id: str
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    uri: str

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        return {
            "artifactId": data["artifact_id"],
            "filename": data["filename"],
            "mediaType": data["media_type"],
            "byteSize": data["byte_size"],
            "sha256": data["sha256"],
            "uri": data["uri"],
        }


class A2AArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save_text(self, *, filename: str, content: str, media_type: str) -> A2AArtifactMetadata:
        encoded = content.encode("utf-8")
        return self.save_bytes(filename=filename, content=encoded, media_type=media_type)

    def save_base64(self, *, filename: str, content: str, media_type: str) -> A2AArtifactMetadata:
        decoded = base64.b64decode(content.encode("ascii"), validate=True)
        return self.save_bytes(filename=filename, content=decoded, media_type=media_type)

    def save_bytes(self, *, filename: str, content: bytes, media_type: str) -> A2AArtifactMetadata:
        safe_name = self._safe_filename(filename)
        artifact_id = str(uuid.uuid4())
        artifact_dir = self.root / artifact_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        path = artifact_dir / safe_name
        path.write_bytes(content)
        return A2AArtifactMetadata(
            artifact_id=artifact_id,
            filename=safe_name,
            media_type=media_type,
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            uri=self._public_uri(artifact_id, safe_name),
        )

    def path_for(self, artifact_id: str) -> Path:
        candidates = list((self.root / artifact_id).iterdir())
        if not candidates:
            raise FileNotFoundError(artifact_id)
        return candidates[0]

    @staticmethod
    def _public_uri(artifact_id: str, filename: str) -> str:
        return f"{PUBLIC_ARTIFACT_URI_PREFIX}{artifact_id}/{quote(filename, safe='')}"

    @staticmethod
    def _safe_filename(filename: str) -> str:
        return safe_artifact_filename(filename)
