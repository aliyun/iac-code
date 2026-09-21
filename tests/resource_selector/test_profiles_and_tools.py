from __future__ import annotations

import asyncio
import json

import pytest

from iac_code.resource_selector.profiles import PROFILE_HASH, get_profile, iter_profiles
from iac_code.resource_selector.tools import ResolveCloudResourceSelectorTool, SelectCloudResourceTool
from iac_code.resource_selector.validation import (
    normalize_metadata,
    resolve_template_metadata_bindings,
    validate_answer_value,
    validate_source,
)
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import CloudResourceSelectionEvent


def test_profile_registry_is_complete_stable_and_only_publicly_executable_entries_are_enabled() -> None:
    profiles = iter_profiles()
    enabled = iter_profiles(include_disabled=False)
    disabled = [item for item in profiles if not item.enabled]
    assert len(profiles) == 117
    assert len(enabled) == 116
    assert len({item.selector_id for item in profiles}) == len(profiles)
    assert len({item.association_property for item in profiles}) == len(profiles)
    assert PROFILE_HASH.startswith("sha256:") and len(PROFILE_HASH) == 71
    assert all(item.operations and item.unsupported_reason is None for item in enabled)
    assert [(item.selector_id, item.unsupported_reason) for item in disabled] == [
        ("dashvector.cluster", "public_endpoint_unavailable")
    ]
    assert disabled[0].operations


@pytest.mark.asyncio
async def test_resolver_exact_alias_unknown_out_of_scope_and_conditional_output_kind() -> None:
    resolver = ResolveCloudResourceSelectorTool(lambda: "cn-hangzhou")

    exact = json.loads(
        (
            await resolver.execute(
                tool_input={
                    "association_property": "ALIYUN::ACR::Repo::RepoAttribute",
                    "association_property_metadata": {"Attribute": "repoId"},
                },
                context=ToolContext(),
            )
        ).content
    )
    assert exact["status"] == "resolved"
    assert exact["selector_id"] == "acr.repo_attribute"
    assert exact["output_kind"] == "resource_id"
    assert exact["normalized_metadata"] == {"RegionId": "cn-hangzhou", "Attribute": "repoId"}
    assert exact["next_tool"] == "select_cloud_resource"
    assert "association_property_metadata_schema" not in exact

    alias = json.loads(
        (
            await resolver.execute(tool_input={"association_property": "ALIYUN::ECS::Instance"}, context=ToolContext())
        ).content
    )
    assert alias["selector_id"] == "ecs.instance"

    unknown = json.loads(
        (
            await resolver.execute(tool_input={"association_property": "ALIYUN::Nope::Missing"}, context=ToolContext())
        ).content
    )
    assert unknown["status"] == "not_found"

    zone = json.loads(
        (
            await resolver.execute(tool_input={"association_property": "ALIYUN::ECS::ZoneId"}, context=ToolContext())
        ).content
    )
    assert zone["status"] == "known_but_out_of_scope"
    assert "WithAvailableResource" in zone["association_property_metadata_schema"]["properties"]

    dashvector = json.loads(
        (
            await resolver.execute(
                tool_input={"association_property": "ALIYUN::DashVector::Cluster::ClusterName"},
                context=ToolContext(),
            )
        ).content
    )
    assert dashvector["status"] == "unsupported_backend"
    assert dashvector["reason"] == "public_endpoint_unavailable"


@pytest.mark.asyncio
async def test_resolver_search_is_bounded_and_does_not_disclose_static_whitelist() -> None:
    resolver = ResolveCloudResourceSelectorTool()
    result = json.loads((await resolver.execute(tool_input={"query": "instance"}, context=ToolContext())).content)
    assert result["status"] == "ambiguous"
    assert 1 < len(result["candidates"]) <= 10
    assert "ALIYUN::" not in resolver.description
    assert "ecs.instance" not in resolver.description


@pytest.mark.asyncio
async def test_resolver_prefers_user_choice_and_recognizes_oos_github_account_aliases() -> None:
    resolver = ResolveCloudResourceSelectorTool(lambda: "cn-hangzhou")

    assert "user explicitly wants to choose" in resolver.description
    assert "Do not pre-list candidates" in resolver.description

    missing_platform = json.loads(
        (
            await resolver.execute(
                tool_input={"query": "已经授权给 OOS 的 GitHub 账号"},
                context=ToolContext(),
            )
        ).content
    )
    assert missing_platform["status"] == "selector_contract_invalid"
    assert missing_platform["selector_id"] == "oos.git_account"
    assert missing_platform["missing_required"] == ["Platform"]

    resolved = json.loads(
        (
            await resolver.execute(
                tool_input={
                    "query": "GitHub 账号 OOS 授权",
                    "association_property_metadata": {"Platform": "github"},
                },
                context=ToolContext(),
            )
        ).content
    )
    assert resolved["status"] == "resolved"
    assert resolved["selector_id"] == "oos.git_account"
    assert resolved["normalized_metadata"] == {"Platform": "github", "ShowUnbindCom": False}
    assert resolved["next_tool"] == "select_cloud_resource"


@pytest.mark.asyncio
async def test_resolver_progressively_discloses_hierarchical_oss_object_contract() -> None:
    resolver = ResolveCloudResourceSelectorTool(lambda: "cn-hangzhou")
    result = json.loads(
        (
            await resolver.execute(
                tool_input={"query": "OSS object", "product": "oss"},
                context=ToolContext(),
            )
        ).content
    )
    assert result["status"] == "resolved"
    assert result["selector_id"] == "oss.bucket_object"
    assert result["normalized_metadata"]["RegionId"] == "cn-hangzhou"
    assert result["interaction"] == {
        "kind": "hierarchical",
        "single_tool_call": True,
        "source_policy": "forbidden",
        "steps": ["select_bucket", "select_object"],
    }
    assert result["next_tool"] == "select_cloud_resource"
    assert "不要预先调用 oss.bucket" in result["usage_hint"]
    assert "association_property_metadata_schema" not in result

    object_in_known_bucket = json.loads(
        (
            await resolver.execute(
                tool_input={
                    "query": "OSS object",
                    "product": "oss",
                    "association_property_metadata": {"BucketName": "bucket-a"},
                },
                context=ToolContext(),
            )
        ).content
    )
    assert object_in_known_bucket["status"] == "resolved"
    assert object_in_known_bucket["selector_id"] == "oss.object"
    assert object_in_known_bucket["normalized_metadata"]["BucketName"] == "bucket-a"

    full = json.loads(
        (
            await resolver.execute(
                tool_input={
                    "association_property": "ALIYUN::OSS::Bucket::Object",
                    "detail_level": "full",
                },
                context=ToolContext(),
            )
        ).content
    )
    assert "Prefix" in full["association_property_metadata_schema"]["properties"]


@pytest.mark.asyncio
async def test_resolver_invalid_metadata_returns_copyable_repair_instead_of_invalid_normalized_metadata() -> None:
    resolver = ResolveCloudResourceSelectorTool(lambda: "cn-hangzhou")
    result = json.loads(
        (
            await resolver.execute(
                tool_input={
                    "association_property": "ALIYUN::OSS::Bucket::Object",
                    "association_property_metadata": {"BucketName": "bucket-a"},
                },
                context=ToolContext(),
            )
        ).content
    )
    assert result["status"] == "selector_contract_invalid"
    assert "normalized_metadata" not in result
    assert result["repair"]["remove_metadata"] == ["BucketName"]
    assert result["repair"]["retry_metadata"]["RegionId"] == "cn-hangzhou"
    assert "BucketName" not in result["repair"]["retry_metadata"]
    assert "association_property_metadata_schema" not in result

    invalid_known_field = json.loads(
        (
            await resolver.execute(
                tool_input={
                    "association_property": "ALIYUN::OSS::Bucket::Object",
                    "association_property_metadata": {"ObjectType": "not-an-object-type"},
                },
                context=ToolContext(),
            )
        ).content
    )
    issue_schema = invalid_known_field["association_property_metadata_schema"]
    assert set(issue_schema["properties"]) == {"ObjectType"}


def test_metadata_defaults_aliases_template_bindings_and_conditions() -> None:
    profile = get_profile("oss.bucket_object")
    assert profile is not None
    validation = normalize_metadata(profile, {"VPCId": "vpc-1"}, default_region_provider=lambda: "cn-shanghai")
    assert validation.normalized["RegionId"] == "cn-shanghai"
    assert validation.normalized["ValueType"] == "OSSUrl"
    assert validation.normalized["Multiple"] is False
    assert validation.invalid_parameters

    concrete, bindings, missing = resolve_template_metadata_bindings(
        {"RegionId": "${Region}", "Prefix": "$${Prefix}", "Delimiter": "${!literal}"},
        {"Region": "cn-beijing"},
    )
    assert concrete == {"RegionId": "cn-beijing", "Delimiter": "${literal}"}
    assert bindings == {"RegionId": {"parameter": "Region"}, "Prefix": {"parameter": "Prefix"}}
    assert missing == ("Prefix",)

    managed_instance = get_profile("ecs.managed_instance")
    assert managed_instance is not None
    invalid_enum = normalize_metadata(managed_instance, {"RegionId": "cn-hangzhou", "OsType": "macos"})
    assert any(item["path"] == "OsType" for item in invalid_enum.invalid_parameters)


@pytest.mark.parametrize(
    ("selector_id", "expected"),
    [
        ("cas.certificate", {"OrderType": "CERT"}),
        ("cr.repository", {"RepoStatus": "ALL"}),
        (
            "eds.bundle",
            {"SupportMultiSession": True, "BundleType": "CUSTOM", "ProtocolType": ""},
        ),
        (
            "emr.cluster",
            {
                "ClusterStates": [
                    "BOOTSTRAPPING",
                    "RUNNING",
                    "STARTING",
                    "START_FAILED",
                    "TERMINATED_WITH_ERRORS",
                    "TERMINATE_FAILED",
                    "TERMINATING",
                ]
            },
        ),
        ("oos.package", {"ShareType": "Public"}),
        ("oos.template", {"ShareType": "Public"}),
        ("oss.object", {"Delimiter": "/"}),
        ("service_catalog.product_version", {"Active": True}),
    ],
)
def test_component_defaults_are_materialized_in_trusted_metadata(
    selector_id: str,
    expected: dict[str, object],
) -> None:
    profile = get_profile(selector_id)
    assert profile is not None
    validation = normalize_metadata(profile, {}, default_region_provider=lambda: "cn-hangzhou")
    assert {key: validation.normalized[key] for key in expected} == expected


def test_derived_source_contract_and_region_fence() -> None:
    profile = get_profile("redis.connection_url")
    assert profile is not None
    source, error = validate_source(
        profile,
        {
            "selector_id": "redis.instance",
            "value": "r-test123",
            "association_property_metadata": {"RegionId": "cn-hangzhou"},
        },
        target_metadata={"RegionId": "cn-shanghai"},
    )
    assert source is None and error == "selector_source_mismatch"


@pytest.mark.parametrize("profile", list(iter_profiles()), ids=lambda profile: profile.selector_id)
def test_every_selector_rejects_an_invalid_control_character_value(profile) -> None:
    assert profile.value_pattern is not None
    assert validate_answer_value(profile, "invalid\x00value") == "selector_value_invalid"


def test_acr_repo_attribute_uses_the_selected_attribute_value_contract() -> None:
    profile = get_profile("acr.repo_attribute")
    assert profile is not None
    assert validate_answer_value(profile, "repo-id", metadata={"Attribute": "repoId"}) is None
    assert (
        validate_answer_value(profile, "repo id with spaces", metadata={"Attribute": "repoId"})
        == "selector_value_invalid"
    )
    assert validate_answer_value(profile, "registry.example.com/repo", metadata={"Attribute": "publicDomain"}) is None


@pytest.mark.asyncio
async def test_derived_source_metadata_is_authoritative_for_all_shared_context() -> None:
    tool = SelectCloudResourceTool()
    result = json.loads(
        (
            await tool.execute(
                tool_input={
                    "question": "请选择镜像标签",
                    "selector_id": "cr.repository_tag",
                    "association_property_metadata": {
                        "RegionId": "cn-hangzhou",
                        "InstanceId": "cri-target",
                        "RepoName": "repo-a",
                        "RepoNamespaceName": "namespace-a",
                    },
                    "source": {
                        "selector_id": "cr.repository",
                        "value": "repo-a",
                        "association_property_metadata": {
                            "RegionId": "cn-hangzhou",
                            "InstanceId": "cri-source",
                            "RepoName": "repo-a",
                            "RepoNamespaceName": "namespace-a",
                        },
                    },
                },
                context=ToolContext(),
            )
        ).content
    )
    assert result["status"] == "selector_source_mismatch"
    assert result["interaction"]["source_policy"] == "required"
    assert result["repair"]["expected_source_selector_id"] == "cr.repository"


@pytest.mark.asyncio
async def test_cloud_resource_source_mismatch_tells_model_to_remove_source_and_retry() -> None:
    tool = SelectCloudResourceTool(lambda: "cn-hangzhou")
    result = json.loads(
        (
            await tool.execute(
                tool_input={
                    "question": "请选择 OSS Object",
                    "selector_id": "oss.bucket_object",
                    "source": {"selector_id": "oss.bucket", "value": "bucket-a"},
                },
                context=ToolContext(),
            )
        ).content
    )
    assert result["status"] == "selector_source_mismatch"
    assert result["interaction"]["source_policy"] == "forbidden"
    assert result["repair"] == {"retry_same_selector": True, "remove_arguments": ["source"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("response_status", ["selected", "canceled"])
async def test_select_tool_emits_one_event_and_returns_structured_result(response_status: str) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    tool = SelectCloudResourceTool()
    task = asyncio.create_task(
        tool.execute(
            tool_input={
                "question": "请选择实例",
                "selector_id": "ecs.instance",
                "association_property_metadata": {"RegionId": "cn-hangzhou"},
            },
            context=ToolContext(event_queue=queue, tool_use_id="tool-1"),
        )
    )
    event = await queue.get()
    assert isinstance(event, CloudResourceSelectionEvent)
    assert event.selector_id == "ecs.instance"
    assert event.profile_hash == PROFILE_HASH
    assert event.response_future is not None
    response = {
        "status": response_status,
        "input_id": event.input_id,
        "selector_id": event.selector_id,
    }
    if response_status == "selected":
        response.update(value="i-test123", label="app-server")
    else:
        response["options_empty"] = True
    event.response_future.set_result(response)
    result = json.loads((await task).content)
    if response_status == "canceled":
        assert result["status"] == "canceled"
        assert result["reason"] == "user_canceled"
        assert result["should_retry"] is False
        assert result["options_empty"] is True
    else:
        assert "status" not in result
    if response_status == "selected":
        assert result["kind"] == "cloud_resource"
        assert result["value"] == "i-test123"
        assert result["region_id"] == "cn-hangzhou"


@pytest.mark.asyncio
async def test_select_tool_reports_unavailable_surface_without_claiming_user_canceled() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    tool = SelectCloudResourceTool()
    task = asyncio.create_task(
        tool.execute(
            tool_input={
                "question": "请选择实例",
                "selector_id": "ecs.instance",
                "association_property_metadata": {"RegionId": "cn-hangzhou"},
            },
            context=ToolContext(event_queue=queue, tool_use_id="tool-1"),
        )
    )
    event = await queue.get()
    assert isinstance(event, CloudResourceSelectionEvent)
    assert event.response_future is not None
    event.response_future.set_result(
        {
            "status": "selector_surface_unavailable",
            "input_id": event.input_id,
            "selector_id": event.selector_id,
        }
    )

    tool_result = await task
    result = json.loads(tool_result.content)
    assert tool_result.is_error is True
    assert result == {
        "schema_version": 1,
        "kind": "cloud_resource_selection",
        "status": "selector_surface_unavailable",
        "selector_id": "ecs.instance",
        "should_retry": False,
    }
