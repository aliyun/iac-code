from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from iac_code.resource_selector.capability import ResourceSelectorCapability, env_enabled, static_root
from iac_code.resource_selector.profiles import PROFILE_HASH, iter_profiles


def test_web_desktop_artifact_handshake_and_unsupported_surfaces(tmp_path) -> None:
    vendor = tmp_path / "js" / "vendor"
    vendor.mkdir(parents=True)
    bundle = b"export const ok = true;"
    (vendor / "ore-resource-selector.min.js").write_bytes(bundle)
    (vendor / "ore-resource-selector.manifest.json").write_text(
        json.dumps({"profileHash": PROFILE_HASH, "sha256": hashlib.sha256(bundle).hexdigest()}),
        encoding="utf-8",
    )
    assert ResourceSelectorCapability.for_surface("web", artifact_root=tmp_path).enabled
    assert ResourceSelectorCapability.for_surface("desktop", artifact_root=tmp_path).enabled
    assert ResourceSelectorCapability.for_surface("repl", artifact_root=tmp_path).reason == "surface_not_supported"
    (vendor / "ore-resource-selector.min.js").write_bytes(bundle + b"tampered")
    assert ResourceSelectorCapability.for_surface("web", artifact_root=tmp_path).reason == "bundle_manifest_mismatch"


def test_a2a_requires_env_and_exact_client_handshake(monkeypatch) -> None:
    metadata = {
        "iac_code": {
            "capabilities": {
                "resourceSelector": {
                    "schemaVersion": 1,
                    "queryMode": "ros_api_json",
                    "profileHash": PROFILE_HASH,
                }
            }
        }
    }
    monkeypatch.delenv("IAC_CODE_A2A_RESOURCE_SELECTOR_ENABLED", raising=False)
    assert ResourceSelectorCapability.for_surface("a2a", request_metadata=metadata).reason == "a2a_feature_disabled"
    monkeypatch.setenv("IAC_CODE_A2A_RESOURCE_SELECTOR_ENABLED", "true")
    assert ResourceSelectorCapability.for_surface("a2a", request_metadata=metadata).enabled
    assert not ResourceSelectorCapability.for_surface("a2a", request_metadata={}).enabled
    assert env_enabled("ON") and not env_enabled("invalid")


def test_bundled_manifest_matches_every_enabled_server_profile() -> None:
    root = static_root()
    vendor = root / "js" / "vendor"
    manifest = json.loads((vendor / "ore-resource-selector.manifest.json").read_text(encoding="utf-8"))
    bundle = (vendor / "ore-resource-selector.min.js").read_bytes()
    assert set(manifest) == {"bundleVersion", "schemaVersion", "profileHash", "sha256", "selectors"}
    assert manifest["profileHash"] == PROFILE_HASH
    assert manifest["sha256"] == hashlib.sha256(bundle).hexdigest()
    notices = (vendor / "ore-resource-selector.THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    assert "Permission is hereby granted" in notices
    assert "@ali/" not in notices
    assert re.search(r"(?m)^@?[^\s@]+(?:/[^\s@]+)?@\d+\.\d+", notices) is None
    assert not list(vendor.glob("ore-resource-selector*.map"))
    for forbidden in (b"sourceMappingURL", b"/Users/", b"contract-fixtures.json"):
        assert forbidden not in bundle

    bundled = {item["selectorId"]: item for item in manifest["selectors"]}
    enabled = {profile.selector_id: profile for profile in iter_profiles(include_disabled=False)}
    assert set(bundled) == set(enabled)
    for selector_id, profile in enabled.items():
        item = bundled[selector_id]
        assert item["associationProperty"] == profile.association_property
        assert item["oreAssociationProperty"] == profile.standalone_association_property
        assert set(item["metadataKeys"]) == set(profile.metadata_schema["properties"])
        assert item["operationKeys"] == [operation.key for operation in profile.operations]


def test_exported_resource_selector_metadata_has_no_internal_source_provenance() -> None:
    root = Path(__file__).parents[2]
    capabilities = json.loads(
        (root / "src/iac_code/resource_selector/ore-capabilities.json").read_text(encoding="utf-8")
    )
    operations = json.loads((root / "src/iac_code/resource_selector/ore-operations.json").read_text(encoding="utf-8"))
    fixtures = json.loads((root / "tests/resource_selector/contract-fixtures.json").read_text(encoding="utf-8"))
    cases = json.loads((root / "tests/resource_selector/e2e-cases.json").read_text(encoding="utf-8"))

    assert all(
        set(item)
        == {
            "selectorId",
            "associationProperty",
            "oreAssociationProperty",
            "selectionKind",
            "outputKind",
            "sourceSelectorId",
            "enabled",
            "metadata",
            "metadataKeys",
            "operationKeys",
        }
        for item in capabilities["selectors"]
    )
    assert set(operations) == {"schemaVersion", "counts", "selectors"}
    assert all(
        set(operation) == {"key", "requestKind", "product", "action", "allowedParameters"}
        for selector in operations["selectors"]
        for operation in selector["operations"]
    )
    assert all(set(fixture) == {"api", "response"} for fixture in fixtures["fixtures"].values())
    assert all(
        set(request)
        in (
            {"operationKey", "fixtureId", "parameters"},
            {"operationKey", "fixtureId", "parameters", "parameterVariants"},
        )
        for case in cases["cases"]
        for request in case["expectedRequests"]
    )
