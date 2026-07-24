from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from a2a.types import Message
from google.protobuf.json_format import MessageToDict, ParseDict

from iac_code.a2a.artifacts import sanitize_public_artifact_text
from iac_code.utils.public_errors import all_redaction_suppressed


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

    def redact_message_echo(
        self,
        message: Message,
        *,
        public_path_roots: Iterable[Mapping[str, str]] | None = None,
    ) -> Message:
        message_dict = MessageToDict(message, preserving_proto_field_name=False)
        metadata = message_dict.get("metadata")
        if isinstance(metadata, Mapping):
            message_dict["metadata"] = self.redact(metadata, public_path_roots=public_path_roots)
        parts = message_dict.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = sanitize_public_artifact_text(
                        part["text"],
                        fallback_summary="",
                        public_path_roots=public_path_roots,
                    )
        redacted_message = Message()
        ParseDict(message_dict, redacted_message)
        return redacted_message

    def redact(
        self,
        value: Any,
        *,
        public_path_roots: Iterable[Mapping[str, str]] | None = None,
    ) -> Any:
        if all_redaction_suppressed():
            # 环回 web 上下文：敏感键也不打 *** —— 整体原样返回。
            return value
        if isinstance(value, Mapping):
            return {
                key: self.REDACTED_VALUE
                if self._is_sensitive_key(key)
                else self.redact(item, public_path_roots=public_path_roots)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item, public_path_roots=public_path_roots) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item, public_path_roots=public_path_roots) for item in value]
        if isinstance(value, str):
            return sanitize_public_artifact_text(value, fallback_summary="", public_path_roots=public_path_roots)
        return value

    def _is_sensitive_key(self, key: Any) -> bool:
        normalized = str(key).lower().replace("-", "_")
        compact = normalized.replace("_", "")
        return any(
            fragment in normalized or fragment.replace("_", "") in compact for fragment in self._SENSITIVE_KEY_FRAGMENTS
        )
