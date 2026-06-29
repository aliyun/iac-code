from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from a2a.types import Message
from google.protobuf.json_format import MessageToDict, ParseDict


class A2AMetadataEchoRedactor:
    REDACTED_VALUE = "***"
    _SENSITIVE_KEY_FRAGMENTS = {
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
        "security_token",
        "session",
        "signature",
        "token",
        "api_key",
        "access_key",
        "access_key_id",
        "access_key_secret",
    }

    def redact_message_echo(self, message: Message) -> Message:
        redacted_message = Message()
        redacted_message.CopyFrom(message)
        if not message.metadata.fields:
            return redacted_message

        metadata = MessageToDict(message.metadata, preserving_proto_field_name=False)
        redacted_message.metadata.Clear()
        ParseDict(self.redact(metadata), redacted_message.metadata)
        return redacted_message

    def redact(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: self.REDACTED_VALUE if self._is_sensitive_key(key) else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        return value

    def _is_sensitive_key(self, key: Any) -> bool:
        normalized = str(key).lower().replace("-", "_")
        compact = normalized.replace("_", "")
        return any(
            fragment in normalized or fragment.replace("_", "") in compact for fragment in self._SENSITIVE_KEY_FRAGMENTS
        )
