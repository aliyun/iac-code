from __future__ import annotations

import asyncio

import pytest

from iac_code.resource_selector.profiles import get_profile
from iac_code.resource_selector.query import (
    ResourceSelectorQueryError,
    ResourceSelectorQueryService,
    _acr_value,
    _adapt_server_parameters,
    _operation_region_id,
    _prepare_transport_request,
    project_ore_response,
)
from iac_code.tools.cloud.aliyun.oss_v4_adapter import OssOperationCatalog


def pending(selector_id: str, metadata: dict | None = None, source: dict | None = None) -> dict:
    return {
        "inputId": "resource-test",
        "selector": {
            "id": selector_id,
            "associationPropertyMetadata": metadata or {"RegionId": "cn-hangzhou"},
            "source": source,
        },
    }


@pytest.mark.parametrize(
    ("selector_id", "selected_region", "expected_endpoint_region"),
    [
        ("cloudsso.directory", "cn-hangzhou", "cn-shanghai"),
        ("cloudsso.directory", "cn-hongkong", "cn-hongkong"),
        ("cloudsso.directory", "ap-southeast-1", "cn-hongkong"),
        ("cloudsso.directory", "us-east-1", "us-west-1"),
        ("resource_manager.folder", "cn-hangzhou", "cn-shanghai"),
        ("resource_manager.folder", "ap-southeast-1", "ap-southeast-1"),
    ],
)
def test_global_control_plane_operations_use_a_trusted_service_region(
    selector_id: str,
    selected_region: str,
    expected_endpoint_region: str,
) -> None:
    profile = get_profile(selector_id)
    assert profile is not None
    operation = next(
        item for item in profile.operations if item.product.casefold() in {"cloudsso", "resourcedirectorymaster"}
    )
    assert (
        _operation_region_id(
            {"RegionId": selected_region},
            {"RegionId": selected_region},
            operation=operation,
        )
        == expected_endpoint_region
    )


@pytest.mark.asyncio
async def test_query_uses_server_fixed_product_action_metadata_and_projection() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        return {
            "Instances": {"Instance": [{"InstanceId": "i-1", "Secret": "drop"}]},
            "TotalCount": 1,
            "PrivateField": "drop",
        }

    result = await ResourceSelectorQueryService(caller).query(
        pending_payload=pending("ecs.instance", {"RegionId": "cn-hangzhou", "Status": "Running"}),
        operation_key="ecs.instance.list",
        dynamic_parameters={"MaxResults": 20, "InstanceName": "app"},
    )
    assert calls == [
        ("ecs", "DescribeInstances", "cn-hangzhou", {"Status": "Running", "MaxResults": 20, "InstanceName": "app"})
    ]
    assert result == {"Instances": {"Instance": [{"InstanceId": "i-1"}]}, "TotalCount": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "params", "error"),
    [
        ("ecs.delete", {}, "selector_operation_not_allowed"),
        ("ecs.instance.list", {"Action": "DeleteInstance"}, "selector_query_parameters_invalid"),
        ("ecs.instance.list", {"MaxResults": 500}, "selector_query_parameters_invalid"),
    ],
)
async def test_query_rejects_unknown_operation_and_browser_overrides(operation, params, error) -> None:
    async def caller(*_args):
        raise AssertionError("caller must not run")

    with pytest.raises(ResourceSelectorQueryError, match=error):
        await ResourceSelectorQueryService(caller).query(
            pending_payload=pending("ecs.instance"),
            operation_key=operation,
            dynamic_parameters=params,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector_id", "metadata", "operation_key", "response"),
    [
        (
            "oos.git_account",
            {"Platform": "github"},
            "oos.git_account.dataapi.oos.listgitaccounts",
            {"GitAccounts": [], "Count": 0},
        ),
        (
            "kms.key",
            {"RegionId": "cn-hangzhou"},
            "kms.key.dataapi.kms.listkeys",
            {"Keys": {"Key": []}, "TotalCount": 0},
        ),
    ],
)
async def test_successful_empty_selector_query_is_available_to_the_cancel_result(
    selector_id: str,
    metadata: dict,
    operation_key: str,
    response: dict,
) -> None:
    async def caller(*_args):
        return response

    service = ResourceSelectorQueryService(caller)
    pending_payload = pending(selector_id, metadata)
    assert service.options_empty(pending_payload=pending_payload) is None

    await service.query(
        pending_payload=pending_payload,
        operation_key=operation_key,
        dynamic_parameters={},
    )

    assert service.options_empty(pending_payload=pending_payload) is True
    service.discard(pending_payload=pending_payload)
    assert service.options_empty(pending_payload=pending_payload) is None


@pytest.mark.asyncio
async def test_cancel_reports_the_latest_selector_page_as_empty_after_an_earlier_nonempty_page() -> None:
    responses = [
        {"Instances": {"Instance": [{"InstanceId": "i-test123"}]}, "TotalCount": 1},
        {"Instances": {"Instance": []}, "TotalCount": 0},
    ]

    async def caller(*_args):
        return responses.pop(0)

    service = ResourceSelectorQueryService(caller)
    pending_payload = pending("ecs.instance", {"RegionId": "cn-hangzhou"})

    await service.query(
        pending_payload=pending_payload,
        operation_key="ecs.instance.list",
        dynamic_parameters={},
    )
    assert service.options_empty(pending_payload=pending_payload) is False

    await service.query(
        pending_payload=pending_payload,
        operation_key="ecs.instance.list",
        dynamic_parameters={},
    )

    assert service.options_empty(pending_payload=pending_payload) is True
    await service.validate_selection(pending_payload=pending_payload, value="i-test123")


@pytest.mark.asyncio
async def test_kms_alias_multi_api_supports_a_full_100_item_selector_page() -> None:
    calls = []
    active_calls = 0
    max_active_calls = 0

    async def caller(product, action, region_id, params):
        nonlocal active_calls, max_active_calls
        calls.append((product, action, region_id, params))
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        await asyncio.sleep(0.001)
        active_calls -= 1
        return {
            "Aliases": {
                "Alias": [
                    {
                        "KeyId": params["KeyId"],
                        "AliasName": "alias/{}".format(params["KeyId"]),
                    }
                ]
            }
        }

    requests = [
        {"parameters": {"RegionId": "cn-hangzhou", "KeyId": "key-{:03d}".format(index)}}
        for index in range(65)
    ]
    result = await ResourceSelectorQueryService(caller).query(
        pending_payload=pending("kms.key"),
        operation_key="kms.key.multiapi.kms.multiapi",
        dynamic_parameters={"requests": requests},
    )

    assert len(calls) == 65
    assert 1 < max_active_calls <= 8
    assert len(result) == 65
    assert result["0"]["Aliases"]["Alias"][0] == {
        "KeyId": "key-000",
        "AliasName": "alias/key-000",
    }
    assert result["64"]["Aliases"]["Alias"][0]["KeyId"] == "key-064"


@pytest.mark.asyncio
async def test_multi_api_rejects_more_than_one_full_selector_page_before_calling_cloud() -> None:
    calls = []

    async def caller(*args):
        calls.append(args)
        return {}

    requests = [
        {"parameters": {"RegionId": "cn-hangzhou", "KeyId": "key-{:03d}".format(index)}}
        for index in range(101)
    ]
    with pytest.raises(ResourceSelectorQueryError, match="selector_query_parameters_invalid"):
        await ResourceSelectorQueryService(caller).query(
            pending_payload=pending("kms.key"),
            operation_key="kms.key.multiapi.kms.multiapi",
            dynamic_parameters={"requests": requests},
        )

    assert calls == []


@pytest.mark.asyncio
async def test_compound_and_derived_queries_bind_source_without_accepting_api_overrides() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        return {"NetInfoItems": {"InstanceNetInfo": []}, "ignored": True}

    result = await ResourceSelectorQueryService(caller).query(
        pending_payload=pending(
            "redis.connection_url",
            source={"selector_id": "redis.instance", "value": "r-test123"},
        ),
        operation_key="redis.connection.list",
        dynamic_parameters={},
    )
    assert calls[0] == (
        "r-kvstore",
        "DescribeDBInstanceNetInfo",
        "cn-hangzhou",
        {"InstanceId": "r-test123"},
    )
    assert result == {"NetInfoItems": {"InstanceNetInfo": []}}

    with pytest.raises(ResourceSelectorQueryError, match="selector_query_parameters_invalid"):
        await ResourceSelectorQueryService(caller).query(
            pending_payload=pending("oss.bucket_object"),
            operation_key="oss.object.list",
            dynamic_parameters={"BucketName": "bucket-a", "product": "ecs"},
        )


@pytest.mark.asyncio
async def test_apig_domain_rebinds_and_validates_the_lower_camel_wire_source() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        return {"data": {"items": []}}

    payload = pending(
        "apig.domain",
        {"RegionId": "cn-hangzhou", "GatewayId": "gw-source"},
        source={"selector_id": "apig.gateway", "value": "gw-source"},
    )
    await ResourceSelectorQueryService(caller).query(
        pending_payload=payload,
        operation_key="apig.domain.dataapi.apig.listdomains",
        dynamic_parameters={"regionId": "cn-hangzhou", "gatewayId": "gw-source", "pageNumber": 1},
    )
    assert calls == [
        (
            "APIG",
            "ListDomains",
            "cn-hangzhou",
            {"regionId": "cn-hangzhou", "gatewayId": "gw-source", "pageNumber": 1},
        )
    ]

    with pytest.raises(ResourceSelectorQueryError, match="selector_source_mismatch"):
        await ResourceSelectorQueryService(caller).query(
            pending_payload=payload,
            operation_key="apig.domain.dataapi.apig.listdomains",
            dynamic_parameters={"regionId": "cn-hangzhou", "gatewayId": "gw-other", "pageNumber": 1},
        )


@pytest.mark.asyncio
async def test_computenest_service_version_rebinds_the_repeated_filter_source() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        return {"Services": []}

    payload = pending(
        "computenest.service_version",
        {"ServiceId": "service-source"},
        source={"selector_id": "computenest.service", "value": "service-source"},
    )
    await ResourceSelectorQueryService(caller).query(
        pending_payload=payload,
        operation_key="computenest.service_version.dataapi.computenest0521.listservices",
        dynamic_parameters={
            "RegionId": "cn-hangzhou",
            "AllVersions": True,
            "MaxResults": 20,
            "Filter.1.Name": "ServiceId",
            "Filter.1.Value.1": "service-source",
        },
    )
    assert calls[0][3]["Filter.1.Value.1"] == "service-source"
    assert "ServiceId" not in calls[0][3]

    with pytest.raises(ResourceSelectorQueryError, match="selector_source_mismatch"):
        await ResourceSelectorQueryService(caller).query(
            pending_payload=payload,
            operation_key="computenest.service_version.dataapi.computenest0521.listservices",
            dynamic_parameters={"Filter.1.Name": "ServiceId", "Filter.1.Value.1": "service-other"},
        )


@pytest.mark.asyncio
async def test_ehpc_mount_target_filters_the_response_without_sending_volume_id() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        return {
            "FileSystemList": {
                "FileSystems": [
                    {
                        "FileSystemId": "filesystem-source",
                        "MountTargetList": {"MountTargets": [{"MountTargetDomain": "source.example.com"}]},
                    },
                    {
                        "FileSystemId": "filesystem-other",
                        "MountTargetList": {"MountTargets": [{"MountTargetDomain": "other.example.com"}]},
                    },
                ]
            }
        }

    payload = pending(
        "ehpc.mount_target",
        {"RegionId": "cn-hangzhou", "VolumeId": "filesystem-source"},
        source={"selector_id": "ehpc.file_system", "value": "filesystem-source"},
    )
    service = ResourceSelectorQueryService(caller)
    await service.query(
        pending_payload=payload,
        operation_key="ehpc.mount_target.dataapi.ehpc.listfilesystemwithmounttargets",
        dynamic_parameters={"PageNumber": 1, "PageSize": 20},
    )
    assert "VolumeId" not in calls[0][3]
    with pytest.raises(ResourceSelectorQueryError, match="selector_value_not_observed"):
        await service.validate_selection(pending_payload=payload, value="other.example.com")
    await service.validate_selection(pending_payload=payload, value="source.example.com")


@pytest.mark.asyncio
async def test_cr_repository_tag_requires_repo_id_from_the_source_repository_lookup() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        if action == "GetRepository":
            return {"RepoId": "repo-resolved", "RepoName": "repository-source"}
        if action == "ListRepoTag":
            return {"Images": [{"Tag": "v1"}], "PageNo": 1, "PageSize": 1, "TotalCount": "1"}
        return {}

    payload = pending(
        "cr.repository_tag",
        {
            "RegionId": "cn-hangzhou",
            "InstanceId": "cri-instance",
            "RepoName": "repository-source",
            "RepoNamespaceName": "namespace-source",
        },
        source={"selector_id": "cr.repository", "value": "repository-source"},
    )
    service = ResourceSelectorQueryService(caller)
    with pytest.raises(ResourceSelectorQueryError, match="selector_parent_resource_not_observed"):
        await service.query(
            pending_payload=payload,
            operation_key="cr.repository_tag.dataapi.cr.listrepotag",
            dynamic_parameters={"RepoId": "repo-resolved", "PageNo": 1},
        )

    await service.query(
        pending_payload=payload,
        operation_key="cr.repository_tag.dataapi.cr.getrepository",
        dynamic_parameters={
            "InstanceId": "cri-instance",
            "RepoName": "repository-source",
            "RepoNamespaceName": "namespace-source",
        },
    )
    assert calls[-1][3]["RepoName"] == "repository-source"

    with pytest.raises(ResourceSelectorQueryError, match="selector_parent_resource_not_observed"):
        await service.query(
            pending_payload=payload,
            operation_key="cr.repository_tag.dataapi.cr.listrepotag",
            dynamic_parameters={"RepoId": "repo-other", "PageNo": 1},
        )
    await service.query(
        pending_payload=payload,
        operation_key="cr.repository_tag.dataapi.cr.listrepotag",
        dynamic_parameters={"RepoId": "repo-resolved", "PageNo": 1},
    )
    assert calls[-1][3] == {
        "RegionId": "cn-hangzhou",
        "InstanceId": "cri-instance",
        "RepoId": "repo-resolved",
        "PageNo": 1,
    }


@pytest.mark.asyncio
async def test_vswitch_instance_type_availability_is_a_fixed_read_only_operation() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        return {
            "AvailableZones": {
                "AvailableZone": [
                    {
                        "ZoneId": "cn-hangzhou-a",
                        "Secret": "drop",
                        "AvailableResources": {
                            "AvailableResource": [
                                {
                                    "Type": "InstanceType",
                                    "SupportedResources": {
                                        "SupportedResource": [{"Value": "ecs.g7.large", "Secret": "drop"}]
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        }

    result = await ResourceSelectorQueryService(caller).query(
        pending_payload=pending(
            "vpc.vswitch",
            {"RegionId": "cn-hangzhou", "InstanceType": "ecs.g7.large"},
        ),
        operation_key="vpc.instance_type.available",
        dynamic_parameters={},
    )
    assert calls == [
        (
            "ecs",
            "DescribeAvailableResource",
            "cn-hangzhou",
            {"DestinationResource": "InstanceType"},
        )
    ]
    assert result == {
        "AvailableZones": {
            "AvailableZone": [
                {
                    "ZoneId": "cn-hangzhou-a",
                    "AvailableResources": {
                        "AvailableResource": [
                            {
                                "Type": "InstanceType",
                                "SupportedResources": {"SupportedResource": [{"Value": "ecs.g7.large"}]},
                            }
                        ]
                    },
                }
            ]
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "resolver_action", "resolver_response", "resolver_params"),
    [
        (
            {"RegionId": "cn-hangzhou", "SecurityGroupId": "sg-test123"},
            "DescribeSecurityGroups",
            {"SecurityGroups": {"SecurityGroup": [{"SecurityGroupId": "sg-test123", "VpcId": "vpc-test123"}]}},
            {"RegionId": "cn-hangzhou", "SecurityGroupId": "sg-test123", "PageNumber": 1, "PageSize": 20},
        ),
        (
            {"RegionId": "cn-hangzhou", "SAENamespaceId": "cn-shanghai:test123"},
            "DescribeNamespaceResources",
            {"Data": {"NamespaceId": "cn-hangzhou:test123", "VpcId": "vpc-test123"}},
            {"NamespaceId": "cn-hangzhou:test123"},
        ),
    ],
)
async def test_vswitch_resolves_ore_parent_metadata_before_listing(
    metadata, resolver_action, resolver_response, resolver_params
) -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        if action == resolver_action:
            return resolver_response
        assert action == "DescribeVSwitches"
        return {"VSwitches": {"VSwitch": [{"VSwitchId": "vsw-test123", "VpcId": "vpc-test123"}]}}

    result = await ResourceSelectorQueryService(caller).query(
        pending_payload=pending("vpc.vswitch", metadata),
        operation_key="vpc.vswitch.list",
        dynamic_parameters={"PageNumber": 1, "PageSize": 20},
    )

    assert calls[0][1:] == (resolver_action, "cn-hangzhou", resolver_params)
    assert calls[1] == (
        "vpc",
        "DescribeVSwitches",
        "cn-hangzhou",
        {"VpcId": "vpc-test123", "PageNumber": 1, "PageSize": 20},
    )
    assert result["VSwitches"]["VSwitch"][0]["VSwitchId"] == "vsw-test123"


@pytest.mark.asyncio
async def test_vswitch_parent_resolution_failure_does_not_fall_back_to_all_vswitches() -> None:
    async def caller(_product, action, _region_id, _params):
        assert action == "DescribeSecurityGroups"
        return {"SecurityGroups": {"SecurityGroup": []}}

    with pytest.raises(ResourceSelectorQueryError, match="selector_parent_resource_not_found"):
        await ResourceSelectorQueryService(caller).query(
            pending_payload=pending(
                "vpc.vswitch",
                {"RegionId": "cn-hangzhou", "SecurityGroupId": "sg-missing"},
            ),
            operation_key="vpc.vswitch.list",
            dynamic_parameters={"PageNumber": 1, "PageSize": 20},
        )


@pytest.mark.asyncio
async def test_ecs_vswitch_accepts_only_the_vpc_observed_from_its_sae_parent() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        if action == "DescribeNamespaceResources":
            return {"Data": {"NamespaceId": "cn-hangzhou:test123", "VpcId": "vpc-test123"}}
        return {
            "VSwitches": {"VSwitch": [{"VSwitchId": "vsw-test123", "VpcId": "vpc-test123", "ZoneId": "cn-hangzhou-a"}]}
        }

    service = ResourceSelectorQueryService(caller)
    pending_payload = pending(
        "ecs.vswitch",
        {"RegionId": "cn-hangzhou", "SAENamespaceId": "cn-shanghai:test123"},
    )
    await service.query(
        pending_payload=pending_payload,
        operation_key="ecs.vswitch.dataapi.serverless.describenamespaceresources",
        dynamic_parameters={"NamespaceId": "cn-hangzhou:test123"},
    )
    result = await service.query(
        pending_payload=pending_payload,
        operation_key="ecs.vswitch.dataapi.vpc.describevswitches",
        dynamic_parameters={"RegionId": "cn-hangzhou", "VpcId": "vpc-test123", "PageSize": 20},
    )

    assert calls == [
        ("sae", "DescribeNamespaceResources", "cn-hangzhou", {"NamespaceId": "cn-hangzhou:test123"}),
        (
            "Vpc",
            "DescribeVSwitches",
            "cn-hangzhou",
            {"RegionId": "cn-hangzhou", "VpcId": "vpc-test123", "PageSize": 20},
        ),
    ]
    assert result["VSwitches"]["VSwitch"][0]["VSwitchId"] == "vsw-test123"

    with pytest.raises(ResourceSelectorQueryError, match="selector_parent_resource_not_observed"):
        await service.query(
            pending_payload=pending_payload,
            operation_key="ecs.vswitch.dataapi.vpc.describevswitches",
            dynamic_parameters={"RegionId": "cn-hangzhou", "VpcId": "vpc-untrusted", "PageSize": 20},
        )


@pytest.mark.asyncio
async def test_ecs_ui_metadata_constraints_are_enforced_by_the_bff() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        if action == "DescribeCloudAssistantStatus":
            return {
                "InstanceCloudAssistantStatusSet": {
                    "InstanceCloudAssistantStatus": [
                        {"InstanceId": "i-keep123", "CloudAssistantStatus": "true"},
                        {"InstanceId": "i-no-ip123", "CloudAssistantStatus": "true"},
                    ]
                }
            }
        return {
            "Instances": {
                "Instance": [
                    {
                        "InstanceId": "i-keep123",
                        "Platform": "Linux",
                        "OSType": "linux",
                        "PublicIpAddress": {"IpAddress": ["203.0.113.1"]},
                    },
                    {
                        "InstanceId": "i-no-ip123",
                        "Platform": "Linux",
                        "OSType": "linux",
                        "PublicIpAddress": {"IpAddress": []},
                    },
                    {
                        "InstanceId": "i-windows123",
                        "Platform": "Windows Server 2022",
                        "OSType": "windows",
                        "EipAddress": {"IpAddress": "203.0.113.2"},
                    },
                ]
            }
        }

    result = await ResourceSelectorQueryService(caller).query(
        pending_payload=pending(
            "ecs.instance",
            {
                "RegionId": "cn-hangzhou",
                "Platform": "Linux",
                "OSType": "linux",
                "PublicIpRequired": True,
                "OnlyCloudAssistantExecutable": True,
            },
        ),
        operation_key="ecs.instance.list",
        dynamic_parameters={"MaxResults": 20},
    )
    assert calls == [
        ("ecs", "DescribeInstances", "cn-hangzhou", {"MaxResults": 20}),
        (
            "ecs",
            "DescribeCloudAssistantStatus",
            "cn-hangzhou",
            {"InstanceId": ["i-keep123"]},
        ),
    ]
    assert result["Instances"]["Instance"] == [{"InstanceId": "i-keep123"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dynamic_parameters",
    [
        {"InstanceId": "i-browser", "PageNumber": 1, "PageSize": 20},
        {"Tag": {"prefix": "Tag", "__type__": "__RepeatList__", "list": [{"Key": "x"}]}},
    ],
)
async def test_browser_cannot_inject_absent_disk_metadata_or_component_owned_tags(
    dynamic_parameters: dict,
) -> None:
    async def caller(*_args):
        pytest.fail("invalid browser parameters must be rejected before an API call")

    with pytest.raises(ResourceSelectorQueryError, match="selector_query_parameters_invalid"):
        await ResourceSelectorQueryService(caller).query(
            pending_payload=pending("ecs.disk", {"RegionId": "cn-hangzhou"}),
            operation_key="ecs.disk.dataapi.ecs.describedisks",
            dynamic_parameters=dynamic_parameters,
        )


@pytest.mark.asyncio
async def test_generated_operation_projection_drops_unrelated_and_wrongly_nested_ids() -> None:
    async def caller(*_args):
        return {
            "Disks": {"Disk": [{"DiskId": "d-observed", "DiskName": "data"}]},
            "Unexpected": {"DiskId": "d-forged"},
            "PrivateField": "drop",
        }

    service = ResourceSelectorQueryService(caller)
    payload = pending("ecs.disk", {"RegionId": "cn-hangzhou"})
    result = await service.query(
        pending_payload=payload,
        operation_key="ecs.disk.dataapi.ecs.describedisks",
        dynamic_parameters={"PageNumber": 1, "PageSize": 20},
    )
    assert result == {"Disks": {"Disk": [{"DiskId": "d-observed", "DiskName": "data"}]}}
    await service.validate_selection(pending_payload=payload, value="d-observed")
    with pytest.raises(ResourceSelectorQueryError, match="selector_value_not_observed"):
        await service.validate_selection(pending_payload=payload, value="d-forged")


@pytest.mark.asyncio
async def test_selection_must_be_observed_and_still_exist_on_confirmation() -> None:
    responses = [
        {"Instances": {"Instance": [{"InstanceId": "i-test123"}]}},
        {"Instances": {"Instance": []}},
    ]

    async def caller(*_args):
        return responses.pop(0)

    now = [100.0]
    service = ResourceSelectorQueryService(caller, clock=lambda: now[0])
    pending_payload = pending("ecs.instance")
    with pytest.raises(ResourceSelectorQueryError, match="selector_value_not_observed"):
        await service.validate_selection(pending_payload=pending_payload, value="i-test123")
    await service.query(
        pending_payload=pending_payload,
        operation_key="ecs.instance.list",
        dynamic_parameters={"MaxResults": 20},
    )
    await service.validate_selection(pending_payload=pending_payload, value="i-test123")
    assert len(responses) == 1
    now[0] += 121
    with pytest.raises(ResourceSelectorQueryError, match="selector_resource_not_found"):
        await service.validate_selection(pending_payload=pending_payload, value="i-test123")


@pytest.mark.asyncio
async def test_opaque_page_tokens_and_compound_bucket_are_bound_to_the_pending_input() -> None:
    async def caller(_product, action, _region_id, params):
        if action == "ListBuckets":
            return {
                "ListAllMyBucketsResult": {
                    "NextMarker": "next-buckets",
                    "Buckets": {
                        "Bucket": [
                            {
                                "Name": "bucket-a",
                                "Region": "cn-hangzhou",
                                "Location": "oss-cn-hangzhou",
                            }
                        ]
                    },
                }
            }
        assert params["bucket"] == "bucket-a"
        assert "BucketName" not in params
        return {"ListBucketResult": {"Contents": [{"Key": "templates/app.yaml"}]}}

    service = ResourceSelectorQueryService(caller)
    first = pending("oss.bucket_object")
    await service.query(
        pending_payload=first,
        operation_key="oss.bucket.list",
        dynamic_parameters={},
    )
    await service.query(
        pending_payload=first,
        operation_key="oss.bucket.list",
        dynamic_parameters={"marker": "next-buckets"},
    )
    with pytest.raises(ResourceSelectorQueryError, match="selector_value_not_observed"):
        await service.validate_selection(pending_payload=first, value="bucket-a")
    await service.query(
        pending_payload=first,
        operation_key="oss.object.list",
        dynamic_parameters={"BucketName": "bucket-a", "max-keys": 50},
    )
    await service.validate_selection(
        pending_payload=first,
        value="oss://bucket-a/templates/app.yaml",
    )

    second = {**pending("oss.bucket_object"), "inputId": "resource-other"}
    with pytest.raises(ResourceSelectorQueryError, match="selector_pagination_token_invalid"):
        await service.query(
            pending_payload=second,
            operation_key="oss.bucket.list",
            dynamic_parameters={"marker": "next-buckets"},
        )
    with pytest.raises(ResourceSelectorQueryError, match="selector_parent_resource_not_observed"):
        await service.query(
            pending_payload=second,
            operation_key="oss.object.list",
            dynamic_parameters={"BucketName": "bucket-a", "max-keys": 50},
        )


def test_oss_v4_catalog_exposes_the_read_only_object_list_operation() -> None:
    operation = OssOperationCatalog.load().require("ListObjects")
    assert operation.sdk_method == "list_objects"
    assert operation.supported is True
    assert {field.openmeta_name for field in operation.field_mapping} >= {
        "bucket",
        "prefix",
        "delimiter",
        "marker",
        "max-keys",
    }


def test_acr_numeric_repo_id_is_normalized_to_the_scalar_answer_contract() -> None:
    assert _acr_value({"repoId": 123}, "repoId") == "123"
    assert _acr_value({"repoId": True}, "repoId") is None


def test_personal_acr_uses_the_reviewed_legacy_roa_paths() -> None:
    expected = {
        "acr.repo_attribute": ("GetRepoList", "/repos", {"Page": 1}),
        "acr.namespace": ("GetNamespaceList", "/namespace", {"Status": "NORMAL"}),
        "acr.repository_tag": (
            "GetRepoTags",
            "/repos/team%20one/demo%2Fimage/tags",
            {"Page": 1},
        ),
    }
    supplied = {
        "acr.repo_attribute": {"RegionId": "cn-hangzhou", "Page": 1},
        "acr.namespace": {"RegionId": "cn-hangzhou", "Status": "NORMAL"},
        "acr.repository_tag": {
            "RegionId": "cn-hangzhou",
            "RepoNamespace": "team one",
            "RepoName": "demo/image",
            "Page": 1,
        },
    }
    for selector_id, (action, pathname, wire_params) in expected.items():
        profile = get_profile(selector_id)
        assert profile is not None
        operation = profile.operations[0]
        assert (operation.product, operation.action, operation.api_version, operation.style, operation.method) == (
            "cr",
            action,
            "2016-06-07",
            "ROA",
            "GET",
        )
        assert _prepare_transport_request(operation, supplied[selector_id]) == (wire_params, pathname)


def test_oss_object_version_uses_bucket_host_and_source_prefix() -> None:
    profile = get_profile("oss.object_version")
    assert profile is not None and profile.source_parameter_key == "prefix"
    operation = profile.operations[0]
    assert _prepare_transport_request(
        operation,
        {
            "RegionId": "cn-hangzhou",
            "BucketName": "example-bucket",
            "prefix": "templates/app.yaml",
        },
    ) == ({"bucket": "example-bucket", "prefix": "templates/app.yaml"}, None)


def test_generic_oss_object_uses_the_public_object_listing_contract() -> None:
    profile = get_profile("oss.object")
    assert profile is not None
    operation = next(item for item in profile.operations if item.key == "oss.object.dataapi.oss.getbucket")
    assert (operation.product, operation.action, operation.api_version) == ("Oss", "ListObjects", "2019-05-17")
    assert _prepare_transport_request(
        operation,
        {"RegionId": "cn-hangzhou", "BucketName": "example-bucket", "prefix": "templates/"},
    ) == ({"bucket": "example-bucket", "prefix": "templates/"}, None)


def test_hologres_filters_use_the_reviewed_json_request_body() -> None:
    profile = get_profile("hologres.instance")
    assert profile is not None
    operation = profile.operations[0]
    assert operation.parameters_in_body is True
    assert _adapt_server_parameters(
        operation,
        {"RegionId": "cn-hangzhou", "resourceGroupId": "rg-test", "cmsInstanceType": "standard"},
    ) == {"resourceGroupId": "rg-test", "cmsInstanceType": "standard"}


@pytest.mark.parametrize(
    ("selector_id", "operation_key", "params", "expected"),
    [
        (
            "appflow.user_auth_config",
            "appflow.user_auth_config.dataapi.appflow.listuserauthconfigs",
            {"ConnectorId": "connector-test", "ConnectorVersion": 1, "MaxResults": 20},
            {"ConnectorId": "connector-test", "ConnectorVersion": "1", "MaxResults": "20"},
        ),
        (
            "oos.package",
            "oos.package.dataapi.ecs.describeinstances",
            {"RegionId": "cn-hangzhou", "InstanceIds": ["i-test"]},
            {"RegionId": "cn-hangzhou", "InstanceIds": '["i-test"]'},
        ),
        (
            "oos.application",
            "oos.application.dataapi.oos.listapplications",
            {"Tags": {"environment": "test"}},
            {"Tags": '{"environment":"test"}'},
        ),
    ],
)
def test_public_api_wire_type_adapters(selector_id, operation_key, params, expected) -> None:
    profile = get_profile(selector_id)
    assert profile is not None
    operation = next(item for item in profile.operations if item.key == operation_key)
    assert _adapt_server_parameters(operation, params) == expected


@pytest.mark.asyncio
async def test_oss_v4_shapes_are_projected_and_bucket_name_is_mapped_to_host_parameter() -> None:
    calls = []

    async def caller(product, action, region_id, params):
        calls.append((product, action, region_id, params))
        if action == "ListBuckets":
            return {
                "is_truncated": True,
                "next_marker": "next-buckets",
                "buckets": [
                    {
                        "name": "bucket-a",
                        "region": "cn-hangzhou",
                        "location": "oss-cn-hangzhou",
                        "storage_class": "Standard",
                    }
                ],
            }
        return {
            "name": "bucket-a",
            "prefix": "allowed/",
            "contents": [{"key": "allowed/app.yaml", "etag": "etag-a", "size": 12}],
            "common_prefixes": [{"prefix": "allowed/dir/"}],
        }

    service = ResourceSelectorQueryService(caller, observation_ttl_seconds=0)
    pending_payload = pending(
        "oss.bucket_object",
        {"RegionId": "cn-hangzhou", "ObjectType": "other", "Prefix": "allowed/", "Delimiter": "/"},
    )
    buckets = await service.query(
        pending_payload=pending_payload,
        operation_key="oss.bucket.list",
        dynamic_parameters={},
    )
    objects = await service.query(
        pending_payload=pending_payload,
        operation_key="oss.object.list",
        dynamic_parameters={"BucketName": "bucket-a", "prefix": "allowed/", "delimiter": "/", "max-keys": 50},
    )

    assert buckets == {
        "ListAllMyBucketsResult": {
            "IsTruncated": True,
            "NextMarker": "next-buckets",
            "Buckets": {
                "Bucket": [
                    {
                        "Name": "bucket-a",
                        "Region": "cn-hangzhou",
                        "Location": "oss-cn-hangzhou",
                        "StorageClass": "Standard",
                    }
                ]
            },
        }
    }
    assert objects == {
        "ListBucketResult": {
            "Name": "bucket-a",
            "Prefix": "allowed/",
            "Contents": [{"Key": "allowed/app.yaml", "ETag": "etag-a", "Size": 12}],
            "CommonPrefixes": [{"Prefix": "allowed/dir/"}],
        }
    }
    assert calls[1] == (
        "oss",
        "ListObjects",
        "cn-hangzhou",
        {"prefix": "allowed/", "delimiter": "/", "bucket": "bucket-a", "max-keys": 50},
    )
    await service.validate_selection(pending_payload=pending_payload, value="oss://bucket-a/allowed/app.yaml")
    assert calls[2][1:] == (
        "ListObjects",
        "cn-hangzhou",
        {"prefix": "allowed/", "delimiter": "/", "bucket": "bucket-a", "max-keys": 50},
    )


def test_generic_oss_object_and_version_project_live_sdk_shapes() -> None:
    owner = {"id": "owner-id", "display_name": "owner-name"}
    object_item = {
        "key": "templates/app.yaml",
        "object_type": "Normal",
        "size": 12,
        "etag": "etag-a",
        "last_modified": "2026-09-14T00:00:00Z",
        "storage_class": "Standard",
        "owner": owner,
    }
    assert project_ore_response(
        "oss.object.dataapi.oss.getbucket",
        {
            "name": "bucket-a",
            "is_truncated": False,
            "contents": [object_item],
        },
    ) == {
        "ListBucketResult": {
            "Name": "bucket-a",
            "IsTruncated": False,
            "Contents": [
                {
                    "Key": "templates/app.yaml",
                    "Type": "Normal",
                    "Size": 12,
                    "ETag": "etag-a",
                    "LastModified": "2026-09-14T00:00:00Z",
                    "StorageClass": "Standard",
                }
            ],
            "CommonPrefixes": [],
        }
    }
    assert project_ore_response(
        "oss.object_version.dataapi.oss.listobjectversions",
        {
            "name": "bucket-a",
            "prefix": "templates/app.yaml",
            "is_truncated": False,
            "version": [{**object_item, "version_id": "version-a"}],
        },
    ) == {
        "ListVersionsResult": {
            "Name": "bucket-a",
            "Prefix": "templates/app.yaml",
            "IsTruncated": "false",
            "Version": [
                {
                    "Key": "templates/app.yaml",
                    "Size": 12,
                    "ETag": "etag-a",
                    "LastModified": "2026-09-14T00:00:00Z",
                    "StorageClass": "Standard",
                    "VersionId": "version-a",
                    "Owner": {"ID": "owner-id", "DisplayName": "owner-name"},
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_oss_object_scope_and_object_type_are_enforced_on_the_server() -> None:
    calls = []

    async def caller(_product, action, _region_id, _params):
        calls.append(action)
        if action == "ListBuckets":
            return {"buckets": [{"name": "bucket-a", "region": "cn-hangzhou", "location": "oss-cn-hangzhou"}]}
        return {
            "contents": [{"key": "allowed/file.yaml"}],
            "common_prefixes": [{"prefix": "allowed/dir/"}],
        }

    service = ResourceSelectorQueryService(caller)
    pending_payload = pending(
        "oss.bucket_object",
        {"RegionId": "cn-hangzhou", "ObjectType": "other", "Prefix": "allowed/", "Delimiter": "/"},
    )
    await service.query(
        pending_payload=pending_payload,
        operation_key="oss.bucket.list",
        dynamic_parameters={},
    )
    for dynamic_parameters in (
        {"BucketName": "bucket-a", "prefix": "outside/"},
        {"BucketName": "bucket-a", "delimiter": ""},
    ):
        with pytest.raises(ResourceSelectorQueryError, match="selector_query_parameters_invalid"):
            await service.query(
                pending_payload=pending_payload,
                operation_key="oss.object.list",
                dynamic_parameters=dynamic_parameters,
            )
    assert calls == ["ListBuckets"]

    await service.query(
        pending_payload=pending_payload,
        operation_key="oss.object.list",
        dynamic_parameters={"BucketName": "bucket-a", "prefix": "allowed/", "delimiter": "/"},
    )
    with pytest.raises(ResourceSelectorQueryError, match="selector_value_not_observed"):
        await service.validate_selection(pending_payload=pending_payload, value="oss://bucket-a/allowed/dir/")

    directory_payload = {
        **pending(
            "oss.bucket_object",
            {"RegionId": "cn-hangzhou", "ObjectType": "dir", "Prefix": "allowed/", "Delimiter": "/"},
        ),
        "inputId": "resource-directory",
    }
    await service.query(
        pending_payload=directory_payload,
        operation_key="oss.bucket.list",
        dynamic_parameters={},
    )
    await service.query(
        pending_payload=directory_payload,
        operation_key="oss.object.list",
        dynamic_parameters={"BucketName": "bucket-a", "prefix": "allowed/", "delimiter": "/"},
    )
    with pytest.raises(ResourceSelectorQueryError, match="selector_value_not_observed"):
        await service.validate_selection(pending_payload=directory_payload, value="oss://bucket-a/allowed/file.yaml")


@pytest.mark.asyncio
async def test_vswitch_confirmation_rejects_a_zone_without_the_requested_instance_type() -> None:
    calls = []

    async def caller(_product, action, _region_id, _params):
        calls.append(action)
        if action == "DescribeVSwitches":
            return {
                "VSwitches": {
                    "VSwitch": [{"VSwitchId": "vsw-test123", "ZoneId": "cn-hangzhou-b", "Status": "Available"}]
                }
            }
        return {
            "AvailableZones": {
                "AvailableZone": [
                    {
                        "ZoneId": "cn-hangzhou-a",
                        "AvailableResources": {
                            "AvailableResource": [
                                {
                                    "Type": "InstanceType",
                                    "SupportedResources": {"SupportedResource": [{"Value": "ecs.g7.large"}]},
                                }
                            ]
                        },
                    }
                ]
            }
        }

    service = ResourceSelectorQueryService(caller)
    pending_payload = pending(
        "vpc.vswitch",
        {"RegionId": "cn-hangzhou", "InstanceType": "ecs.g7.large"},
    )
    await service.query(
        pending_payload=pending_payload,
        operation_key="vpc.vswitch.list",
        dynamic_parameters={"PageNumber": 1, "PageSize": 20},
    )
    with pytest.raises(ResourceSelectorQueryError, match="selector_value_not_allowed"):
        await service.validate_selection(pending_payload=pending_payload, value="vsw-test123")
    assert calls == ["DescribeVSwitches", "DescribeVSwitches", "DescribeAvailableResource"]
