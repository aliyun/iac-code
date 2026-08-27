"""Resolve the A2A execution mode from request metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from google.protobuf.json_format import MessageToDict

from iac_code.pipeline.config import RunMode, get_run_mode


def resolve_request_run_mode(value: Any | None) -> RunMode:
    """Use the internal request override, falling back to the server mode."""

    metadata = getattr(value, "metadata", value)
    if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
        metadata = MessageToDict(metadata, preserving_proto_field_name=False)
    if isinstance(metadata, Mapping):
        iac_code = metadata.get("iac_code")
        if isinstance(iac_code, Mapping):
            raw_mode = iac_code.get("run_mode") or iac_code.get("runMode")
            if isinstance(raw_mode, str):
                try:
                    return RunMode(raw_mode.strip().lower())
                except ValueError:
                    pass
    return get_run_mode()
