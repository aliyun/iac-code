"""Request-scoped immutable provider ownership snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from iac_code.providers.base import Provider


@dataclass
class LeaseToken:
    state: str = "active"


@dataclass(frozen=True)
class ProviderRequestLease:
    request_id: str
    provider: Provider
    system_prompt: str
    requested_model: str
    logical_provider_key: str
    wire_provider_key: str
    telemetry_provider_name: str
    adapter_name: str | None
    context_window_model: str
    _owner_identity: Any = field(repr=False, compare=False)
    _lease_token: Any = field(repr=False, compare=False)
