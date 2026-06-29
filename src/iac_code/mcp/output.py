from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Mapping

from iac_code.config import get_config_dir
from iac_code.i18n import _
from iac_code.tools.base import ToolResult
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.state_io import atomic_write_bytes


def convert_mcp_tool_result(
    result: Any,
    *,
    server_name: str,
    tool_name: str,
    session_id: str,
) -> ToolResult:
    """Convert an MCP tool result into iac-code's model-visible ToolResult."""

    artifacts: list[dict[str, Any]] = []
    sections: list[str] = []

    for index, block in enumerate(_get_value(result, "content", []) or []):
        converted = _convert_content_block(
            block,
            server_name=server_name,
            tool_name=tool_name,
            session_id=session_id,
            index=index,
            artifacts=artifacts,
        )
        if converted:
            sections.append(converted)

    structured_content = _get_value(result, "structuredContent")
    if structured_content is not None:
        sections.append(_("Structured content:\n{content}").format(content=_json_dumps(structured_content)))

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
    index: int,
    artifacts: list[dict[str, Any]],
) -> str:
    block_type = _get_value(block, "type")
    if block_type == "text":
        return str(_get_value(block, "text", ""))

    if block_type in {"image", "audio"}:
        return _store_base64_artifact(
            _get_value(block, "data", ""),
            mime_type=str(_get_value(block, "mimeType", "application/octet-stream")),
            kind=str(block_type),
            server_name=server_name,
            tool_name=tool_name,
            session_id=session_id,
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
            return "{}\n\n{}".format(header, text)

        blob = _get_value(resource, "blob")
        if blob is not None:
            return _store_base64_artifact(
                blob,
                mime_type=str(mime_type or "application/octet-stream"),
                kind="resource",
                server_name=server_name,
                tool_name=tool_name,
                session_id=session_id,
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


def _store_base64_artifact(
    encoded: object,
    *,
    mime_type: str,
    kind: str,
    server_name: str,
    tool_name: str,
    session_id: str,
    index: int,
    artifacts: list[dict[str, Any]],
    uri: str | None = None,
) -> str:
    if not isinstance(encoded, str):
        raise ValueError(_("MCP {kind} content must contain base64 string data.").format(kind=kind))

    data = base64.b64decode(encoded, validate=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    extension = _extension_for_mime_type(mime_type)
    directory = (
        get_config_dir()
        / "tool-results"
        / session_id
        / "mcp"
        / _safe_path_segment(server_name)
        / _safe_path_segment(tool_name)
    )
    ensure_private_dir(directory)
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
        "text/plain": ".txt",
        "text/markdown": ".md",
        "text/yaml": ".yml",
    }.get(normalized, ".bin")
