from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from google.protobuf.json_format import MessageToDict

MAX_INLINE_BYTES = 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_BINARY_INLINE_BYTES = 5 * 1024 * 1024
MAX_BINARY_FILE_BYTES = 25 * 1024 * 1024

DEFAULT_TEXT_LIKE_MIME_TYPES = (
    "text/plain",
    "application/json",
    "text/markdown",
    "text/yaml",
    "application/yaml",
    "application/x-yaml",
)
DEFAULT_MULTIMODAL_MIME_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "application/octet-stream",
)
TEXT_LIKE_MIME_TYPES = frozenset(DEFAULT_TEXT_LIKE_MIME_TYPES)
MULTIMODAL_MIME_TYPES = frozenset(DEFAULT_MULTIMODAL_MIME_TYPES)
SUPPORTED_INPUT_MIME_TYPES = [*DEFAULT_TEXT_LIKE_MIME_TYPES, *DEFAULT_MULTIMODAL_MIME_TYPES]


def supported_input_mime_types() -> list[str]:
    values = [
        *DEFAULT_TEXT_LIKE_MIME_TYPES,
        *DEFAULT_MULTIMODAL_MIME_TYPES,
        *sorted(_extra_mime_types("IACCODE_A2A_TEXT_MIME_TYPES")),
        *sorted(_extra_mime_types("IACCODE_A2A_MULTIMODAL_MIME_TYPES")),
    ]
    return list(dict.fromkeys(values))


def text_like_mime_types() -> frozenset[str]:
    return TEXT_LIKE_MIME_TYPES | _extra_mime_types("IACCODE_A2A_TEXT_MIME_TYPES")


def multimodal_mime_types() -> frozenset[str]:
    return MULTIMODAL_MIME_TYPES | _extra_mime_types("IACCODE_A2A_MULTIMODAL_MIME_TYPES")


def _extra_mime_types(env_name: str) -> frozenset[str]:
    raw = os.environ.get(env_name, "")
    return frozenset(item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip())


def allowed_cwd_roots() -> list[Path]:
    raw = os.environ.get("IACCODE_A2A_ALLOWED_CWDS")
    if raw:
        candidates = [Path(item) for item in raw.split(os.pathsep) if item]
    else:
        candidates = [Path.cwd(), Path(tempfile.gettempdir())]
    return [resolve_workspace_path(path) for path in candidates if path.exists() and path.is_dir()]


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_workspace_path(path: Path) -> Path:
    try:
        return path.resolve()
    except FileNotFoundError:
        if not path.is_absolute() or _has_symlink_component(path):
            raise
        return path.absolute()


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return False
    return False


def parts_to_prompt(message_parts: Iterable[Any], *, cwd: str | Path) -> str:
    values = [part_to_prompt(part, cwd=cwd) for part in message_parts]
    return "\n".join(value for value in values if value)


def part_to_prompt(part: Any, *, cwd: str | Path) -> str:
    media_type = _media_type(part)
    if _has_field(part, "text"):
        _ensure_text_like(media_type)
        return str(part.text)
    if _has_field(part, "data"):
        if _is_multimodal(media_type):
            return _binary_data_part_to_manifest(part, media_type=media_type)
        if media_type != "application/json":
            raise ValueError("A2A data parts must use application/json media type.")
        data = MessageToDict(part.data, preserving_proto_field_name=False)
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        _ensure_size(serialized.encode("utf-8"), limit=MAX_INLINE_BYTES, label="A2A data part")
        return serialized
    if _has_field(part, "raw"):
        raw = bytes(part.raw)
        if _is_multimodal(media_type):
            _ensure_size(raw, limit=MAX_BINARY_INLINE_BYTES, label="A2A binary raw part")
            return _multimodal_manifest(
                filename=_filename(part) or "inline",
                media_type=media_type,
                content=raw,
                source="inline",
            )
        _ensure_text_like(media_type)
        _ensure_size(raw, limit=MAX_INLINE_BYTES, label="A2A raw part")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("A2A raw parts must contain valid UTF-8.") from exc
    if _has_field(part, "url"):
        if _is_multimodal(media_type):
            return _file_url_part_to_manifest(str(part.url), media_type=media_type, cwd=Path(cwd))
        _ensure_text_like(media_type)
        return _read_file_url_part(str(part.url), cwd=Path(cwd))
    raise ValueError("A2A server supports text, JSON data, raw text, or workspace file URL parts only.")


def _read_file_url_part(url: str, *, cwd: Path) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "file" or parsed.netloc:
        raise ValueError("A2A file URL parts must use local file:// URLs.")

    cwd_path = cwd.resolve()
    path = Path(url2pathname(unquote(parsed.path))).resolve()
    if not is_relative_to(path, cwd_path) or not any(is_relative_to(path, root) for root in allowed_cwd_roots()):
        raise ValueError("A2A file URL part is outside the allowed workspace.")
    if not path.is_file():
        raise ValueError("A2A file URL part must reference an existing file.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("A2A file URL part content is too large.")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("A2A file URL parts must contain valid UTF-8.") from exc


def _media_type(part: Any) -> str:
    return str(getattr(part, "media_type", "") or "text/plain").lower()


def _ensure_text_like(media_type: str) -> None:
    if media_type not in text_like_mime_types():
        raise ValueError("A2A part has unsupported media type.")


def _ensure_size(content: bytes, *, limit: int, label: str) -> None:
    if len(content) > limit:
        raise ValueError(f"{label} content is too large.")


def _is_multimodal(media_type: str) -> bool:
    return media_type in multimodal_mime_types()


def _filename(part: Any) -> str:
    return os.path.basename(str(getattr(part, "filename", "") or ""))


def _binary_data_part_to_manifest(part: Any, *, media_type: str) -> str:
    data = MessageToDict(part.data, preserving_proto_field_name=False)
    if not isinstance(data, dict):
        raise ValueError("A2A binary data parts must contain an object.")
    encoded = data.get("bytes") or data.get("base64")
    if not isinstance(encoded, str):
        raise ValueError("A2A binary data parts must include base64 bytes.")
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("A2A binary data part bytes must be valid base64.") from exc
    _ensure_size(content, limit=MAX_BINARY_INLINE_BYTES, label="A2A binary data part")
    filename = str(data.get("filename") or _filename(part) or "inline")
    return _multimodal_manifest(
        filename=os.path.basename(filename),
        media_type=media_type,
        content=content,
        source="data",
    )


def _file_url_part_to_manifest(url: str, *, media_type: str, cwd: Path) -> str:
    path = _safe_file_url_path(url, cwd=cwd)
    if path.stat().st_size > MAX_BINARY_FILE_BYTES:
        raise ValueError("A2A binary file URL part content is too large.")
    content = path.read_bytes()
    return _multimodal_manifest(filename=path.name, media_type=media_type, content=content, source=path.as_uri())


def _safe_file_url_path(url: str, *, cwd: Path) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "file" or parsed.netloc:
        raise ValueError("A2A file URL parts must use local file:// URLs.")

    cwd_path = cwd.resolve()
    path = Path(url2pathname(unquote(parsed.path))).resolve()
    if not is_relative_to(path, cwd_path) or not any(is_relative_to(path, root) for root in allowed_cwd_roots()):
        raise ValueError("A2A file URL part is outside the allowed workspace.")
    if not path.is_file():
        raise ValueError("A2A file URL part must reference an existing file.")
    return path


def _multimodal_manifest(*, filename: str, media_type: str, content: bytes, source: str) -> str:
    safe_filename = filename if filename and filename == os.path.basename(filename) else "attachment"
    return "\n".join(
        [
            "A2A multimodal attachment:",
            f"- filename={safe_filename}",
            f"- mediaType={media_type}",
            f"- byteSize={len(content)}",
            f"- sha256={hashlib.sha256(content).hexdigest()}",
            f"- source={source}",
        ]
    )


def _has_field(message: Any, field: str) -> bool:
    try:
        return bool(message.HasField(field))
    except ValueError:
        return False
