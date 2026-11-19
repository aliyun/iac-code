from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from a2a.types import Message
from google.protobuf.json_format import MessageToDict, ParseDict

_NON_ECHOED_IAC_CODE_KEYS = (
    "llm_headers",
    "iac_code_api_key",
    "alibaba_cloud_access_key_id",
    "alibaba_cloud_access_key_secret",
    "alibaba_cloud_security_token",
)


class A2AMetadataEchoRedactor:
    """Copy A2A message data without provider headers or runtime credentials."""

    def redact_message_echo(
        self,
        message: Message,
        *,
        public_path_roots: Iterable[Mapping[str, str]] | None = None,
    ) -> Message:
        del public_path_roots
        payload = MessageToDict(message, preserving_proto_field_name=False)
        _remove_private_iac_metadata(payload.get("metadata"))
        result = Message()
        ParseDict(payload, result)
        return result

    def redact(
        self,
        value: Any,
        *,
        public_path_roots: Iterable[Mapping[str, str]] | None = None,
    ) -> Any:
        del public_path_roots
        result = copy.deepcopy(value)
        if isinstance(result, dict):
            metadata = result.get("metadata")
            _remove_private_iac_metadata(metadata if isinstance(metadata, dict) else result)
        return result


def _remove_private_iac_metadata(metadata: Any) -> None:
    if not isinstance(metadata, dict):
        return
    iac_code = metadata.get("iac_code")
    if isinstance(iac_code, dict):
        for key in _NON_ECHOED_IAC_CODE_KEYS:
            iac_code.pop(key, None)


def strip_llm_headers_from_metadata(metadata: Any) -> None:
    """Remove provider headers and runtime credentials from public metadata in place."""

    if isinstance(metadata, dict):
        _remove_private_iac_metadata(metadata)
        return
    fields = getattr(metadata, "fields", None)
    if fields is None:
        return
    iac_code = fields.get("iac_code")
    if iac_code is None or iac_code.WhichOneof("kind") != "struct_value":
        return
    iac_fields = iac_code.struct_value.fields
    for key in _NON_ECHOED_IAC_CODE_KEYS:
        if key in iac_fields:
            del iac_fields[key]
