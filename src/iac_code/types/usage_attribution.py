"""Internal identity attached to the terminal outcome of one provider request."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageAttribution:
    logical_provider_key: str
    wire_provider_key: str
    telemetry_provider_name: str
    adapter_name: str | None
    requested_model: str
    actual_model: str
