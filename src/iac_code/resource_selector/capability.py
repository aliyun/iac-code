"""Surface and artifact capability checks for resource selectors."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .profiles import PROFILE_HASH, iter_profiles

RESOURCE_SELECTOR_EXTENSION_URI = "urn:iac-code:resource-selector:v1"
A2A_RESOURCE_SELECTOR_ENV = "IAC_CODE_A2A_RESOURCE_SELECTOR_ENABLED"


def env_enabled(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def static_root() -> Path:
    return Path(__file__).resolve().parent.parent / "web" / "static"


@dataclass(frozen=True)
class ResourceSelectorCapability:
    enabled: bool
    surface: str
    profile_hash: str = PROFILE_HASH
    reason: str | None = None

    @classmethod
    def for_surface(
        cls,
        surface: Literal["web", "desktop", "a2a", "repl", "acp"],
        *,
        request_metadata: Any = None,
        artifact_root: Path | None = None,
    ) -> ResourceSelectorCapability:
        if surface not in {"web", "desktop", "a2a"}:
            return cls(False, surface, reason="surface_not_supported")
        if not tuple(iter_profiles(include_disabled=False)):
            return cls(False, surface, reason="no_enabled_profiles")
        if surface == "a2a":
            if not env_enabled(os.environ.get(A2A_RESOURCE_SELECTOR_ENV)):
                return cls(False, surface, reason="a2a_feature_disabled")
            advertised = request_capability(request_metadata)
            if advertised is None:
                return cls(False, surface, reason="client_capability_missing")
            if advertised.get("schemaVersion") != 1 or advertised.get("queryMode") != "ros_api_json":
                return cls(False, surface, reason="client_capability_incompatible")
            if advertised.get("profileHash") != PROFILE_HASH:
                return cls(False, surface, reason="profile_hash_mismatch")
            return cls(True, surface)

        root = artifact_root or static_root()
        manifest_path = root / "js" / "vendor" / "ore-resource-selector.manifest.json"
        bundle_path = root / "js" / "vendor" / "ore-resource-selector.min.js"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
        except (OSError, json.JSONDecodeError):
            return cls(False, surface, reason="bundle_missing")
        if manifest.get("sha256") != digest or manifest.get("profileHash") != PROFILE_HASH:
            return cls(False, surface, reason="bundle_manifest_mismatch")
        return cls(True, surface)


def request_capability(metadata: Any) -> dict[str, Any] | None:
    if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
        from google.protobuf.json_format import MessageToDict

        metadata = MessageToDict(metadata, preserving_proto_field_name=False)
    if not isinstance(metadata, Mapping):
        return None
    iac_code = metadata.get("iac_code")
    if not isinstance(iac_code, Mapping):
        return None
    capabilities = iac_code.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return None
    value = capabilities.get("resourceSelector")
    return dict(value) if isinstance(value, Mapping) else None
