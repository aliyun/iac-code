"""Strict request validation for the AG-UI to A2A protocol boundary."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from ag_ui.core import RunAgentInput
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from iac_code.a2a.artifacts import UnsafeArtifactNameError, safe_artifact_filename
from iac_code.agui.errors import AguiError
from iac_code.utils.image.resizer import maybe_resize_and_downsample

MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 10 * 1024 * 1024
SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGE_FILENAME_BYTES = 255


class StrictModel(BaseModel):
    model_config = ConfigDict(alias_generator=None, populate_by_name=True, extra="forbid", strict=True)


class ThinkingOptions(StrictModel):
    enabled: bool = False
    effort: str | None = None
    budget: int | None = Field(default=None, gt=0)


class AlibabaCloudOptions(StrictModel):
    access_key_id: str | None = Field(default=None, alias="accessKeyId", repr=False)
    access_key_secret: str | None = Field(default=None, alias="accessKeySecret", repr=False)
    security_token: str | None = Field(default=None, alias="securityToken", repr=False)
    region_id: str | None = Field(default=None, alias="regionId")


class IacCodeForwardedProps(StrictModel):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    ros_invocation_id: str = Field(alias="rosInvocationId", min_length=1, max_length=256)
    cwd: str
    model: str | None = None
    llm_api_key: str | None = Field(default=None, alias="llmApiKey", repr=False)
    thinking: ThinkingOptions | None = None
    user_id: str | None = Field(default=None, alias="userId")
    channel: str | None = None
    preferred_language: str | None = Field(default=None, alias="preferredLanguage")
    candidate_presentation: Literal["standard", "rich"] | None = Field(
        default=None,
        alias="candidatePresentation",
    )
    run_mode: Literal["normal", "pipeline"] | None = Field(default=None, alias="runMode")
    pipeline_name: str | None = Field(default=None, alias="pipelineName")
    cleanup_only: bool = Field(default=False, alias="cleanupOnly")
    active_guidance: bool = Field(default=False, alias="activeGuidance")
    alibaba_cloud: AlibabaCloudOptions | None = Field(default=None, alias="alibabaCloud", repr=False)


class ForwardedProps(StrictModel):
    iac_code: IacCodeForwardedProps = Field(alias="iacCode")


def parse_run_input(payload: Any) -> RunAgentInput:
    try:
        return RunAgentInput.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("Invalid AG-UI RunAgentInput envelope.") from exc


def parse_forwarded_props(value: Any) -> ForwardedProps:
    try:
        return ForwardedProps.model_validate(value)
    except ValidationError as exc:
        raise AguiError("INVALID_INPUT", "Invalid iac-code forwarded properties.") from exc


def canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_tools(run_input: RunAgentInput) -> None:
    if run_input.tools:
        raise AguiError("INVALID_INPUT", "Client-provided tools are not supported.")


def resolve_cwd(raw_cwd: str) -> str:
    """Resolve a per-request workspace without allowing a symlink escape."""
    if not raw_cwd or not Path(raw_cwd).expanduser().is_absolute():
        raise AguiError("INVALID_INPUT", "The iac-code workspace must be an absolute path.")
    logical = os.path.normpath(os.path.expandvars(os.path.expanduser(raw_cwd)))
    try:
        resolved = Path(logical).resolve(strict=False)
    except OSError as exc:
        raise AguiError("INVALID_INPUT", "The iac-code workspace is invalid.") from exc

    roots = allowed_cwd_roots()
    if not any(_is_relative_to(resolved, root) for root in roots):
        raise AguiError("INVALID_INPUT", "The iac-code workspace is outside the allowed roots.")
    if resolved.exists() and not resolved.is_dir():
        raise AguiError("INVALID_INPUT", "The iac-code workspace is not a directory.")
    if not resolved.exists():
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AguiError("INVALID_INPUT", "The iac-code workspace cannot be created.") from exc
    return str(resolved)


def allowed_cwd_roots() -> list[Path]:
    raw = os.environ.get("IAC_CODE_AGUI_ALLOWED_CWDS") or os.environ.get("IACCODE_A2A_ALLOWED_CWDS")
    candidates = (
        [Path(item).expanduser() for item in raw.split(os.pathsep) if item]
        if raw
        else [Path.cwd(), Path(tempfile.gettempdir())]
    )
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir():
            roots.append(resolved)
    return roots


def latest_user_message(run_input: RunAgentInput) -> tuple[str, list[dict[str, Any]]] | None:
    """Return the newest user message as A2A wire parts."""
    for message in reversed(run_input.messages):
        if getattr(message, "role", None) != "user":
            continue
        message_id = str(message.id)
        content = message.content
        if isinstance(content, str):
            return message_id, [{"text": content}]
        if not isinstance(content, list):
            raise AguiError("INVALID_INPUT", "The user message content is invalid.")
        parts: list[dict[str, Any]] = []
        total_image_bytes = 0
        for raw_part in content:
            part: Any = raw_part
            part_type = getattr(part, "type", None)
            if part_type == "text":
                parts.append({"text": str(part.text)})
                continue
            if part_type != "image":
                raise AguiError("INVALID_INPUT", "Only text and inline data images are supported.")
            source = part.source
            if getattr(source, "type", None) != "data":
                raise AguiError("INVALID_INPUT", "Remote media URLs are not supported.")
            mime_type = str(source.mime_type).lower()
            if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
                raise AguiError("INVALID_INPUT", "The image media type is not supported.")
            try:
                raw = base64.b64decode(source.value.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
                raise AguiError("INVALID_INPUT", "The image data is not valid base64.") from exc
            if len(raw) > MAX_IMAGE_BYTES:
                raise AguiError("INVALID_INPUT", "An image exceeds the maximum size.")
            total_image_bytes += len(raw)
            if total_image_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise AguiError("INVALID_INPUT", "The total image content exceeds the maximum size.")
            try:
                resized = maybe_resize_and_downsample(raw)
            except Exception as exc:
                raise AguiError("INVALID_INPUT", "The image data is invalid.") from exc
            parts.append(
                {
                    "data": {
                        "filename": _image_filename(part, fallback=f"agui-image-{len(parts) + 1}"),
                        "bytes": base64.b64encode(resized.data).decode("ascii"),
                    },
                    "mediaType": resized.media_type,
                }
            )
        return message_id, parts
    return None


def _image_filename(part: Any, *, fallback: str) -> str:
    metadata = getattr(part, "metadata", None)
    if not isinstance(metadata, Mapping):
        return fallback
    filename = metadata.get("filename")
    if not isinstance(filename, str):
        return fallback
    try:
        if len(filename.encode("utf-8")) > MAX_IMAGE_FILENAME_BYTES:
            return fallback
        safe_filename = safe_artifact_filename(filename)
    except (UnicodeError, UnsafeArtifactNameError):
        return fallback
    return filename if safe_filename == filename else fallback


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
