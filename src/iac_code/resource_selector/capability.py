"""Surface and artifact capability checks for resource selectors."""

from __future__ import annotations

import hashlib
import json
import os
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
