"""Resolve the A2A execution mode from request metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.runtime_overrides import resolve_a2a_preferred_language
from iac_code.i18n import translate_message
from iac_code.pipeline.config import RunMode, get_run_mode


def resolve_request_run_mode(value: Any | None) -> RunMode:
    """Use the internal request override, falling back to the server mode."""

    metadata = getattr(value, "metadata", value)
    language = resolve_a2a_preferred_language(metadata) or "en"
    if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
        metadata = MessageToDict(metadata, preserving_proto_field_name=False)
    if isinstance(metadata, Mapping):
        iac_code = metadata.get("iac_code")
        if isinstance(iac_code, Mapping):
            if "run_mode" in iac_code:
                raw_mode = iac_code["run_mode"]
            elif "runMode" in iac_code:
                raw_mode = iac_code["runMode"]
            else:
                return get_run_mode()
            if not isinstance(raw_mode, str):
                raise InvalidParamsError(translate_message("Unsupported run mode.", language=language))
            try:
                return RunMode(raw_mode.strip().lower())
            except ValueError as exc:
                raise InvalidParamsError(translate_message("Unsupported run mode.", language=language)) from exc
    return get_run_mode()
