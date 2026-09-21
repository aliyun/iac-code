from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from iac_code.resource_selector.export_ore_contract import export_contract
from iac_code.resource_selector.profiles import PROFILE_HASH, get_profile


def _browser_facts(root: Path) -> dict:
    selector_root = root / "src" / "iac_code" / "resource_selector"
    return {
        "schemaVersion": 1,
        "capabilities": json.loads((selector_root / "ore-capabilities.json").read_text(encoding="utf-8")),
        "operations": json.loads((selector_root / "ore-operations.json").read_text(encoding="utf-8")),
        "responseProjections": json.loads(
            (selector_root / "ore-response-projections.json").read_text(encoding="utf-8")
        ),
    }


def test_export_contract_records_exact_facts_and_complete_source_contract(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    facts_path = tmp_path / "ore-browser-facts.json"
    facts_path.write_text(json.dumps(_browser_facts(root), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_path = tmp_path / "ore-build-contract.json"

    contract = export_contract(browser_facts_path=facts_path, output_path=output_path)

    assert contract["browserFactsSha256"] == hashlib.sha256(facts_path.read_bytes()).hexdigest()
    assert contract["profileHash"] == PROFILE_HASH
    assert len(contract["selectors"]) == 116
    by_id = {item["selectorId"]: item for item in contract["selectors"]}
    assert by_id["ecs.managed_instance"]["metadataSchema"]["required"] == ["RegionId"]
    source = by_id["ess.eci_container"]["source"]
    assert source["selectorId"] == "ess.eci_scaling_configuration"
    assert source["sourceSchema"]["additionalProperties"] is False
    assert source["sourceSchema"]["properties"]["selector_id"] == {
        "const": "ess.eci_scaling_configuration"
    }
    assert source["sourceSchema"]["required"] == ["selector_id", "value"]
    assert by_id["redis.connection_url"]["source"]["sourceSchema"]["required"] == [
        "selector_id",
        "value",
    ]
    assert source["valueTransform"] == "singleton_list"
    assert source["metadataConflictPolicy"] == "equal_if_repeated"
    eci_container = get_profile("ess.eci_container")
    assert eci_container is not None
    assert set(source["operationParameters"]) == {
        operation.key for operation in eci_container.operations
    }
    assert all(value == "ScalingConfigurationId" for value in source["operationParameters"].values())
    assert json.loads(output_path.read_text(encoding="utf-8")) == contract

    derived = [selector for selector in contract["selectors"] if selector["source"] is not None]
    assert len(derived) == 16
    for selector in derived:
        profile = get_profile(selector["selectorId"])
        assert profile is not None
        assert selector["source"]["operationParameters"] == {
            operation.key: profile.source_parameter_for(operation.key)
            for operation in profile.operations
        }


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("capabilities", "capability facts"),
        ("operations", "operation facts"),
        ("responseProjections", "response projections"),
    ],
)
def test_export_contract_rejects_stale_browser_fact_projections(
    tmp_path: Path,
    section: str,
    message: str,
) -> None:
    root = Path(__file__).parents[2]
    facts = _browser_facts(root)
    if section == "capabilities":
        facts[section]["selectors"][0]["associationProperty"] = "ALIYUN::Changed"
    elif section == "operations":
        facts[section]["selectors"][0]["operations"][0]["action"] = "ChangedAction"
    else:
        facts[section]["schemaVersion"] = 999
    facts_path = tmp_path / "ore-browser-facts.json"
    facts_path.write_text(json.dumps(facts), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        export_contract(browser_facts_path=facts_path, output_path=tmp_path / "contract.json")


def test_export_contract_is_byte_deterministic(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    facts_path = tmp_path / "ore-browser-facts.json"
    facts_path.write_text(json.dumps(_browser_facts(root), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_a = tmp_path / "contract-a.json"
    output_b = tmp_path / "contract-b.json"

    export_contract(browser_facts_path=facts_path, output_path=output_a)
    export_contract(browser_facts_path=facts_path, output_path=output_b)

    assert output_a.read_bytes() == output_b.read_bytes()


def test_export_contract_rejects_boolean_schema_version(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    facts = _browser_facts(root)
    facts["schemaVersion"] = True
    facts_path = tmp_path / "ore-browser-facts.json"
    facts_path.write_text(json.dumps(facts), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported ORE browser facts schema"):
        export_contract(browser_facts_path=facts_path, output_path=tmp_path / "contract.json")


def test_profile_hash_json_golden_matches_python_byte_contract() -> None:
    golden = json.loads(
        (Path(__file__).parent / "profile-hash-golden.json").read_text(encoding="utf-8")
    )
    assert (
        json.dumps(golden["input"], ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        == golden["canonical"]
    )
