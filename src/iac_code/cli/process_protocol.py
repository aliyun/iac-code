"""Protocol models for the CLI stream-json process mode."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProcessFrameValidationError(ValueError):
    code: str
    message: str
    request_id: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class SDKErrorPayload:
    code: str
    message: str
    retryable: bool = False
    error_id: str | None = None
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.error_id:
            data["error_id"] = self.error_id
        if self.data is not None:
            data["data"] = self.data
        return data


class SDKProcessRuntimeError(RuntimeError):
    """Runtime error that should be returned to SDK clients as a public error frame."""

    def __init__(self, payload: SDKErrorPayload) -> None:
        super().__init__(payload.message)
        self.payload = payload


@dataclass(frozen=True)
class SDKControlRequest:
    request_id: str
    subtype: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SDKControlResponse:
    request_id: str
    subtype: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SDKUserMessage:
    request_id: str | None
    session_id: str | None
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    parent_tool_use_id: str | None = None
    uuid: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class SDKUpdateEnvironmentVariables:
    variables: dict[str, str]


ProcessInputMessage = SDKControlRequest | SDKControlResponse | SDKUpdateEnvironmentVariables | SDKUserMessage


class ProcessFrameParser:
    """Parse stdin JSON lines into process-mode protocol objects."""

    def parse_line(self, line: str) -> ProcessInputMessage | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ProcessFrameValidationError("invalid_json", "Invalid JSON frame.") from exc
        if not isinstance(raw, dict):
            raise ProcessFrameValidationError("invalid_frame", "Frame must be a JSON object.")

        frame_type = raw.get("type")
        request_id = _optional_string(raw.get("request_id")) or _optional_string(raw.get("id"))
        if frame_type == "control_request":
            return self._parse_control_request(raw, request_id)
        if frame_type == "control_response":
            return self._parse_control_response(raw, request_id)
        if frame_type == "keep_alive":
            return None
        if frame_type == "update_environment_variables":
            return self._parse_update_environment_variables(raw, request_id)
        if frame_type == "control":
            return self._parse_legacy_control(raw, request_id)
        if frame_type == "initialize":
            return self._parse_legacy_initialize(raw, request_id)
        if frame_type == "close":
            return self._parse_legacy_close(raw, request_id)
        if frame_type == "user":
            return self._parse_user(raw, request_id)
        if frame_type == "user_message":
            return self._parse_legacy_user(raw, request_id)
        raise ProcessFrameValidationError("invalid_frame", f"Unsupported frame type: {frame_type!r}.", request_id)

    def _parse_control_request(self, raw: dict[str, Any], request_id: str | None) -> SDKControlRequest:
        if not request_id:
            raise ProcessFrameValidationError("invalid_frame", "control_request requires request_id.")
        request = raw.get("request")
        if not isinstance(request, dict):
            raise ProcessFrameValidationError("invalid_frame", "control_request.request must be an object.", request_id)
        subtype = _required_string(request.get("subtype"), "control_request.request.subtype", request_id)
        payload = dict(request)
        self._validate_payload_paths(payload, request_id)
        return SDKControlRequest(request_id=request_id, subtype=subtype, payload=payload)

    def _parse_control_response(self, raw: dict[str, Any], request_id: str | None) -> SDKControlResponse:
        response = raw.get("response")
        if not isinstance(response, dict):
            raise ProcessFrameValidationError(
                "invalid_frame",
                "control_response.response must be an object.",
                request_id,
            )
        response_request_id = _optional_string(response.get("request_id")) or request_id
        if not response_request_id:
            raise ProcessFrameValidationError(
                "invalid_frame", "control_response.response.request_id is required.", request_id
            )
        subtype = _required_string(response.get("subtype"), "control_response.response.subtype", response_request_id)
        return SDKControlResponse(request_id=response_request_id, subtype=subtype, payload=dict(response))

    def _parse_update_environment_variables(
        self, raw: dict[str, Any], request_id: str | None
    ) -> SDKUpdateEnvironmentVariables:
        variables = raw.get("variables")
        if not isinstance(variables, dict):
            raise ProcessFrameValidationError(
                "invalid_frame", "update_environment_variables.variables must be an object.", request_id
            )
        parsed: dict[str, str] = {}
        for key, value in variables.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ProcessFrameValidationError(
                    "invalid_frame", "update_environment_variables variables must be string pairs.", request_id
                )
            parsed[key] = value
        return SDKUpdateEnvironmentVariables(variables=parsed)

    def _parse_legacy_control(self, raw: dict[str, Any], request_id: str | None) -> SDKControlRequest:
        if not request_id:
            raise ProcessFrameValidationError("invalid_frame", "control frame requires id.")
        subtype = _required_string(raw.get("subtype"), "control.subtype", request_id)
        payload = {key: value for key, value in raw.items() if key not in {"type", "id"}}
        self._validate_payload_paths(payload, request_id)
        return SDKControlRequest(request_id=request_id, subtype=subtype, payload=payload)

    def _parse_legacy_initialize(self, raw: dict[str, Any], request_id: str | None) -> SDKControlRequest:
        if not request_id:
            raise ProcessFrameValidationError("invalid_frame", "initialize frame requires id.")
        raw_options = raw.get("options")
        options: dict[str, Any] = raw_options if isinstance(raw_options, dict) else {}
        payload = dict(options)
        self._validate_payload_paths(payload, request_id)
        return SDKControlRequest(request_id=request_id, subtype="initialize", payload=payload)

    def _parse_legacy_close(self, raw: dict[str, Any], request_id: str | None) -> SDKControlRequest:
        if not request_id:
            raise ProcessFrameValidationError("invalid_frame", "close frame requires id.")
        return SDKControlRequest(request_id=request_id, subtype="close", payload={"subtype": "close"})

    def _parse_user(self, raw: dict[str, Any], request_id: str | None) -> SDKUserMessage:
        message = raw.get("message")
        if not isinstance(message, dict):
            raise ProcessFrameValidationError("invalid_frame", "user.message must be an object.", request_id)
        if message.get("role") not in (None, "user"):
            raise ProcessFrameValidationError("invalid_frame", "user.message.role must be user.", request_id)
        metadata = _metadata(raw.get("metadata"), request_id)
        cwd = _metadata_cwd(metadata, request_id)
        return SDKUserMessage(
            request_id=request_id,
            session_id=_optional_string(raw.get("session_id")),
            text=_content_text(message.get("content"), request_id),
            metadata=metadata,
            cwd=cwd,
            parent_tool_use_id=_nullable_string(raw.get("parent_tool_use_id"), "parent_tool_use_id", request_id),
            uuid=_optional_string(raw.get("uuid")),
            timestamp=_optional_string(raw.get("timestamp")),
        )

    def _parse_legacy_user(self, raw: dict[str, Any], request_id: str | None) -> SDKUserMessage:
        metadata = _metadata(raw.get("metadata"), request_id)
        cwd = _metadata_cwd(metadata, request_id)
        return SDKUserMessage(
            request_id=request_id,
            session_id=_optional_string(raw.get("session_id")),
            text=_content_text(raw.get("content"), request_id),
            metadata=metadata,
            cwd=cwd,
            parent_tool_use_id=_nullable_string(raw.get("parent_tool_use_id"), "parent_tool_use_id", request_id),
            uuid=_optional_string(raw.get("uuid")),
            timestamp=_optional_string(raw.get("timestamp")),
        )

    def _validate_payload_paths(self, payload: dict[str, Any], request_id: str | None) -> None:
        cwd = payload.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise ProcessFrameValidationError("invalid_frame", "cwd must be a string.", request_id)
            _validate_absolute_cwd(cwd, request_id)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _nullable_string(value: Any, field_name: str, request_id: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ProcessFrameValidationError("invalid_frame", f"{field_name} must be a string or null.", request_id)


def _required_string(value: Any, field_name: str, request_id: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise ProcessFrameValidationError("invalid_frame", f"{field_name} must be a non-empty string.", request_id)
    return value


def _metadata(value: Any, request_id: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProcessFrameValidationError("invalid_frame", "metadata must be an object.", request_id)
    return value


def _metadata_cwd(metadata: dict[str, Any], request_id: str | None) -> str | None:
    iac_code = metadata.get("iac_code")
    if not isinstance(iac_code, dict):
        return None
    cwd = iac_code.get("cwd")
    if cwd is None:
        return None
    if not isinstance(cwd, str):
        raise ProcessFrameValidationError("invalid_frame", "metadata.iac_code.cwd must be a string.", request_id)
    _validate_absolute_cwd(cwd, request_id)
    return cwd


def _validate_absolute_cwd(cwd: str, request_id: str | None) -> None:
    if not os.path.isabs(os.path.expanduser(cwd)):
        raise ProcessFrameValidationError("invalid_frame", "cwd must be an absolute path.", request_id)


def _content_text(value: Any, request_id: str | None) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ProcessFrameValidationError("invalid_frame", "content must be text or text blocks.", request_id)
    parts: list[str] = []
    for block in value:
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise ProcessFrameValidationError("invalid_frame", "content blocks must be text blocks.", request_id)
        parts.append(block["text"])
    return "".join(parts)
