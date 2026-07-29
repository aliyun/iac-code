from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from a2a.types import Message
from google.protobuf.json_format import MessageToDict, ParseDict


class A2AMetadataEchoRedactor:
    """Compatibility wrapper that now preserves canonical A2A message data."""

    def redact_message_echo(
        self,
        message: Message,
        *,
        public_path_roots: Iterable[Mapping[str, str]] | None = None,
    ) -> Message:
        del public_path_roots
        result = Message()
        ParseDict(MessageToDict(message, preserving_proto_field_name=False), result)
        return result

    def redact(
        self,
        value: Any,
        *,
        public_path_roots: Iterable[Mapping[str, str]] | None = None,
    ) -> Any:
        del public_path_roots
        return copy.deepcopy(value)
