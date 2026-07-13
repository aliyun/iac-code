from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from iac_code.config import get_config_dir
from iac_code.i18n import _
from iac_code.services.session_layout import SessionPaths, ensure_session_owned_dir
from iac_code.tools.base import ToolResult
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.state_io import atomic_write_bytes

MAX_INLINE_TEXT_CHARS = 50_000
MAX_INLINE_TEXT_BYTES = 100_000


def convert_mcp_tool_result(
    result: Any,
    *,
    server_name: str,
    tool_name: str,
    session_id: str,
    session_dir: Path | str | None = None,
) -> ToolResult:
    """Convert an MCP tool result into iac-code's model-visible ToolResult."""

    artifacts: list[dict[str, Any]] = []
    sections: list[str] = []

    content_blocks = _get_value(result, "content", []) or []
    for index, block in enumerate(content_blocks):
        converted = _convert_content_block(
            block,
            server_name=server_name,
            tool_name=tool_name,
            session_id=session_id,
            session_dir=session_dir,
            index=index,
            artifacts=artifacts,
        )
        if converted:
            sections.append(converted)

    structured_content = _get_value(result, "structuredContent")
    if structured_content is not None:
        converted_structured_content = _convert_text_content(
            _json_dumps(structured_content),
            mime_type="application/json",
            kind="structured-content",
            server_name=server_name,
            tool_name=tool_name,
            session_id=session_id,
            session_dir=session_dir,
            index=len(content_blocks),
            artifacts=artifacts,
        )
        sections.append(_("Structured content:\n{content}").format(content=converted_structured_content))

    is_error = bool(_get_value(result, "isError", False))
    meta = _get_value(result, "_meta")
    if meta is None:
        meta = _get_value(result, "meta", {})

    metadata = {
        "mcp": {
            "server_name": server_name,
            "tool_name": tool_name,
            "is_error": is_error,
            "meta": meta or {},
            "artifacts": artifacts,
        }
    }
    content = "\n\n".join(section for section in sections if section).strip()
    if not content:
        content = _("MCP tool returned no content.")
    return ToolResult(content=content, is_error=is_error, metadata=metadata)


def _convert_content_block(
    block: Any,
    *,
    server_name: str,
    tool_name: str,
    session_id: str,
    session_dir: Path | str | None,
    index: int,
    artifacts: list[dict[str, Any]],
) -> str:
    block_type = _get_value(block, "type")
    if block_type == "text":
        return _convert_text_content(
            str(_get_value(block, "text", "")),
            mime_type=str(_get_value(block, "mimeType", "text/plain") or "text/plain"),
            kind="text",
            server_name=server_name,
            tool_name=tool_name,
            session_id=session_id,
            session_dir=session_dir,
            index=index,
            artifacts=artifacts,
        )

    if block_type in {"image", "audio"}:
        return _store_base64_artifact(
            _get_value(block, "data", ""),
            mime_type=str(_get_value(block, "mimeType", "application/octet-stream")),
            kind=str(block_type),
            server_name=server_name,
            tool_name=tool_name,
            session_id=session_id,
            session_dir=session_dir,
            index=index,
            artifacts=artifacts,
        )

    if block_type == "resource":
        resource = _get_value(block, "resource", {})
        text = _get_value(resource, "text")
        uri = str(_get_value(resource, "uri", ""))
        mime_type = _get_value(resource, "mimeType")
        if text is not None:
            header = _("Resource from MCP server {server!r}\nURI: {uri}").format(server=server_name, uri=uri)
            if mime_type:
                header = _("{header}\nMIME: {mime_type}").format(header=header, mime_type=mime_type)
            converted_text = _convert_text_content(
                str(text),
                mime_type=str(mime_type or "text/plain"),
                kind="resource",
                server_name=server_name,
                tool_name=tool_name,
                session_id=session_id,
                session_dir=session_dir,
                index=index,
                artifacts=artifacts,
                uri=uri,
            )
            return "{}\n\n{}".format(header, converted_text)

        blob = _get_value(resource, "blob")
        if blob is not None:
            return _store_base64_artifact(
                blob,
                mime_type=str(mime_type or "application/octet-stream"),
                kind="resource",
                server_name=server_name,
                tool_name=tool_name,
                session_id=session_id,
                session_dir=session_dir,
                index=index,
                artifacts=artifacts,
                uri=uri,
            )

    if block_type == "resource_link":
        name = str(_get_value(block, "name", "") or _("(unnamed)"))
        uri = str(_get_value(block, "uri", ""))
        mime_type = _get_value(block, "mimeType")
        details = [_("Resource link: {name}").format(name=name), _("URI: {uri}").format(uri=uri)]
        if mime_type:
            details.append(_("MIME: {mime_type}").format(mime_type=mime_type))
        return "\n".join(details)

    return _("Unsupported MCP content block:\n{content}").format(content=_json_dumps(_to_jsonable(block)))


def _convert_text_content(
    text: str,
    *,
    mime_type: str,
    kind: str,
    server_name: str,
    tool_name: str,
    session_id: str,
    session_dir: Path | str | None,
    index: int,
    artifacts: list[dict[str, Any]],
    uri: str | None = None,
) -> str:
    if len(text) <= MAX_INLINE_TEXT_CHARS and len(text.encode("utf-8")) <= MAX_INLINE_TEXT_BYTES:
        return text
    return _store_text_artifact(
        text,
        mime_type=mime_type,
        kind=kind,
        server_name=server_name,
        tool_name=tool_name,
        session_id=session_id,
        session_dir=session_dir,
        index=index,
        artifacts=artifacts,
        uri=uri,
    )


def _artifact_directory(
    *,
    server_name: str,
    tool_name: str,
    session_id: str,
    session_dir: Path | str | None,
) -> Path:
    if session_dir is not None:
        session_root = Path(session_dir)
        session_paths = SessionPaths.require_supported(session_root)
        artifact_root = ensure_session_owned_dir(session_root, session_paths.tool_results_dir)
    else:
        artifact_root = get_config_dir() / "tool-results" / session_id
    directory = artifact_root / "mcp" / _safe_path_segment(server_name) / _safe_path_segment(tool_name)
    if session_dir is not None:
        return ensure_session_owned_dir(session_dir, directory)
    return ensure_private_dir(directory)


def _store_text_artifact(
    text: str,
    *,
    mime_type: str,
    kind: str,
    server_name: str,
    tool_name: str,
    session_id: str,
    session_dir: Path | str | None,
    index: int,
    artifacts: list[dict[str, Any]],
    uri: str | None = None,
) -> str:
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()[:16]
    extension = _extension_for_text_mime_type(mime_type)
    directory = _artifact_directory(
        server_name=server_name,
        tool_name=tool_name,
        session_id=session_id,
        session_dir=session_dir,
    )
    path = directory / "{:02d}-{}-{}{}".format(index, _safe_path_segment(kind), digest, extension)
    atomic_write_bytes(path, data)

    artifact_id = "{}/{}/{}".format(
        _safe_path_segment(server_name),
        _safe_path_segment(tool_name),
        path.name,
    )
    artifact = {
        "id": artifact_id,
        "kind": kind,
        "mime_type": mime_type,
        "path": str(path),
        "size": len(data),
        "chars": len(text),
    }
    if uri:
        artifact["uri"] = uri
    artifacts.append(artifact)
    return (
        _("Saved large MCP text output as {artifact_id} ({chars} chars, {bytes} bytes).").format(
            artifact_id=artifact_id,
            chars=len(text),
            bytes=len(data),
        )
        + "\n"
        + _("Read the full output from {path}.").format(path=path)
    )


def _store_base64_artifact(
    encoded: object,
    *,
    mime_type: str,
    kind: str,
    server_name: str,
    tool_name: str,
    session_id: str,
    session_dir: Path | str | None,
    index: int,
    artifacts: list[dict[str, Any]],
    uri: str | None = None,
) -> str:
    if not isinstance(encoded, str):
        raise ValueError(_("MCP {kind} content must contain base64 string data.").format(kind=kind))

    data = base64.b64decode(encoded, validate=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    extension = _extension_for_mime_type(mime_type)
    directory = _artifact_directory(
        server_name=server_name,
        tool_name=tool_name,
        session_id=session_id,
        session_dir=session_dir,
    )
    path = directory / "{:02d}-{}-{}{}".format(index, _safe_path_segment(kind), digest, extension)
    atomic_write_bytes(path, data)

    artifact_id = "{}/{}/{}".format(
        _safe_path_segment(server_name),
        _safe_path_segment(tool_name),
        path.name,
    )
    artifact = {
        "id": artifact_id,
        "kind": kind,
        "mime_type": mime_type,
        "path": str(path),
        "size": len(data),
    }
    if uri:
        artifact["uri"] = uri
    artifacts.append(artifact)
    return _("Saved {mime_type} artifact as {artifact_id} ({bytes} bytes).").format(
        mime_type=mime_type,
        artifact_id=artifact_id,
        bytes=len(data),
    )


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    if key == "_meta":
        return getattr(value, "meta", default)
    return getattr(value, key, default)


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, mode="json")
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True)


def _safe_path_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return safe or "mcp"


def _extension_for_mime_type(mime_type: str) -> str:
    normalized = mime_type.split(";", 1)[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/ogg": ".ogg",
        "application/json": ".json",
        "application/octet-stream": ".bin",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "text/yaml": ".yml",
        "video/mp4": ".mp4",
    }.get(normalized, ".bin")


def _extension_for_text_mime_type(mime_type: str) -> str:
    normalized = mime_type.split(";", 1)[0].strip().lower()
    if normalized in {"application/json", "text/json"} or normalized.endswith("+json"):
        return ".json"
    if normalized in {"text/markdown", "text/x-markdown"}:
        return ".md"
    return ".txt"
