from __future__ import annotations

import hashlib
import json
from collections import Counter
from importlib.metadata import version
from importlib.resources import files

import pytest

from iac_code.tools.cloud.aliyun.ros_validation.action_policy import ACTION_POLICIES, validate_action_request
from iac_code.tools.cloud.aliyun.ros_validation.analyzer import ExpressionAnalyzer
from iac_code.tools.cloud.aliyun.ros_validation.count import (
    CountRewriteReason,
    fold_count_select,
    getatt_count_eligibility,
    ref_count_eligibility,
)
from iac_code.tools.cloud.aliyun.ros_validation.function_specs import (
    FUNCTION_SPECS,
    ExpressionContext,
    NoValueEffect,
)
from iac_code.tools.cloud.aliyun.ros_validation.model import Severity, TrustedRosAccountContext
from iac_code.tools.cloud.aliyun.ros_validation.resource_value_specs import (
    DEFAULT_RESOURCE_SPECS,
    _verify_catalog_checksum,
)
from iac_code.tools.cloud.aliyun.ros_validation.types import (
    ANY_VALUE,
    JSON_DECODED_VALUE,
    NO_VALUE,
    NULL,
    Compatibility,
    FloatCoercionOutcome,
    TypeKind,
    compatibility,
    float_coercion,
    infer_type,
    is_json_serializable_value,
    normalize,
    parse_json_parameter,
)

LOCKED_RUNTIME_FUNCTIONS = {
    "Fn::Add",
    "Fn::And",
    "Fn::Any",
    "Fn::Avg",
    "Fn::Base64",
    "Fn::Base64Decode",
    "Fn::Base64Encode",
    "Fn::Calculate",
    "Fn::Cidr",
    "Fn::Contains",
    "Fn::EachMemberIn",
    "Fn::Equals",
    "Fn::FindInMap",
    "Fn::FormatTime",
    "Fn::GetAZs",
    "Fn::GetAtt",
    "Fn::GetJsonValue",
    "Fn::GetStackOutput",
    "Fn::If",
    "Fn::Indent",
    "Fn::Index",
    "Fn::Join",
    "Fn::Jq",
    "Fn::Length",
    "Fn::ListMerge",
    "Fn::MarketplaceImage",
    "Fn::MatchPattern",
    "Fn::Max",
    "Fn::MemberListToMap",
    "Fn::MergeMap",
    "Fn::MergeMapToList",
    "Fn::Min",
    "Fn::Not",
    "Fn::Or",
    "Fn::Replace",
    "Fn::ResourceFacade",
    "Fn::Select",
    "Fn::SelectMapList",
    "Fn::Split",
    "Fn::Str",
    "Fn::Sub",
    "Fn::TransformNamespace",
    "Ref",
}


def test_function_registry_matches_locked_ros_registry_exactly() -> None:
    assert set(FUNCTION_SPECS) == LOCKED_RUNTIME_FUNCTIONS
    assert len(FUNCTION_SPECS) == len(set(FUNCTION_SPECS)) == 43
    assert {"Fn::Test", "Fn::ValueOf", "Fn::ValueOfAll"}.isdisjoint(FUNCTION_SPECS)
    assert FUNCTION_SPECS["Ref"].no_value_effect == NoValueEffect.PRESERVE
    assert FUNCTION_SPECS["Fn::If"].no_value_effect == NoValueEffect.CONDITIONAL
    assert FUNCTION_SPECS["Ref"].contracts_by_context[ExpressionContext.MODULE].implementation == "RefFactory"


@pytest.mark.parametrize("name", sorted(LOCKED_RUNTIME_FUNCTIONS))
def test_every_function_has_complete_contract_and_analyzer(name: str) -> None:
    spec = FUNCTION_SPECS[name]
    assert spec.short_tag
    assert spec.contracts_by_context
    assert spec.return_type.kind != TypeKind.UNKNOWN
    for contract in spec.contracts_by_context.values():
        assert contract.implementation
    if name not in {"Ref", "Fn::GetAtt", "Fn::If"}:
        handler = "_fn_{}".format(name.removeprefix("Fn::").replace("::", "_"))
        assert callable(getattr(ExpressionAnalyzer, handler, None)), name


def test_action_policy_registry_has_exactly_18_template_body_actions() -> None:
    assert len(ACTION_POLICIES) == 18
    assert set(ACTION_POLICIES) == {
        "ValidateTemplate",
        "CreateStack",
        "UpdateStack",
        "PreviewStack",
        "ContinueCreateStack",
        "CreateChangeSet",
        "GetTemplateEstimateCost",
        "GetTemplateSummary",
        "GetTemplateRecommendParameters",
        "GenerateTemplatePolicy",
        "GetTemplateParameterConstraints",
        "GetServiceProvisions",
        "ListStackOperationRisks",
        "CreateStackGroup",
        "UpdateStackGroup",
        "CreateTemplate",
        "UpdateTemplate",
        "RegisterResourceType",
    }


def test_action_policy_registry_is_built_from_locked_sdk_metadata_fixture() -> None:
    from alibabacloud_ros20190910 import models as ros_models

    fixture_path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath(
        "data/ros_template_body_action_policies.json"
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["sdk_package"] == "alibabacloud-ros20190910"
    assert payload["sdk_version"] == version("alibabacloud-ros20190910") == "3.6.0"
    assert {item["action"] for item in payload["actions"]} == set(ACTION_POLICIES)

    sdk_actions: set[str] = set()
    records = {item["action"]: item for item in payload["actions"]}
    for class_name in dir(ros_models):
        if not class_name.endswith("Request") or class_name.endswith("ShrinkRequest"):
            continue
        request_class = getattr(ros_models, class_name)
        try:
            request_fields = sorted(vars(request_class()))
        except TypeError:
            continue
        if "template_body" not in request_fields:
            continue
        action = class_name.removesuffix("Request")
        sdk_actions.add(action)
        digest = hashlib.sha256(json.dumps(request_fields, separators=(",", ":")).encode()).hexdigest()
        assert records[action]["request_fields_sha256"] == digest

    assert sdk_actions == set(ACTION_POLICIES)
    for action, policy in ACTION_POLICIES.items():
        record = records[action]
        assert policy.metadata_fields == frozenset(record["metadata_fields"])
        assert policy.allowed_fields == frozenset(record["allowed_fields"])
        assert policy.cardinality.value == record["cardinality"]
        assert policy.semantic_mode.value == record["semantic_mode"]
        assert policy.evaluation_mode.value == record["evaluation_mode"]
        assert policy.required_fields == frozenset(record["required_fields"])
        assert policy.conditional_predicates == tuple(record["conditional_predicates"])
        assert policy.active_body_predicate == record["active_body_predicate"]
        assert policy.known_differences == tuple(record["known_differences"])
        assert policy.evidence == tuple(record["evidence"])
        assert policy.contract_capability_id == payload["contract_capability_id"]


def test_action_policy_enforces_branch_constraints_and_reports_ignored_sources() -> None:
    _, change_set, _ = validate_action_request(
        "CreateChangeSet",
        {
            "TemplateScratchId": "scratch",
            "ChangeSetType": "IMPORT",
            "StackId": "existing-stack",
        },
    )
    assert any(item.severity == Severity.ERROR for item in change_set)

    _, recommend, active = validate_action_request(
        "GetTemplateRecommendParameters",
        {"TemplateBody": "body", "TemplateId": "id"},
    )
    assert active
    assert any(item.code == "ROS9003" and item.severity == Severity.WARNING for item in recommend)

    _, stack_group, _ = validate_action_request(
        "CreateStackGroup",
        {"StackArn": "arn", "PermissionModel": "SERVICE_MANAGED"},
    )
    assert any(item.severity == Severity.ERROR for item in stack_group)

    risks = ACTION_POLICIES["ListStackOperationRisks"]
    assert "TemplateScratchId" not in risks.allowed_fields
    _, invalid_operation, _ = validate_action_request(
        "ListStackOperationRisks",
        {"OperationType": "UpdateStack", "StackId": "stack"},
    )
    assert any(item.code == "ROS1201" and "OperationType" in item.summary for item in invalid_operation)


def test_action_policy_covers_update_preview_continue_and_reserved_module_rules() -> None:
    _, missing_target, _ = validate_action_request("UpdateTemplate", {"TemplateBody": "body"})
    assert any(item.code == "ROS1201" and "TemplateId" in item.summary for item in missing_target)

    _, valid_metadata_update, active = validate_action_request("UpdateTemplate", {"TemplateId": "template"})
    assert not valid_metadata_update
    assert not active

    _, preview_reuse, _ = validate_action_request("PreviewStack", {"StackId": "stack"})
    assert any(item.code == "ROS1201" and "Parameters" in item.summary for item in preview_reuse)

    _, invalid_mode, active = validate_action_request(
        "ContinueCreateStack",
        {"Mode": "Unexpected", "TemplateBody": "body"},
    )
    assert any(item.code == "ROS1201" and "Mode" in item.summary for item in invalid_mode)
    assert not active

    trusted = TrustedRosAccountContext(
        tenant_id="development",
        site_owner="alicloud",
        production_account_id="production",
        provenance="host-test",
    )
    _, reserved, _ = validate_action_request(
        "RegisterResourceType",
        {
            "TemplateBody": "body",
            "EntityType": "Module",
            "ResourceType": "MODULE::AliyunDev::Service::Resource",
        },
        trusted_ros_account_context=trusted,
    )
    assert any(item.code == "ROS1201" and "reserved for non-production" in item.summary for item in reserved)


def test_register_resource_type_account_context_is_an_explicit_limitation() -> None:
    params = {
        "TemplateBody": "body",
        "EntityType": "Module",
        "ResourceType": "MODULE::Example::Service::Resource",
    }
    _, without_context, _ = validate_action_request("RegisterResourceType", params)
    assert any(item.code == "ROS9101" and item.severity == Severity.LIMITATION for item in without_context)

    trusted = TrustedRosAccountContext(
        tenant_id="tenant",
        site_owner="owner",
        production_account_id="account",
        provenance="host-test",
    )
    _, with_context, _ = validate_action_request(
        "RegisterResourceType",
        params,
        trusted_ros_account_context=trusted,
    )
    assert not any(item.code == "ROS9101" for item in with_context)


def test_committed_resource_catalog_and_ref_types() -> None:
    path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_resource_value_specs.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _verify_catalog_checksum(payload)
    assert payload["schema_version"] == 4
    assert set(payload) == {
        "content_sha256",
        "datasource_ref_type_counts",
        "official_evidence_snapshot",
        "resources",
        "schema_version",
    }
    records = payload["resources"]
    assert len(records) == len({item["resource_type"] for item in records}) == 1349
    assert all(
        set(item)
        == {
            "attribute_types",
            "attributes",
            "attributes_complete",
            "known_differences",
            "official_evidence",
            "ref_type",
            "resource_type",
        }
        for item in records
    )
    assert sum(item["resource_type"].startswith("DATASOURCE::") for item in records) == 354
    assert payload["datasource_ref_type_counts"] == {
        "AnyValue": 1,
        "Integer | Null": 3,
        "List[Integer] | Null": 3,
        "List[Map] | Null": 4,
        "List[String] | Null": 193,
        "Map | Null": 4,
        "Null": 3,
        "String | Null": 143,
    }
    assert sum(bool(item["attributes"]) for item in records) == 1221
    assert sum(bool(item["attribute_types"]) for item in records) == 1221
    assert all(
        set(item["attribute_types"]) == set(item["attributes"])
        and set(item["attribute_types"].values())
        <= {
            "AnyValue",
            "Integer | Null",
            "List[Integer] | Null",
            "List[Map] | Null",
            "List[String] | Null",
            "Map | Null",
            "Null",
            "String | Null",
        }
        for item in records
    )
    assert Counter(item["official_evidence"][0]["status"] for item in records) == {
        "FOUND": 1292,
        "NOT_FOUND": 57,
    }
    assert all(item["official_evidence"] for item in records)
    for item in records:
        evidence = item["official_evidence"][0]
        required_evidence_fields = {
            "url",
            "resource_page_url",
            "locale",
            "status",
            "content_sha256",
            "extractor_version",
            "retrieved_at",
            "documented_type",
        }
        assert required_evidence_fields <= set(evidence)
        assert evidence["status"] in {"FOUND", "NOT_FOUND"}
        if evidence["status"] == "FOUND":
            assert evidence["snapshot_kind"] == "official-resource-detail"
            detail = payload["official_evidence_snapshot"]["resource_details"][item["resource_type"]]
            assert evidence["url"] == detail["url"]
            assert evidence["content_sha256"] == detail["content_sha256"]
        else:
            assert evidence["snapshot_kind"] == "official-resource-type-index"
            assert evidence["url"] == payload["official_evidence_snapshot"]["source_url"]
            assert evidence["content_sha256"] == payload["official_evidence_snapshot"]["content_sha256"]
        if evidence["status"] == "FOUND":
            assert evidence["documented_type"] == item["resource_type"]
            assert isinstance(evidence["resource_page_url"], str)
        else:
            assert evidence["documented_type"] is None
            assert evidence["resource_page_url"] is None
    gateway = next(item for item in records if item["resource_type"] == "ALIYUN::APIG::Gateway")
    assert "GatewayId" in gateway["attributes"]
    domain = next(item for item in records if item["resource_type"] == "ALIYUN::APIG::Domain")
    assert {"DomainId", "TlsCipherSuitesConfig"}.issubset(domain["attributes"])
    instance = next(item for item in records if item["resource_type"] == "ALIYUN::ECS::Instance")
    assert {"InstanceId", "SecurityGroupIds"}.issubset(instance["attributes"])
    zones = next(item for item in records if item["resource_type"] == "DATASOURCE::ECS::Zones")
    assert zones["attribute_types"]["ZoneIds"] == "List[String] | Null"
    package = next(item for item in records if item["resource_type"] == "ALIYUN::CDT::ResourcePackage")
    assert package["attributes_complete"] is True
    assert package["attribute_types"]["OrderId"] == "String | Null"
    monitor = next(item for item in records if item["resource_type"] == "ALIYUN::OSS::BucketAccessMonitor")
    assert monitor["attributes"] == []
    assert monitor["attributes_complete"] is True
    cleaner = next(item for item in records if item["resource_type"] == "ALIYUN::ROS::ResourceCleaner")
    assert cleaner["attributes_complete"] is False
    random_string = next(item for item in records if item["resource_type"] == "ALIYUN::RandomString")
    assert random_string["ref_type"] == "String | Null"
    assert random_string["official_evidence"]
    assert random_string["official_evidence"][0]["status"] == "NOT_FOUND"
    raw_ref_types = {item["resource_type"]: item["ref_type"] for item in records}
    assert {
        name: raw_ref_types[name]
        for name in {
            "ALIYUN::RandomString",
            "ALIYUN::ROS::Stack",
            "ALIYUN::ECS::PrepayInstance",
            "ALIYUN::RDS::PrepayDBInstance",
        }
    } == {
        "ALIYUN::RandomString": "String | Null",
        "ALIYUN::ROS::Stack": "String | Null",
        "ALIYUN::ECS::PrepayInstance": "List[String] | Null",
        "ALIYUN::RDS::PrepayDBInstance": "List[String] | Null",
    }


def test_resource_catalog_checksum_rejects_tampering() -> None:
    path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_resource_value_specs.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["resources"][0]["ref_type"] = "AnyValue"
    with pytest.raises(RuntimeError, match="content_sha256 mismatch"):
        _verify_catalog_checksum(payload)


@pytest.mark.parametrize(
    "field",
    ("effective_loader", "evidence", "source_kind", "source_revisions"),
)
def test_resource_catalog_rejects_internal_provenance_fields(field: str) -> None:
    path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_resource_value_specs.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["resources"][0][field] = "not-public-catalog-data"
    del payload["content_sha256"]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    payload["content_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(RuntimeError, match="invalid resource schema"):
        _verify_catalog_checksum(payload)


def test_resource_catalog_rejects_incomplete_official_evidence_even_with_valid_checksum() -> None:
    path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_resource_value_specs.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["resources"][0]["official_evidence"][0]["url"]
    del payload["content_sha256"]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    payload["content_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(RuntimeError, match="incomplete official evidence"):
        _verify_catalog_checksum(payload)


def test_committed_official_resource_index_snapshot_is_traceable() -> None:
    path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_official_resource_index.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source_url"] == (
        "https://help.aliyun.com/zh/ros/developer-reference/list-of-resource-types-by-service"
    )
    assert payload["locale"] == "zh-CN"
    assert len(payload["content_sha256"]) == 64
    assert payload["extractor_version"] == "official-resource-index-v1"
    assert len(payload["resources"]) == 1292
    assert set(payload["resource_details"]) == set(payload["resources"])
    assert all(
        detail["url"] == payload["resources"][resource_type]
        and detail["documented_type"] == resource_type
        and detail["extractor_version"] == "official-resource-detail-v1"
        and detail["normalization"] == "embedded-document-content-v1"
        and len(detail["content_sha256"]) == 64
        for resource_type, detail in payload["resource_details"].items()
    )
    assert payload["resources"]["ALIYUN::CDT::ResourcePackage"].endswith("aliyun-cdt-resourcepackage")


def test_official_detail_evidence_locks_three_known_drift_contracts() -> None:
    official_path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath(
        "data/ros_official_resource_index.json"
    )
    official = json.loads(official_path.read_text(encoding="utf-8"))
    details = official["resource_details"]
    fixture_types = {
        "DATASOURCE::CMS::Namespaces",
        "DATASOURCE::DTS::MigrationJobs",
        "DATASOURCE::ECS::ManagedInstances",
    }
    assert fixture_types < set(details)
    assert all(
        item["content_sha256"] != official["content_sha256"]
        and item["extractor_version"] == "official-resource-detail-v1"
        and item["normalization"] == "embedded-document-content-v1"
        for item in details.values()
    )
    assert details["DATASOURCE::CMS::Namespaces"]["observations"] == {
        "documented_members": ["CreateTime", "Namespace", "Specification", "Description", "ModifyTime"],
        "documented_type": "List[Map]",
        "output": "Namespaces",
    }
    assert details["DATASOURCE::DTS::MigrationJobs"]["observations"] == {
        "declared_outputs": ["DtsInstanceIds", "MigrationInstances"],
        "table_outputs": ["DtsInstanceIds", "SynchronizationInstances"],
    }
    assert details["DATASOURCE::ECS::ManagedInstances"]["observations"] == {
        "example_member_keys": ["TagKey", "TagValue"],
        "output": "Instances.Tags",
        "table_type": "Map",
    }

    catalog_path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_resource_value_specs.json")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    records = {item["resource_type"]: item for item in catalog["resources"]}
    expected_differences = {
        "DATASOURCE::CMS::Namespaces": (
            "official detail page documents Namespaces as List[Map], while locked runtime appends Namespace String "
            "values"
        ),
        "DATASOURCE::DTS::MigrationJobs": (
            "official detail table names SynchronizationInstances, while its declaration/examples and locked runtime "
            "use MigrationInstances"
        ),
        "DATASOURCE::ECS::ManagedInstances": (
            "official detail table declares Instances.Tags as Map with TagKey/TagValue, while locked runtime returns a "
            "List of Key/Value Maps"
        ),
    }
    for resource_type in fixture_types:
        detail = details[resource_type]
        evidence = records[resource_type]["official_evidence"][0]
        assert evidence["snapshot_kind"] == "official-resource-detail"
        assert evidence["url"] == detail["url"]
        assert evidence["content_sha256"] == detail["content_sha256"]
        assert evidence["observations"] == detail["observations"]
        assert expected_differences[resource_type] in records[resource_type]["known_differences"]


def test_resource_catalog_rejects_detail_evidence_that_uses_index_hash() -> None:
    path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_resource_value_specs.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = next(item for item in payload["resources"] if item["resource_type"] == "DATASOURCE::CMS::Namespaces")
    record["official_evidence"][0]["content_sha256"] = payload["official_evidence_snapshot"]["content_sha256"]
    del payload["content_sha256"]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    payload["content_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(RuntimeError, match="official detail evidence does not match"):
        _verify_catalog_checksum(payload)


def test_resource_catalog_reproduces_ref_and_attribute_contracts() -> None:
    path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_resource_value_specs.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(DEFAULT_RESOURCE_SPECS.specs) == {item["resource_type"] for item in payload["resources"]}

    assert str(DEFAULT_RESOURCE_SPECS.ref_type("ALIYUN::ECS::Instance")) == "String"
    assert str(DEFAULT_RESOURCE_SPECS.ref_type("ALIYUN::ECS::PrepayInstance")) == "List[String] | Null"
    assert str(DEFAULT_RESOURCE_SPECS.ref_type("DATASOURCE::DTS::MigrationJobs")) == "List[String] | Null"
    assert str(DEFAULT_RESOURCE_SPECS.ref_type("DATASOURCE::CMS::MonitorGroups")) == "List[Integer] | Null"
    assert str(DEFAULT_RESOURCE_SPECS.attribute_type("DATASOURCE::ECS::Zones", "ZoneIds")) == "List[String] | Null"
    assert (
        str(DEFAULT_RESOURCE_SPECS.ref_type("DATASOURCE::CEN::TransitRouters")) == "List[Map[String, AnyValue]] | Null"
    )
    assert str(DEFAULT_RESOURCE_SPECS.ref_type("DATASOURCE::ROS::StackInstance")) == "String | Null"
    assert str(DEFAULT_RESOURCE_SPECS.ref_type("DATASOURCE::ECS::ManagedInstances")) == "Null"
    assert str(DEFAULT_RESOURCE_SPECS.ref_type("DATASOURCE::Hologram::Instance")) == "Map[String, AnyValue] | Null"
    assert (
        DEFAULT_RESOURCE_SPECS.attribute_exists("ALIYUN::OSS::BucketAccessMonitor", "DefinitelyMissingAttribute")
        is False
    )
    assert (
        DEFAULT_RESOURCE_SPECS.attribute_exists(
            "ALIYUN::ROS::ResourceCleaner",
            "Detail:ECS:Instance:cn-hangzhou:i-123",
        )
        is None
    )
    assert DEFAULT_RESOURCE_SPECS.attribute_exists("ALIYUN::Unknown::Resource", "Anything") is None


def test_raw_content_properties_follow_locked_ros_schema_flags() -> None:
    assert DEFAULT_RESOURCE_SPECS.is_raw_content_property("ALIYUN::ROS::Stack", "TemplateBody")
    assert DEFAULT_RESOURCE_SPECS.is_raw_content_property("ALIYUN::ROS::StackGroup", "TemplateBody")
    assert not DEFAULT_RESOURCE_SPECS.is_raw_content_property("ALIYUN::ROS::Stack", "DynamicTemplateBody")
    assert not DEFAULT_RESOURCE_SPECS.is_raw_content_property("ALIYUN::ECS::Instance", "is_raw_content")


def test_count_raw_shape_position_and_select_fold() -> None:
    assert ref_count_eligibility(True, "Server").eligible
    assert ref_count_eligibility(False, "Server").reason == CountRewriteReason.POSITION
    assert ref_count_eligibility(True, {"Ref": "Name"}).reason == CountRewriteReason.RAW_SHAPE
    assert getatt_count_eligibility(True, ["Server", "PrivateIp"]).eligible
    assert not getatt_count_eligibility(True, [{"Ref": "Server"}, "PrivateIp"]).eligible

    def resolve_count_function(value: dict[str, object]) -> object:
        if value.get("Ref") == "Repeated":
            return ["a", "b", "c"]
        return None

    def resolve_lookup(value: dict[str, object]) -> object:
        return 1 if value.get("Ref") == "Index" else None

    folded = fold_count_select(
        [{"Ref": "Index"}, {"Ref": "Repeated"}, "fallback"],
        resolve_count_function,
        resolve_lookup,
    )
    assert folded.activated
    assert folded.transformed_node == "b"
    assert folded.deleted_node_indexes == (0, 1, 2)

    assert not fold_count_select(
        [{"Ref": "Index"}, {"Fn::FindInMap": ["M", "K", "V"]}],
        resolve_count_function,
        resolve_lookup,
    ).activated

    def resolve_numeric_string(value: dict[str, object]) -> object:
        return "1" if "Ref" in value else ["zero", "one"]

    assert not fold_count_select([{"Ref": "Index"}, {"Fn::FindInMap": []}], resolve_numeric_string).activated

    def resolve_boolean(value: dict[str, object]) -> object:
        return True if "Ref" in value else ["zero", "one"]

    boolean_fold = fold_count_select([{"Ref": "Index"}, {"Fn::FindInMap": []}], resolve_boolean)
    assert boolean_fold.activated
    assert boolean_fold.transformed_node == "one"


def test_raw_normalize_json_parameter_and_numeric_outcomes() -> None:
    assert normalize(b"\x00\xff") == [0, 255]
    assert infer_type(b"ab").kind == TypeKind.LIST
    assert infer_type(b"ab").item_type.kind == TypeKind.INTEGER
    assert compatibility(ANY_VALUE, NULL) == Compatibility.POSSIBLE_MATCH
    decoded, error = parse_json_parameter('{"items": [1, true, null]}')
    assert error is None
    assert decoded.type == JSON_DECODED_VALUE
    assert decoded.value == {"items": [1, True, None]}
    invalid, error = parse_json_parameter("NaN")
    assert invalid.poisoned
    assert error is not None
    assert float_coercion(10**10_000) == FloatCoercionOutcome.OVERFLOW
    assert float_coercion("nan") == FloatCoercionOutcome.NAN
    assert infer_type({b"binary-key": "value"}).key_type is not None
    assert infer_type({b"binary-key": "value"}).key_type.kind == TypeKind.BINARY
    assert compatibility(NO_VALUE, NULL) == Compatibility.DEFINITE_MATCH
    assert is_json_serializable_value({1: [True, None]})
    assert not is_json_serializable_value({b"binary-key": "value"})


def test_float_coercion_does_not_execute_opaque_user_objects() -> None:
    class ExplosiveFloat:
        def __float__(self) -> float:
            raise AssertionError("opaque __float__ must not run in the local validator")

    assert float_coercion(ExplosiveFloat()) == FloatCoercionOutcome.UNKNOWN
