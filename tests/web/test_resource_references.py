from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from iac_code.web.resource_references import ResourceReferenceStore, ResourceRevisionConflictError


def role(name="DeployRole", account="account-one", tool="tool-1", role_id="", **overrides):
    return {
        "resource_type": "ALIYUN::RAM::Role",
        "identifier": name,
        "identifier_kind": "name",
        "identifier_field": "RoleName",
        "evidence": "api_response",
        "account_scope": account,
        "console_partition": "aliyun",
        "sources": [{"tool_use_id": tool, "source_path": "/Role/RoleName"}],
        "aliases": (
            [{"identifier": role_id, "identifier_field": "RoleId", "identifier_kind": "id"}] if role_id else []
        ),
        **overrides,
    }


def instance(identifier="i-test111111", region="cn-hangzhou", account="account-one", tool="tool-1", **overrides):
    return {
        "resource_type": "ALIYUN::ECS::Instance",
        "identifier": identifier,
        "identifier_kind": "id",
        "identifier_field": "InstanceId",
        "evidence": "api_response",
        "region_id": region,
        "account_scope": account,
        "console_partition": "aliyun",
        "sources": [{"tool_use_id": tool, "source_path": "/Instances/Instance/0/InstanceId"}],
        **overrides,
    }


def nas_access_group(name="group-one", tool="group-tool", turn="turn-one", **overrides):
    return {
        "resource_type": "ALIYUN::NAS::AccessGroup",
        "identifier": name,
        "identifier_kind": "id",
        "identifier_field": "PhysicalResourceId",
        "evidence": "api_response",
        "region_id": "cn-hangzhou",
        "account_scope": "account-one",
        "console_partition": "aliyun",
        "attributes": {"AccessGroupType": "Vpc", "FileSystemType": "standard"},
        "sources": [{"tool_use_id": tool, "turn_id": turn, "source_path": "/AccessGroups/0/AccessGroupName"}],
        **overrides,
    }


def nas_access_rule(group="group-one", tool="rule-tool", turn="turn-one", **overrides):
    return {
        "resource_type": "ALIYUN::NAS::AccessRule",
        "identifier": "0",
        "identifier_kind": "id",
        "identifier_field": "PhysicalResourceId",
        "evidence": "api_response",
        "region_id": "cn-hangzhou",
        "account_scope": "account-one",
        "console_partition": "aliyun",
        "attributes": {
            "AccessRuleId": "0",
            "AccessGroupName": group,
            "FileSystemType": "standard",
        },
        "sources": [{"tool_use_id": tool, "turn_id": turn, "source_path": "/AccessRules/0/AccessRuleId"}],
        **overrides,
    }


@pytest.fixture
def store(tmp_path):
    return ResourceReferenceStore(tmp_path / "session" / "web-resource-references.json")


def test_account_role_merges_across_request_regions(store):
    first = role(region_id="cn-hangzhou")
    second = role(tool="tool-2", region_id="cn-beijing")
    first["sources"][0]["request_region_id"] = "cn-hangzhou"
    second["sources"][0]["request_region_id"] = "cn-beijing"
    store.observe([first, second])
    assert len(store.references) == 1
    reference = next(iter(store.references.values()))
    assert "region_id" not in reference
    assert len(reference["sources"]) == 2
    assert store.revision == 1


def test_name_later_gains_id_without_changing_reference_or_binding(store):
    store.observe([role(evidence="tool_input")])
    reference_id = next(iter(store.references))
    store.bind(
        source_key="message-1:digest",
        identifier="DeployRole",
        identifier_kind="name",
        reference_id=reference_id,
        expected_revision=1,
    )
    store.observe([role(role_id="123456789012345678")])
    assert list(store.references) == [reference_id]
    assert store.references[reference_id]["aliases"][0]["identifier"] == "123456789012345678"
    assert store.snapshot()["bindings"][0]["referenceId"] == reference_id
    assert len(store.references[reference_id]["sources"]) == 2


def test_distinct_account_region_type_and_parent_never_overwrite(store):
    store.observe(
        [
            role(),
            role(account="account-two"),
            instance(),
            instance(region="cn-beijing"),
            instance(account="account-two"),
        ]
    )
    for parent in ("service-one", "service-two"):
        store.observe(
            [
                {
                    **role(),
                    "resource_type": "ALIYUN::FC::Function",
                    "identifier_field": "FunctionName",
                    "region_id": "cn-hangzhou",
                    "attributes": {"ServiceName": parent},
                }
            ]
        )
    assert len(store.references) == 7


def test_nas_access_rule_uses_same_turn_parent_type_and_retracts_only_enrichment(store):
    store.observe([nas_access_group(), nas_access_rule()])

    rule = next(item for item in store.references.values() if item["resource_type"] == "ALIYUN::NAS::AccessRule")
    assert rule["attributes"]["AccessGroupType"] == "Vpc"
    assert {source["tool_use_id"] for source in rule["sources"]} == {"group-tool", "rule-tool"}

    store.retain_sources(lambda source: source.get("tool_use_id") != "group-tool")

    remaining = list(store.references.values())
    assert len(remaining) == 1
    assert remaining[0]["resource_type"] == "ALIYUN::NAS::AccessRule"
    assert "AccessGroupType" not in remaining[0]["attributes"]
    assert {source["tool_use_id"] for source in remaining[0]["sources"]} == {"rule-tool"}


def test_nas_access_rule_id_is_scoped_by_parent_access_group(store):
    store.observe([nas_access_rule(group="group-one"), nas_access_rule(group="group-two", tool="rule-tool-2")])

    rules = [item for item in store.references.values() if item["resource_type"] == "ALIYUN::NAS::AccessRule"]
    assert len(rules) == 2
    assert {item["attributes"]["AccessGroupName"] for item in rules} == {"group-one", "group-two"}


def test_same_name_alias_never_merges_distinct_id_addressed_resources(store):
    first = instance(
        "i-test111111",
        aliases=[
            {
                "identifier": "shared-instance-name",
                "identifier_field": "InstanceName",
                "identifier_kind": "name",
            }
        ],
    )
    second = instance(
        "i-test222222",
        aliases=[
            {
                "identifier": "shared-instance-name",
                "identifier_field": "InstanceName",
                "identifier_kind": "name",
            }
        ],
    )
    second["sources"][0]["source_path"] = "/Instances/Instance/1/InstanceId"

    store.observe([first, second])

    assert len(store.references) == 2
    references = {item["identifier"]: item for item in store.references.values()}
    assert set(references) == {"i-test111111", "i-test222222"}
    assert all(
        {alias["identifier"] for alias in reference["aliases"]} == {"shared-instance-name"}
        for reference in references.values()
    )
    assert all(
        other_id not in {alias["identifier"] for alias in reference["aliases"]}
        for current_id, reference in references.items()
        for other_id in references
        if other_id != current_id
    )


def test_unknown_accounts_remain_source_isolated(store):
    store.observe([role(account="", tool="tool-1"), role(account="", tool="tool-2")])
    assert len(store.references) == 2
    assert store.observe([role(account="", tool="tool-1")]) is None


def test_same_name_recreated_role_has_distinct_identity_and_old_binding(store):
    store.observe([role(role_id="111111111111111111")])
    old_id = next(iter(store.references))
    store.bind(
        source_key="old-message:hash",
        identifier="DeployRole",
        identifier_kind="name",
        reference_id=old_id,
        expected_revision=1,
    )
    store.observe([role(tool="tool-delete", status="DELETE_COMPLETE", evidence="tool_input")])
    store.observe([role(tool="tool-new", role_id="222222222222222222")])
    assert len(store.references) == 2
    assert store.references[old_id]["status"] == "DELETE_COMPLETE"
    assert store.snapshot()["bindings"][0]["referenceId"] == old_id


def lifecycle_role(status, observed_at):
    fact = role(status=status)
    fact["sources"][0].update(
        source_kind="stack_progress",
        source_request_digest="a" * 64,
        source_revision=("d" if status == "DELETE_COMPLETE" else "c") * 64,
        observed_at=observed_at,
    )
    return fact


def test_same_source_name_recreation_without_object_id_preserves_deleted_lifecycle(store):
    first = lifecycle_role("CREATE_COMPLETE", 100)
    deleted = lifecycle_role("DELETE_COMPLETE", 200)
    recreated = lifecycle_role("CREATE_COMPLETE", 300)
    store.observe([first])
    old_id = next(iter(store.references))
    store.bind(
        source_key="old-name",
        identifier="DeployRole",
        identifier_kind="name",
        reference_id=old_id,
        expected_revision=store.revision,
    )
    store.observe([deleted, recreated])
    assert len(store.references) == 2
    assert store.references[old_id]["status"] == "DELETE_COMPLETE"
    assert store.snapshot()["bindings"][0]["referenceId"] == old_id
    new_id = next(key for key in store.references if key != old_id)
    revision = store.revision
    assert store.observe([first, deleted, recreated]) is None
    assert store.revision == revision
    assert store.references[new_id]["status"] == "CREATE_COMPLETE"


@pytest.mark.asyncio
async def test_recreated_name_latest_progress_hydration_does_not_create_more_identities(store):
    store.observe([lifecycle_role("CREATE_COMPLETE", 100)])
    store.observe([lifecycle_role("DELETE_COMPLETE", 200), lifecycle_role("CREATE_COMPLETE", 300)])
    assert len(store.references) == 2
    snapshot = store.snapshot()
    await store.flush()
    restored = ResourceReferenceStore(store.path)
    for _ in range(3):
        assert restored.observe([lifecycle_role("CREATE_COMPLETE", 0)]) is None
        assert restored.snapshot()["items"] == snapshot["items"]
        assert restored.revision == snapshot["revision"]


def test_repeated_name_lifecycles_replay_in_order_and_out_of_order(store):
    observations = [lifecycle_role("DELETE_COMPLETE" if i % 2 else "CREATE_COMPLETE", 100 * (i + 1)) for i in range(5)]
    for observation in observations:
        store.observe([observation])
    assert len(store.references) == 3
    snapshot = store.snapshot()
    assert sorted(item["status"] for item in snapshot["items"]) == [
        "CREATE_COMPLETE",
        "DELETE_COMPLETE",
        "DELETE_COMPLETE",
    ]
    for observation in [*reversed(observations), *observations]:
        assert store.observe([observation]) is None
    assert store.snapshot() == snapshot


@pytest.mark.asyncio
async def test_repeated_identical_progress_retains_first_observation_across_restart(store):
    first = lifecycle_role("CREATE_COMPLETE", 100)
    store.observe([first, lifecycle_role("CREATE_COMPLETE", 150)])
    store.observe([lifecycle_role("DELETE_COMPLETE", 200), lifecycle_role("CREATE_COMPLETE", 300)])
    snapshot = store.snapshot()
    assert len(snapshot["items"]) == 2
    await store.flush()
    restored = ResourceReferenceStore(store.path)
    assert restored.observe([first]) is None
    assert restored.snapshot()["items"] == snapshot["items"]


def test_recurrent_progress_version_keeps_latest_status_without_one_contribution_per_poll(store):
    store.observe([lifecycle_role("CREATE_COMPLETE", 100)])
    update = lifecycle_role("UPDATE_COMPLETE", 200)
    update["sources"][0]["source_revision"] = "e" * 64
    store.observe([update, lifecycle_role("CREATE_COMPLETE", 300)])
    assert next(iter(store.references.values()))["status"] == "CREATE_COMPLETE"
    revision = store.revision
    for when in range(301, 401):
        assert store.observe([lifecycle_role("CREATE_COMPLETE", when)]) is None
    assert store.revision == revision
    facts = next(iter(store._contributions.values()))
    assert len(facts) == 3, "retain first/latest endpoints per version, not every unchanged poll"
    assert sorted(source["observed_at"] for fact in facts for source in fact["sources"]) == [100, 200, 400]


def test_later_name_input_does_not_move_a_deleted_objects_lifecycle_boundary(store):
    store.observe([lifecycle_role("CREATE_COMPLETE", 100), lifecycle_role("DELETE_COMPLETE", 200)])
    old_id = next(iter(store.references))
    later_input = role(tool="input-tool", evidence="tool_input")
    later_input["sources"][0]["observed_at"] = 400
    store.observe([later_input])
    store.observe([lifecycle_role("CREATE_COMPLETE", 300)])
    assert len(store.references) == 2
    assert store.references[old_id]["status"] == "DELETE_COMPLETE"
    assert next(value for key, value in store.references.items() if key != old_id)["status"] == "CREATE_COMPLETE"


def test_later_input_cannot_hide_an_earlier_status_observation_or_its_restoration(store):
    store.observe([lifecycle_role("CREATE_COMPLETE", 100)])
    later_input = role(tool="input-tool", evidence="tool_input")
    later_input["sources"][0]["observed_at"] = 400
    store.observe([later_input, lifecycle_role("DELETE_COMPLETE", 200)])
    old_id = next(iter(store.references))
    assert store.references[old_id]["status"] == "DELETE_COMPLETE"
    # Retraction rebuilds from contributions too, irrespective of arrival order.
    store.retain_sources(lambda source: source.get("source_revision") != "c" * 64)
    assert store.references[old_id]["status"] == "DELETE_COMPLETE"
    store.observe([lifecycle_role("CREATE_COMPLETE", 300)])
    assert len(store.references) == 2


def test_only_observation_timestamp_changes_do_not_increment_revision(store):
    first = role()
    first["sources"][0]["observed_at"] = 100
    store.observe([first])
    first["sources"][0]["observed_at"] = 200
    assert store.observe([first]) is None
    assert store.revision == 1


def test_older_replay_cannot_undo_deleted_status(store):
    deleted = role(status="DELETE_COMPLETE")
    deleted["sources"][0]["observed_at"] = 200
    store.observe([deleted])
    older = role(tool="tool-older", status="CREATE_COMPLETE")
    older["sources"][0]["observed_at"] = 100
    store.observe([older])
    assert len(store.references) == 1
    assert next(iter(store.references.values()))["status"] == "DELETE_COMPLETE"


def test_pagination_requires_a_consistent_revision(store):
    store.observe([instance(f"i-test{index:06d}") for index in range(8)])
    page1 = store.snapshot(limit=3)
    assert len(page1["items"]) == 3
    page2 = store.snapshot(cursor=page1["nextCursor"], limit=3)
    page3 = store.snapshot(cursor=page2["nextCursor"], limit=3)
    assert len(page3["items"]) == 2
    assert page3["nextCursor"] is None
    store.observe([instance("i-additional111")])
    with pytest.raises(ResourceRevisionConflictError):
        store.snapshot(cursor=page1["nextCursor"])


def test_binding_cannot_choose_another_identifier_and_requires_revision(store):
    store.observe([role()])
    reference_id = next(iter(store.references))
    with pytest.raises(ResourceRevisionConflictError):
        store.bind(
            source_key="message",
            identifier="DeployRole",
            identifier_kind="name",
            reference_id=reference_id,
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="resource_binding_invalid"):
        store.bind(
            source_key="message",
            identifier="OtherRole",
            identifier_kind="name",
            reference_id=reference_id,
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_name_only_snapshot_and_source_binding_survive_restart(store):
    store.observe([role()])
    reference_id = next(iter(store.references))
    store.bind(
        source_key="message-1:content-hash",
        identifier="DeployRole",
        identifier_kind="name",
        reference_id=reference_id,
        expected_revision=1,
    )
    assert await store.flush()
    restored = ResourceReferenceStore(store.path)
    assert restored.snapshot() == store.snapshot()
    assert restored.references[reference_id]["identifier_kind"] == "name"
    assert "regionId" not in restored.snapshot()["items"][0]


def test_restart_drops_previously_persisted_sentinel_id_reference(store):
    stale = instance("none")
    stale["resource_type"] = "ALIYUN::ECS::Disk"
    stale["identifier_field"] = "DiskId"
    stale["attributes"] = {"DiskId": "none"}
    stale["reference_id"] = "ref-" + "1" * 32
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": 1,
                "references": [stale],
                "contributions": {stale["reference_id"]: [stale]},
                "bindings": [],
                "preview_choices": [],
            }
        ),
        encoding="utf-8",
    )

    restored = ResourceReferenceStore(store.path)

    assert restored.references == {}
    assert restored.snapshot()["items"] == []


def test_restart_drops_legacy_reference_that_merged_distinct_primary_ids(store):
    stale = instance(
        "i-test111111",
        aliases=[
            {
                "identifier": "shared-instance-name",
                "identifier_field": "InstanceName",
                "identifier_kind": "name",
            },
            {
                "identifier": "i-test222222",
                "identifier_field": "InstanceId",
                "identifier_kind": "id",
            },
        ],
    )
    stale["reference_id"] = "ref-" + "2" * 32
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": 1,
                "references": [stale],
                "contributions": {stale["reference_id"]: [stale]},
                "bindings": [
                    {
                        "source_key": "message:digest",
                        "identifier": "i-test222222",
                        "identifier_kind": "id",
                        "reference_id": stale["reference_id"],
                    }
                ],
                "preview_choices": [],
            }
        ),
        encoding="utf-8",
    )

    restored = ResourceReferenceStore(store.path)

    assert restored.references == {}
    assert restored.bindings == {}


@pytest.mark.asyncio
async def test_write_failure_preserves_memory_and_can_retry(store):
    store.observe([role()])
    with patch("iac_code.web.resource_references.atomic_write_text", side_effect=OSError("private path")):
        assert not await store.flush()
    assert store.snapshot()["persistenceStatus"] == "write_failed"
    assert len(store.references) == 1
    assert await store.flush()
    assert ResourceReferenceStore(store.path).revision == store.revision


@pytest.mark.asyncio
async def test_unknown_version_is_not_overwritten(store):
    store.path.parent.mkdir(parents=True)
    content = json.dumps({"schema_version": 999, "future_data": "keep"})
    store.path.write_text(content, encoding="utf-8")
    restored = ResourceReferenceStore(store.path)
    restored.observe([role()])
    assert not await restored.flush()
    assert restored.snapshot()["persistenceStatus"] == "unsupported_version"
    assert store.path.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_corrupt_snapshot_can_rebuild_from_facts(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{invalid", encoding="utf-8")
    restored = ResourceReferenceStore(store.path)
    assert restored.persistence_status == "recovery_required"
    restored.observe([role()])
    assert await restored.flush()
    assert len(ResourceReferenceStore(store.path).references) == 1


def test_returned_snapshot_is_not_a_mutable_view(store):
    store.observe([role()])
    snapshot = store.snapshot()
    snapshot["items"][0]["identifier"] = "Changed"
    snapshot["items"][0]["attributes"]["RoleId"] = "fake"
    assert store.snapshot()["items"][0]["identifier"] == "DeployRole"
    assert store.snapshot()["items"][0]["attributes"] == {}


def nas_terraform_access_group(turn="turn-one"):
    from iac_code.tools.cloud.resource_references import stack_resource_reference

    reference = stack_resource_reference(
        {"ResourceType": "alicloud_nas_access_group", "PhysicalResourceId": "group-one:standard"},
        stack_id="offline-stack",
        region_id="cn-hangzhou",
        tool_use_id="stack-tool",
        source_path="/Resources/0",
        account_scope="account-one",
        console_partition="aliyun",
    )
    reference["sources"][0]["turn_id"] = turn
    return reference


def nas_observed_access_group(**overrides):
    parent = nas_access_group(**overrides)
    parent["sources"][0].update(product="nas", action="DescribeAccessGroups", evidence="api_response")
    return parent


def test_nas_terraform_group_joins_exact_detail_source_and_retracts_enrichment(store):
    store.observe([nas_terraform_access_group(), nas_observed_access_group()])
    child = next(item for item in store.references.values() if item["resource_type"] == "alicloud_nas_access_group")
    assert child["identifier"] == "group-one:standard"
    assert child["attributes"]["AccessGroupType"] == "Vpc"
    assert {source["tool_use_id"] for source in child["sources"]} == {"stack-tool", "group-tool"}
    store.retain_sources(lambda source: source.get("tool_use_id") != "group-tool")
    child = next(item for item in store.references.values() if item["resource_type"] == "alicloud_nas_access_group")
    assert "AccessGroupType" not in child["attributes"]
    assert child["attributes"]["FileSystemType"] == "standard"
    assert child["sources"][0]["tool_use_id"] == "stack-tool"


@pytest.mark.parametrize(
    "overrides",
    [
        {"turn": "other-turn"},
        {"account_scope": "other-account"},
        {"region_id": "cn-shanghai"},
        {"console_partition": "alibabacloud"},
        {"name": "other-group"},
        {"evidence": "tool_input"},
        {"attributes": {"AccessGroupType": "Vpc", "FileSystemType": "extreme"}},
    ],
)
def test_nas_terraform_group_does_not_borrow_other_response_context(store, overrides):
    store.observe([nas_terraform_access_group(), nas_observed_access_group(**overrides)])
    child = next(item for item in store.references.values() if item["resource_type"] == "alicloud_nas_access_group")
    assert "AccessGroupType" not in child["attributes"]


def test_nas_terraform_group_rejects_ambiguous_or_non_product_detail(store):
    first = nas_observed_access_group()
    second = nas_observed_access_group(
        tool="group-other", attributes={"AccessGroupType": "Classic", "FileSystemType": "standard"}
    )
    store.observe([first, second, nas_terraform_access_group()])
    child = next(item for item in store.references.values() if item["resource_type"] == "alicloud_nas_access_group")
    assert "AccessGroupType" not in child["attributes"]
    other = ResourceReferenceStore(store.path.parent / "other.json")
    parent = nas_observed_access_group()
    parent["sources"][0]["action"] = "CreateAccessGroup"
    other.observe([parent, nas_terraform_access_group()])
    child = next(item for item in other.references.values() if item["resource_type"] == "alicloud_nas_access_group")
    assert "AccessGroupType" not in child["attributes"]


@pytest.mark.parametrize("detail_first", [True, False])
def test_nas_terraform_group_separate_responses_join_in_either_order(store, detail_first):
    child_fact = nas_terraform_access_group()
    detail_fact = nas_observed_access_group()
    observations = [detail_fact, child_fact] if detail_first else [child_fact, detail_fact]
    for observation in observations:
        store.observe([observation])
    child = next(item for item in store.references.values() if item["resource_type"] == "alicloud_nas_access_group")
    assert child["identifier"] == "group-one:standard"
    assert child["attributes"]["AccessGroupType"] == "Vpc"
    store.retain_sources(lambda source: source.get("tool_use_id") != "group-tool")
    child = next(item for item in store.references.values() if item["resource_type"] == "alicloud_nas_access_group")
    assert "AccessGroupType" not in child["attributes"]
    assert child["sources"][0]["tool_use_id"] == "stack-tool"
