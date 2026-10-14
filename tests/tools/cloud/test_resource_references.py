"""Resource fact contracts use only fake resources and explicit local sources."""

from __future__ import annotations

import pytest

from iac_code.tools.cloud.resource_references import (
    RESOURCE_REFERENCES_METADATA_KEY,
    resource_reference_metadata,
    resource_references_from_metadata,
    resource_spec,
    sanitize_resource_reference,
    stack_resource_reference,
)


def role_reference(**overrides):
    return {
        "provider": "aliyun",
        "resource_type": "ALIYUN::RAM::Role",
        "identifier": "DeployRole",
        "identifier_kind": "name",
        "identifier_field": "RoleName",
        "evidence": "api_response",
        "account_scope": "test-account",
        "sources": [{"tool_use_id": "tool-1", "source_path": "/Role/RoleName"}],
        **overrides,
    }


def test_name_only_contract_does_not_require_id_or_region():
    result = sanitize_resource_reference(role_reference(region_id="cn-hangzhou", scope_kind="regional"))
    assert result is not None
    assert result["scope_kind"] == "account"
    assert result["identifier_kind"] == "name"
    assert "region_id" not in result
    assert "physical_resource_id" not in result


@pytest.mark.parametrize(
    "identifier,file_system_type", [("group-one:0", "standard"), ("group-one:42:extreme", "extreme")]
)
def test_nas_terraform_composite_identity_retains_exact_parents(identifier, file_system_type):
    result = stack_resource_reference(
        {"ResourceType": "alicloud_nas_access_rule", "PhysicalResourceId": identifier},
        stack_id="offline-stack",
        region_id="cn-hangzhou",
        tool_use_id="offline",
        source_path="/Resources/0",
    )
    assert resource_spec("alicloud_nas_access_rule").resource_type == "ALIYUN::NAS::AccessRule"
    assert result["identifier"] == identifier.split(":")[1]
    assert result["attributes"] == {
        "AccessGroupName": "group-one",
        "AccessRuleId": identifier.split(":")[1],
        "FileSystemType": file_system_type,
    }


@pytest.mark.parametrize(
    "identifier,attributes",
    [
        ("0", {}),
        ("group:0:unknown", {}),
        ("group:0:extreme:extra", {}),
        ("group:0", {"AccessGroupName": "other"}),
        ("group:0", {"FileSystemType": "extreme"}),
    ],
)
def test_nas_terraform_composite_rejects_incomplete_or_conflicting_parent(identifier, attributes):
    assert (
        stack_resource_reference(
            {
                "ResourceType": "alicloud_nas_access_rule",
                "PhysicalResourceId": identifier,
                "ResourceAttributes": attributes,
            },
            stack_id="offline-stack",
            region_id="cn-hangzhou",
            tool_use_id="offline",
            source_path="/Resources/0",
        )
        is None
    )


def test_ebs_replica_group_exposes_api_group_name_alias():
    spec = resource_spec("ALIYUN::EBS::DiskReplicaGroup")
    assert spec is not None
    assert ("GroupName", "name") in spec.alias_fields


def test_sag_rule_preserves_only_reviewed_same_row_locators():
    result = sanitize_resource_reference(
        {
            "provider": "aliyun",
            "resource_type": "alicloud_sag_acl_rule",
            "identifier": "acr-fake",
            "identifier_kind": "id",
            "identifier_field": "AcrId",
            "region_id": "cn-fake",
            "evidence": "api_response",
            "attributes": {
                "AclId": "acl-fake",
                "Name": "rule-name",
                "Direction": "in",
                "Policy": "accept",
                "Priority": 1,
                "SecretKey": "fake-secret",
            },
            "sources": [{"tool_use_id": "tool-fake", "source_path": "/Acrs/Acr/0/AcrId"}],
        }
    )
    assert result is not None
    assert result["attributes"]["Name"] == "rule-name"
    assert result["attributes"]["AclId"] == "acl-fake"
    assert "fake-secret" not in str(result)


def test_metadata_is_versioned_and_whitelisted():
    metadata = {
        RESOURCE_REFERENCES_METADATA_KEY: resource_reference_metadata(
            [
                role_reference(
                    attributes={"RoleId": "123456789012345678", "Secret": "fake-secret"},
                    console_url="https://evil.example",
                    access_key_secret="fake-secret",
                ),
            ]
        )
    }
    references = resource_references_from_metadata(metadata)
    assert len(references) == 1
    assert references[0]["attributes"] == {"RoleId": "123456789012345678"}
    assert "fake-secret" not in str(references)
    assert "console_url" not in references[0]
    metadata[RESOURCE_REFERENCES_METADATA_KEY]["schema_version"] = 2
    assert resource_references_from_metadata(metadata) == []


@pytest.mark.parametrize("kind", [None, [], {}, "invented", "tool_result", "stack_progress", "attribute_query"])
@pytest.mark.parametrize("digest", [None, [], "https://invalid.example", "a" * 64])
def test_source_versions_require_complete_controlled_tuple(kind, digest):
    source = {
        "tool_use_id": "tool-1",
        "source_kind": kind,
        "source_request_digest": digest,
        "source_revision": "b" * 64,
    }
    clean = sanitize_resource_reference(role_reference(sources=[source]))
    valid = (
        isinstance(kind, str) and kind in {"tool_result", "stack_progress", "attribute_query"} and digest == "a" * 64
    )
    assert ("source_kind" in clean["sources"][0]) == valid
    assert ("source_request_digest" in clean["sources"][0]) == valid
    assert ("source_revision" in clean["sources"][0]) == valid


def test_malformed_aliases_and_unbounded_timestamps_cannot_break_collection():
    result = sanitize_resource_reference(
        role_reference(
            aliases=[{"identifier_field": "unreviewed", "identifier": "value"}],
            sources=[{"tool_use_id": "tool-1", "observed_at": 10**500}],
        )
    )
    assert result["aliases"] == []
    assert "observed_at" not in result["sources"][0]
    assert sanitize_resource_reference(role_reference(identifier_field="unreviewed", identifier_kind=None)) is None


@pytest.mark.parametrize("identifier", ["", "i-***", "${role}", "<role>", "role…", " role ", "a\nb", "https://x"])
def test_rejects_nonliteral_identifiers(identifier):
    assert sanitize_resource_reference(role_reference(identifier=identifier)) is None


@pytest.mark.parametrize("identifier", ["-", "n/a", "nil", "none", "NONE", "null"])
def test_rejects_sentinel_ids_without_reserving_user_names(identifier):
    id_reference = {
        "provider": "aliyun",
        "resource_type": "ALIYUN::ECS::Disk",
        "identifier": identifier,
        "identifier_kind": "id",
        "identifier_field": "DiskId",
        "evidence": "api_response",
        "region_id": "cn-hangzhou",
        "account_scope": "test-account",
        "sources": [{"tool_use_id": "tool-1", "source_path": "/Disks/Disk/0/DiskId"}],
    }
    assert sanitize_resource_reference(id_reference) is None
    assert sanitize_resource_reference(role_reference(identifier=identifier)) is not None


def test_rejects_id_reference_with_a_different_canonical_id_alias():
    reference = {
        "provider": "aliyun",
        "resource_type": "ALIYUN::ECS::Instance",
        "identifier": "i-test111111",
        "identifier_kind": "id",
        "identifier_field": "InstanceId",
        "evidence": "api_response",
        "region_id": "cn-hangzhou",
        "account_scope": "test-account",
        "aliases": [
            {
                "identifier": "shared-instance-name",
                "identifier_kind": "name",
                "identifier_field": "InstanceName",
            },
            {
                "identifier": "i-test222222",
                "identifier_kind": "id",
                "identifier_field": "InstanceId",
            },
        ],
        "sources": [{"tool_use_id": "tool-1", "source_path": "/Instances/Instance/0/InstanceId"}],
    }

    assert sanitize_resource_reference(reference) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "aws"},
        {"resource_type": "unknown"},
        {"sources": []},
        {"identifier_kind": "id"},
        {"identifier_field": "DisplayName"},
        {"identifier_field": {}},
        {"evidence": "invented"},
    ],
)
def test_rejects_invalid_contracts(overrides):
    assert sanitize_resource_reference(role_reference(**overrides)) is None


def test_alias_keeps_business_field_and_source_evidence():
    result = sanitize_resource_reference(
        role_reference(
            aliases=[
                {"identifier": "123456789012345678", "identifier_kind": "id", "identifier_field": "RoleId"},
                {"identifier": "unrelated", "identifier_kind": "name", "identifier_field": "DisplayName"},
            ],
            sources=[
                {"tool_use_id": "tool-1", "evidence": "tool_input"},
                {"tool_use_id": "tool-1", "source_path": "/Role", "evidence": "api_response"},
            ],
        )
    )
    assert result["aliases"] == [
        {"identifier": "123456789012345678", "identifier_kind": "id", "identifier_field": "RoleId"},
    ]
    assert result["sources"][0]["evidence"] == "tool_input"


def test_ros_attributes_use_actual_name_not_logical_name_and_keep_query_region():
    result = stack_resource_reference(
        {
            "ResourceType": "ALIYUN::RAM::Role",
            "PhysicalResourceId": "123456789012345678",
            "LogicalResourceId": "TemplateRole",
            "ResourceAttributes": [
                {"ResourceAttributeKey": "RoleName", "ResourceAttributeValue": "DeployRole"},
                {"ResourceAttributeKey": "RoleId", "ResourceAttributeValue": "123456789012345678"},
                {"ResourceAttributeKey": "PolicyDocument", "ResourceAttributeValue": "private-policy"},
            ],
        },
        stack_id="stack-1",
        region_id="cn-beijing",
        tool_use_id="tool-1",
        source_path="/Resources/0",
        account_scope="account-1",
    )
    assert result["identifier"] == "DeployRole"
    assert result["identifier_kind"] == "name"
    assert "region_id" not in result
    assert result["sources"][0]["request_region_id"] == "cn-beijing"
    assert result["logical_resource_id"] == "TemplateRole"
    assert "PolicyDocument" not in result["attributes"]


def test_opaque_ros_physical_id_does_not_become_role_name():
    result = stack_resource_reference(
        {"resource_type": "ALIYUN::RAM::Role", "physical_resource_id": "123456789012345678", "name": "LogicalRole"},
        stack_id="stack-1",
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        source_path="/resources/0",
    )
    assert result["identifier_field"] == "PhysicalResourceId"
    assert result["identifier"] != "LogicalRole"
    assert result["aliases"] == []


def test_ros_role_physical_name_keeps_table_locator_and_adds_verified_name_alias():
    result = stack_resource_reference(
        {"resource_type": "ALIYUN::RAM::Role", "physical_resource_id": "HivotStackDeletionRole", "name": "Role"},
        stack_id="stack-1",
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        source_path="/resources/0",
        account_scope="account-1",
        console_partition="aliyun",
    )
    assert result["identifier_field"] == "PhysicalResourceId"
    assert result["identifier_kind"] == "id"
    assert result["aliases"] == [
        {"identifier": "HivotStackDeletionRole", "identifier_field": "RoleName", "identifier_kind": "name"}
    ]


def test_verified_terraform_role_state_id_is_a_name():
    result = stack_resource_reference(
        {"resource_type": "alicloud_ram_role", "physical_resource_id": "DeployRole", "name": "logical"},
        stack_id="stack-1",
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        source_path="/resources/0",
    )
    assert result["identifier_kind"] == "name"
    assert result["identifier_field"] == "RoleName"
    assert result["identifier"] == "DeployRole"


@pytest.mark.parametrize(
    ("ros_type", "canonical_type", "identifier_field", "identifier"),
    [
        ("ALIYUN::ECS::VPC", "ALIYUN::VPC::VPC", "VpcId", "vpc-test123456"),
        ("ALIYUN::ECS::VSwitch", "ALIYUN::VPC::VSwitch", "VSwitchId", "vsw-test123456"),
    ],
)
def test_ros_network_resource_types_use_the_api_identity_contract(
    ros_type, canonical_type, identifier_field, identifier
):
    spec = resource_spec(ros_type)
    assert spec is not None and spec.resource_type == canonical_type
    result = stack_resource_reference(
        {"ResourceType": ros_type, "PhysicalResourceId": identifier, "LogicalResourceId": "Network"},
        stack_id="stack-1",
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        source_path="/Resources/0",
        account_scope="account-1",
        console_partition="aliyun",
    )
    assert result is not None
    assert result["identifier"] == identifier
    assert result["identifier_field"] == identifier_field
    assert result["identifier_kind"] == "id"


def test_ros_oss_physical_id_is_the_bucket_name_used_by_the_production_route():
    result = stack_resource_reference(
        {
            "ResourceType": "ALIYUN::OSS::Bucket",
            "PhysicalResourceId": "example-bucket",
            "LogicalResourceId": "Bucket",
        },
        stack_id="stack-1",
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        source_path="/Resources/0",
        account_scope="account-1",
        console_partition="aliyun",
    )
    assert result is not None
    assert result["identifier"] == "example-bucket"
    assert result["identifier_field"] == "BucketName"
    assert result["identifier_kind"] == "name"


@pytest.mark.parametrize(
    ("resource_type", "attributes", "expected_identity", "expected_result_field", "expected_parents"),
    [
        ("ALIYUN::OOS::Execution", {}, "PhysicalResourceId", "PhysicalResourceId", ()),
        (
            "ALIYUN::FNF::Flow",
            {"FlowName": "OrderFlow"},
            "PhysicalResourceId",
            "PhysicalResourceId",
            ("FlowName",),
        ),
        (
            "ALIYUN::ALB::BackendServerAttachment",
            {"ServerGroupId": "sgp-1"},
            "ServerGroupId",
            "ServerGroupId",
            (),
        ),
        (
            "alicloud_slb_listener",
            {"load_balancer_id": "lb-1", "ListenerId": "lb-1:http:80"},
            "ListenerId",
            "ListenerId",
            ("load_balancer_id",),
        ),
    ],
)
def test_ros_channel_catalog_types_become_bounded_stack_facts(
    resource_type, attributes, expected_identity, expected_result_field, expected_parents
):
    spec = resource_spec(resource_type)
    assert spec is not None
    assert spec.identifier_field == expected_identity
    assert spec.parent_fields == expected_parents
    result = stack_resource_reference(
        {
            "ResourceType": resource_type,
            "PhysicalResourceId": "physical-1",
            "LogicalResourceId": "LogicalResource",
            "ResourceAttributes": attributes,
        },
        stack_id="stack-1",
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        source_path="/Resources/0",
        account_scope="account-1",
        console_partition="aliyun",
    )
    assert result is not None
    assert result["identifier"] == attributes.get(expected_identity, "physical-1")
    assert result["identifier_field"] == expected_result_field
    assert result["attributes"] == attributes


def test_unknown_type_outside_the_ros_channel_catalog_stays_unsupported():
    assert resource_spec("ALIYUN::NOTREAL::Unknown") is None
    assert (
        stack_resource_reference(
            {"ResourceType": "ALIYUN::NOTREAL::Unknown", "PhysicalResourceId": "unknown-1"},
            stack_id="stack-1",
            region_id="cn-hangzhou",
            tool_use_id="tool-1",
            source_path="/Resources/0",
        )
        is None
    )


def test_generated_direct_api_identity_is_shared_with_terraform_alias():
    ros = resource_spec("ALIYUN::ECS::DeploymentSet")
    terraform = resource_spec("alicloud_ecs_deployment_set")
    assert ros is not None
    assert terraform is ros
    assert ros.identifier_field == "PhysicalResourceId"
    assert ros.identifier_kind == "id"


def test_image_detail_uses_the_observed_copy_destination_region():
    result = stack_resource_reference(
        {
            "ResourceType": "ALIYUN::ECS::ImageDetail",
            "PhysicalResourceId": "m-image-1",
            "ResourceAttributes": {"DestinationRegionId": "cn-shanghai"},
        },
        stack_id="stack-1",
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        source_path="/Resources/0",
        account_scope="account-1",
        console_partition="aliyun",
    )
    assert result is not None
    assert result["region_id"] == "cn-shanghai"
    assert result["attributes"] == {"DestinationRegionId": "cn-shanghai"}


@pytest.mark.parametrize("file_system_type", ["standard", "extreme", "cpfs"])
def test_nas_access_group_provider_id_preserves_raw_locator_and_source(file_system_type):
    identifier = "group-fixture:" + file_system_type
    result = stack_resource_reference(
        {
            "ResourceType": "alicloud_nas_access_group",
            "PhysicalResourceId": identifier,
            "LogicalResourceId": "NotTheGroupName",
        },
        stack_id="offline-stack",
        region_id="cn-hangzhou",
        tool_use_id="stack-tool",
        source_path="/Resources/0",
        account_scope="offline-account",
        console_partition="aliyun",
    )
    assert resource_spec("alicloud_nas_access_group") == resource_spec("ALIYUN::NAS::AccessGroup")
    assert result["resource_type"] == "alicloud_nas_access_group"
    assert result["identifier"] == identifier
    assert result["identifier_field"] == "PhysicalResourceId"
    assert result["attributes"] == {"AccessGroupName": "group-fixture", "FileSystemType": file_system_type}
    assert result["aliases"] == [
        {"identifier": "group-fixture", "identifier_field": "AccessGroupName", "identifier_kind": "name"}
    ]
    assert result["sources"][0]["source_path"] == "/Resources/0"
    assert result["sources"][0]["tool_use_id"] == "stack-tool"
    assert "AccessGroupType" not in result["attributes"]


@pytest.mark.parametrize(
    "identifier,attributes",
    [
        ("group-fixture", {}),
        ("group-fixture:unknown", {}),
        ("group-fixture:standard:extra", {}),
        (":standard", {}),
        ("0group:standard", {}),
        ("group-fixture:standard", {"AccessGroupName": "other"}),
        ("group-fixture:standard", {"FileSystemType": "extreme"}),
    ],
)
def test_nas_access_group_rejects_invalid_or_conflicting_provider_id(identifier, attributes):
    assert (
        stack_resource_reference(
            {
                "ResourceType": "alicloud_nas_access_group",
                "PhysicalResourceId": identifier,
                "ResourceAttributes": attributes,
            },
            stack_id="offline-stack",
            region_id="cn-hangzhou",
            tool_use_id="stack-tool",
            source_path="/Resources/0",
        )
        is None
    )


@pytest.mark.parametrize(
    "evidence,product,action",
    [
        ("tool_input", "ros", "ListStackResources"),
        ("api_response", "nas", "ListStackResources"),
        ("api_response", "ros", "CreateStack"),
        ("user_context", "ros", "GetStackResource"),
    ],
)
def test_nas_access_group_composite_projection_requires_original_stack_response(evidence, product, action):
    assert (
        sanitize_resource_reference(
            {
                "resource_type": "alicloud_nas_access_group",
                "identifier": "group-fixture:standard",
                "identifier_kind": "id",
                "identifier_field": "PhysicalResourceId",
                "evidence": evidence,
                "sources": [{"tool_use_id": "offline", "product": product, "action": action, "evidence": evidence}],
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "physical,attributes,valid",
    [
        ("offline-parent:offline_table", {}, True),
        ("offline-parent:offline_table", {"InstanceName": "wrong-parent"}, False),
        ("offline-parent:offline_table", {"TableName": "wrong_table"}, False),
        ("offline-parent:offline_table:extra", {}, False),
        ("offline-parent", {}, False),
        ("bad/parent:offline_table", {}, False),
        ("offline-parent:bad/table", {}, False),
    ],
)
def test_managed_ots_table_composite_id_is_source_bound(physical, attributes, valid):
    from iac_code.tools.cloud.resource_references import stack_resource_reference

    fact = stack_resource_reference(
        {
            "ResourceType": "alicloud_ots_table",
            "PhysicalResourceId": physical,
            "LogicalResourceId": "alicloud_ots_table.table",
            "ResourceAttributes": attributes,
        },
        stack_id="offline-stack",
        region_id="cn-hangzhou",
        tool_use_id="offline-tool",
        source_path="/result/Resources/0",
        account_scope="offline-account",
        console_partition="aliyun",
    )
    assert (fact is not None) is valid
    if valid:
        assert fact["identifier"] == physical
        assert fact["attributes"] == {"InstanceName": "offline-parent", "TableName": "offline_table"}
        assert fact["aliases"] == []
        assert fact["sources"][0]["stack_id"] == "offline-stack"


@pytest.mark.parametrize(
    "attributes",
    [
        {},
        {
            "FileSystemId": "offlinefs",
            "MountTargetDomain": "other-prefix.cn-hangzhou.nas.aliyuncs.com",
        },
    ],
)
def test_managed_nas_mount_target_preserves_composite_over_domain_attribute(attributes):
    from copy import deepcopy

    physical = "offlinefs:other-prefix.cn-hangzhou.nas.aliyuncs.com"
    row = {
        "ResourceType": "alicloud_nas_mount_target",
        "PhysicalResourceId": physical,
        "ResourceAttributes": attributes,
        "LogicalResourceId": "mount_target",
    }
    original = deepcopy(row)
    fact = stack_resource_reference(
        row,
        stack_id="offline-stack",
        region_id="cn-hangzhou",
        tool_use_id="offline-ros",
        source_path="/result/Resources/0",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    assert row == original
    assert fact["identifier"] == physical
    assert fact["identifier_field"] == "PhysicalResourceId"
    assert fact["attributes"] == {
        "FileSystemId": "offlinefs",
        "MountTargetDomain": "other-prefix.cn-hangzhou.nas.aliyuncs.com",
    }
    assert fact["sources"][0]["product"] == "ros"
    assert fact["sources"][0]["stack_id"] == "offline-stack"
    assert fact["sources"][0]["source_path"] == "/result/Resources/0"


@pytest.mark.parametrize(
    "physical,attributes",
    [
        ("offlinefs:other-prefix.cn-hangzhou.nas.aliyuncs.com", {"FileSystemId": "foreign"}),
        ("offlinefs:other-prefix.cn-hangzhou.nas.aliyuncs.com", {"MountTargetDomain": "foreign.nas.aliyuncs.com"}),
        ("offlinefs:other-prefix.cn-hangzhou.nas.aliyuncs.com:extra", {}),
        ("offlinefs:evil.example.com", {}),
        (":other-prefix.nas.aliyuncs.com", {}),
        ("bad/parent:other-prefix.nas.aliyuncs.com", {}),
        ("offlinefs:", {}),
        ("other-prefix.nas.aliyuncs.com", {"FileSystemId": "offlinefs"}),
    ],
)
def test_managed_nas_mount_target_rejects_conflicting_or_invalid_physical_id(physical, attributes):
    assert (
        stack_resource_reference(
            {
                "ResourceType": "alicloud_nas_mount_target",
                "PhysicalResourceId": physical,
                "ResourceAttributes": attributes,
            },
            stack_id="offline-stack",
            region_id="cn-hangzhou",
            tool_use_id="offline-ros",
            source_path="/result",
        )
        is None
    )


@pytest.mark.parametrize("field", ["PhysicalResourceId", "MountTargetDomain"])
@pytest.mark.parametrize("action", ["ListStackResources", "GetStackResource"])
def test_managed_nas_mount_target_legacy_field_projection_is_source_bound(field, action):
    from copy import deepcopy

    raw = {
        "resource_type": "alicloud_nas_mount_target",
        "identifier": "offlinefs:other-prefix.cn-hangzhou.nas.aliyuncs.com",
        "identifier_field": field,
        "identifier_kind": "id",
        "evidence": "api_response",
        "sources": [
            {
                "tool_use_id": "offline-ros",
                "product": "ros",
                "action": action,
                "evidence": "api_response",
                "stack_id": "offline-stack",
            }
        ],
    }
    original = deepcopy(raw)
    fact = sanitize_resource_reference(raw)
    assert raw == original
    assert fact["identifier"] == raw["identifier"]
    assert fact["identifier_field"] == "PhysicalResourceId"
    assert fact["attributes"]["FileSystemId"] == "offlinefs"
    assert fact["attributes"]["MountTargetDomain"] == "other-prefix.cn-hangzhou.nas.aliyuncs.com"
    assert fact["sources"] == raw["sources"]


@pytest.mark.parametrize(
    "evidence,product,action",
    [
        ("tool_input", "ros", "ListStackResources"),
        ("api_response", "nas", "ListStackResources"),
        ("api_response", "ros", "CreateStack"),
        ("user_context", "ros", "GetStackResource"),
    ],
)
def test_managed_nas_mount_target_composite_never_uses_unproved_sources(evidence, product, action):
    assert (
        sanitize_resource_reference(
            {
                "resource_type": "alicloud_nas_mount_target",
                "identifier": "offlinefs:target.nas.aliyuncs.com",
                "identifier_field": "MountTargetDomain",
                "identifier_kind": "id",
                "evidence": evidence,
                "sources": [{"tool_use_id": "offline", "product": product, "action": action, "evidence": evidence}],
            }
        )
        is None
    )


def test_native_nas_mount_domain_does_not_infer_parent_from_prefix():
    raw = {
        "resource_type": "ALIYUN::NAS::MountTarget",
        "identifier": "offlinefs-target.nas.aliyuncs.com",
        "identifier_field": "MountTargetDomain",
        "identifier_kind": "id",
        "evidence": "api_response",
        "sources": [
            {
                "tool_use_id": "offline-nas",
                "product": "nas",
                "action": "DescribeMountTargets",
                "evidence": "api_response",
            }
        ],
    }
    fact = sanitize_resource_reference(raw)
    assert fact["identifier"] == raw["identifier"]
    assert fact["attributes"] == {}
