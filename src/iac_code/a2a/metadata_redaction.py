from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from a2a.types import Message
from google.protobuf.json_format import MessageToDict, ParseDict


class A2AMetadataEchoRedactor:
    """Preserve canonical A2A message data while withholding provider headers."""

    def redact_message_echo(
        self,
        message: Message,
        *,
        public_path_roots: Iterable[Mapping[str, str]] | None = None,
    ) -> Message:
        del public_path_roots
        payload = MessageToDict(message, preserving_proto_field_name=False)
        _remove_llm_headers(payload.get("metadata"))
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
            _remove_llm_headers(metadata if isinstance(metadata, dict) else result)
        return result


def _remove_llm_headers(metadata: Any) -> None:
    if not isinstance(metadata, dict):
        return
    iac_code = metadata.get("iac_code")
    if isinstance(iac_code, dict):
        iac_code.pop("llm_headers", None)


def strip_llm_headers_from_metadata(metadata: Any) -> None:
    """Remove provider headers from dict or protobuf Struct metadata in place."""

    if isinstance(metadata, dict):
        _remove_llm_headers(metadata)
        return
    fields = getattr(metadata, "fields", None)
    if fields is None:
        return
    iac_code = fields.get("iac_code")
    if iac_code is None or iac_code.WhichOneof("kind") != "struct_value":
        return
    iac_fields = iac_code.struct_value.fields
    if "llm_headers" in iac_fields:
        del iac_fields["llm_headers"]
