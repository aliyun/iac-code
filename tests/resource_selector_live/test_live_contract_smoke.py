from __future__ import annotations

import json
import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from iac_code.resource_selector.profiles import SERVER_AUXILIARY_OPERATIONS, QueryOperation, iter_profiles
from iac_code.resource_selector.query import (
    ResourceSelectorQueryError,
    ResourceSelectorQueryService,
    call_aliyun_api,
    project_ore_response,
)
from iac_code.services.configuration_readiness import configuration_readiness

pytestmark = pytest.mark.resource_selector_live


def _enabled() -> bool:
    return os.environ.get("IAC_CODE_RESOURCE_SELECTOR_LIVE_SMOKE", "").lower() in {"1", "true", "yes", "on"}


def _representatives() -> list[tuple[str, QueryOperation]]:
    by_key: dict[str, tuple[str, QueryOperation]] = {}
    for profile in iter_profiles(include_disabled=False):
        for operation in profile.operations:
            by_key.setdefault(operation.key, (profile.selector_id, operation))
    for operation in SERVER_AUXILIARY_OPERATIONS:
        by_key.setdefault(operation.key, ("ecs.instance", operation))
    return list(by_key.values())


_REPRESENTATIVES = _representatives()
_OPERATIONS_BY_KEY = {operation.key: operation for _, operation in _REPRESENTATIVES}
_ENVIRONMENT_UNAVAILABLE: dict[str, tuple[tuple[str, ...], str]] = {
    "rds.instance.list": (
        ("error code Forbidden", "User not authorized to operate on the specified resource"),
        "configured credentials cannot call RDS DescribeDBInstances",
    ),
    "oss.bucket.list": (
        ("error code AccessDenied", "forbidden to oss:ListBuckets"),
        "configured credentials cannot call OSS ListBuckets",
    ),
    "acr.repo_attribute.dataapi.acr.listrepo": (
        ("error code USER_NOT_REGISTERED", "user is not registered"),
        "configured account has not registered Container Registry Personal Edition",
    ),
    "acr.namespace.dataapi.acr.listnamespace": (
        ("error code USER_NOT_REGISTERED", "user is not registered"),
        "configured account has not registered Container Registry Personal Edition",
    ),
    "fc.service.dataapi.fc_api.listservices": (
        ("error code AccessDenied", "missing parameter SecurityToken"),
        "Function Compute rejects the configured OAuth-derived STS credential for ListServices",
    ),
    "fc.function.dataapi.fc_api.listfunctions": (
        ("error code AccessDenied", "missing parameter SecurityToken"),
        "Function Compute rejects the configured OAuth-derived STS credential for ListFunctions",
    ),
    "fc3.function.dataapi.fc3_api.listfunctions": (
        ("error code AccessDenied", "missing parameter SecurityToken"),
        "Function Compute 3.0 rejects the configured OAuth-derived STS credential for ListFunctions",
    ),
    "domain.domain.dataapi.cdn.checkcdndomainicp": (
        ("domain provided does not belong to you",),
        "reviewed live context does not contain a domain owned by the configured account",
    ),
    "oos.git_organization.dataapi.oos.listgitorganizations": (
        ("error code GitOauthNotAuthorized", "oauth not authorized"),
        "configured account has not authorized the selected Git platform in OOS",
    ),
    "oos.git_repository.dataapi.oos.listgitrepositories": (
        ("error code GitOauthNotAuthorized", "oauth not authorized"),
        "configured account has not authorized the selected Git platform in OOS",
    ),
}


def _contexts() -> dict[str, Any]:
    path = os.environ.get("IAC_CODE_RESOURCE_SELECTOR_LIVE_CONTEXT")
    if not path:
        pytest.fail("IAC_CODE_RESOURCE_SELECTOR_LIVE_CONTEXT must point to the reviewed live parameter file")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("operations"), dict):
        pytest.fail("live parameter file must contain an operations object")
    return value["operations"]


def _environment_unavailable_reason(operation_key: str, message: str) -> str | None:
    expected = _ENVIRONMENT_UNAVAILABLE.get(operation_key)
    if expected is None:
        return None
    markers, reason = expected
    folded = message.casefold()
    return reason if all(marker.casefold() in folded for marker in markers) else None


def _is_missing_resource(message: str) -> bool:
    """Keep resource absence distinct from an invalid API contract."""

    folded = message.casefold()
    contract_errors = (
        "invalidaction",
        "unsupportedoperation",
        "operationnotfound",
        "apinotfound",
        "invalidversion",
        "versionnotfound",
        "no valid alibaba cloud api version",
    )
    if any(marker in folded for marker in contract_errors):
        return False
    return any(
        marker.casefold() in folded
        for marker in (
            ".NotFound",
            "NotFound.",
            "ResourceNotFound",
            "EntityNotExist",
            "cannot be found",
            "not exist",
        )
    )


def _context_call(
    contexts: Mapping[str, Any],
    operation_key: str,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[QueryOperation, str | None, dict[str, Any]]:
    context = contexts.get(operation_key)
    operation = _OPERATIONS_BY_KEY.get(operation_key)
    if operation is None or not isinstance(context, Mapping):
        pytest.fail("missing live discovery context for {}".format(operation_key))
    region_id = context.get("regionId")
    params = context.get("params")
    if region_id is not None and not isinstance(region_id, str):
        pytest.fail("invalid live discovery region for {}".format(operation_key))
    if not isinstance(params, Mapping):
        pytest.fail("invalid live discovery parameters for {}".format(operation_key))
    merged = deepcopy(dict(params))
    merged.update(overrides or {})
    return operation, region_id, merged


async def _call_context(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    operation_key: str,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    operation, region_id, params = _context_call(contexts, operation_key, overrides=overrides)
    return await service._call_operation(operation, region_id=region_id, params=params)


def _first_mapping(value: object) -> Mapping[str, Any] | None:
    if not isinstance(value, list):
        return None
    return next((item for item in value if isinstance(item, Mapping)), None)


def _replace_resource_manager_parent(value: Any, root_folder_id: str) -> Any:
    if isinstance(value, list):
        return [_replace_resource_manager_parent(item, root_folder_id) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _replace_resource_manager_parent(item, root_folder_id) for key, item in value.items()}
    if "ParentFolderId" in result:
        result["ParentFolderId"] = root_folder_id
    if "customRequestKey" in result:
        result["customRequestKey"] = root_folder_id
    return result


async def _resolve_resource_manager_parameters(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    operation: QueryOperation,
    params: dict[str, Any],
) -> dict[str, Any]:
    if operation.action not in {"ListAccountsForParent", "ListFoldersForParent"}:
        return params
    response = await _call_context(
        service,
        contexts,
        "resource_manager.account.dataapi.resourcemanager.getresourcedirectory",
    )
    directory = response.get("ResourceDirectory")
    root_folder_id = directory.get("RootFolderId") if isinstance(directory, Mapping) else None
    if not isinstance(root_folder_id, str) or not root_folder_id:
        pytest.skip("configured account has no enabled resource directory root folder")
    return _replace_resource_manager_parent(params, root_folder_id)


async def _first_cr_instance(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
) -> Mapping[str, Any]:
    response = await _call_context(service, contexts, "cr.instance.dataapi.cr.listinstance")
    instance = _first_mapping(response.get("Instances"))
    if instance is None or not isinstance(instance.get("InstanceId"), str):
        pytest.skip("configured account has no Container Registry Enterprise Edition instance")
    return instance


async def _first_cr_namespace(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    instance_id: str,
) -> Mapping[str, Any]:
    response = await _call_context(
        service,
        contexts,
        "cr.namespace.dataapi.cr.listnamespace",
        overrides={"InstanceId": instance_id},
    )
    namespace = _first_mapping(response.get("Namespaces"))
    if namespace is None or not isinstance(namespace.get("NamespaceName"), str):
        pytest.skip("configured Container Registry Enterprise Edition instance has no namespace")
    return namespace


async def _first_cr_repository(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    instance_id: str,
    namespace_name: str,
) -> Mapping[str, Any]:
    response = await _call_context(
        service,
        contexts,
        "cr.repository.dataapi.cr.listrepository",
        overrides={"InstanceId": instance_id, "RepoNamespaceName": namespace_name},
    )
    repository = _first_mapping(response.get("Repositories"))
    if (
        repository is None
        or not isinstance(repository.get("RepoId"), str)
        or not isinstance(repository.get("RepoName"), str)
    ):
        pytest.skip("configured Container Registry Enterprise Edition namespace has no repository")
    return repository


async def _resolve_cr_parameters(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    operation: QueryOperation,
    params: dict[str, Any],
) -> dict[str, Any]:
    if operation.product.casefold() != "cr" or operation.api_version != "2018-12-01":
        return params
    if operation.action == "ListInstance":
        return params
    instance = await _first_cr_instance(service, contexts)
    instance_id = instance["InstanceId"]
    params["InstanceId"] = instance_id
    if operation.action in {"ListNamespace", "GetInstance"}:
        return params
    namespace = await _first_cr_namespace(service, contexts, instance_id)
    namespace_name = namespace["NamespaceName"]
    if operation.action == "ListRepository":
        params["RepoNamespaceName"] = namespace_name
        params.pop("RepoName", None)
        return params
    repository = await _first_cr_repository(service, contexts, instance_id, namespace_name)
    params.update(
        {
            "RepoName": repository["RepoName"],
            "RepoNamespaceName": namespace_name,
        }
    )
    if operation.action == "ListRepoTag":
        params["RepoId"] = repository["RepoId"]
        params.pop("RepoName", None)
        params.pop("RepoNamespaceName", None)
    return params


async def _resolve_oss_parameters(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    operation: QueryOperation,
    region_id: str | None,
    params: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    if operation.key not in {
        "oss.object.list",
        "oss.object.dataapi.oss.getbucket",
        "oss.object_version.dataapi.oss.listobjectversions",
    }:
        return region_id, params

    bucket_operation, bucket_region_id, bucket_params = _context_call(contexts, "oss.bucket.list")
    response = await service._call_operation(
        bucket_operation,
        region_id=bucket_region_id,
        params=bucket_params,
    )
    projected = project_ore_response(bucket_operation.key, response)
    root = projected.get("ListAllMyBucketsResult")
    container = root.get("Buckets") if isinstance(root, Mapping) else None
    buckets = container.get("Bucket") if isinstance(container, Mapping) else None
    candidates = (
        [item for item in buckets if isinstance(item, Mapping)]
        if isinstance(buckets, list)
        else [buckets]
        if isinstance(buckets, Mapping)
        else []
    )
    bucket = next((item for item in candidates if item.get("Region") == region_id), None)
    bucket = bucket or (candidates[0] if candidates else None)
    bucket_name = bucket.get("Name") if isinstance(bucket, Mapping) else None
    if not isinstance(bucket_name, str) or not bucket_name:
        pytest.skip("configured account has no OSS bucket")

    selected_region = bucket.get("Region")
    if not isinstance(selected_region, str) or not selected_region:
        location = bucket.get("Location")
        selected_region = location.removeprefix("oss-") if isinstance(location, str) else region_id
    if operation.key == "oss.object.list":
        params.pop("BucketName", None)
        params["bucket"] = bucket_name
    else:
        params.pop("bucket", None)
        params["BucketName"] = bucket_name
    return selected_region, params


def _projected_field_values(value: object, field: str) -> list[str]:
    normalized_field = field.replace("_", "").replace("-", "").casefold()
    values: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if (
                    isinstance(key, str)
                    and key.replace("_", "").replace("-", "").casefold() == normalized_field
                    and isinstance(child, (str, int))
                    and not isinstance(child, bool)
                ):
                    values.append(str(child))
                visit(child)
        elif isinstance(item, list):
            for child in item[:200]:
                visit(child)

    visit(value)
    return values


async def _discover_context_value(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    operation_key: str,
    field: str,
) -> str:
    operation = _OPERATIONS_BY_KEY[operation_key]
    response = await _call_context(service, contexts, operation_key)
    projected = project_ore_response(operation.response_projector, response)
    values = _projected_field_values(projected, field)
    if not values:
        pytest.skip("configured account has no {} parent/source resource".format(field))
    return values[0]


_LIVE_PARENT_BINDINGS: dict[str, tuple[str, str, str]] = {
    "redis.connection.list": ("redis.instance.list", "InstanceId", "InstanceId"),
    "vpc.vswitch.dataapi.serverless.describenamespaceresources": (
        "sae.namespace.dataapi.serverless.listnamespacesv2",
        "NamespaceId",
        "NamespaceId",
    ),
    "ecs.vswitch.dataapi.serverless.describenamespaceresources": (
        "sae.namespace.dataapi.serverless.listnamespacesv2",
        "NamespaceId",
        "NamespaceId",
    ),
    "cs.node_pool.dataapi.cs.describeclusternodepools": (
        "cs.cluster.dataapi.cs.describeclustersv1",
        "cluster_id",
        "ClusterId",
    ),
    "cloudsso.user.dataapi.cloudsso.listusers": (
        "cloudsso.directory.dataapi.cloudsso.listdirectories",
        "DirectoryId",
        "DirectoryId",
    ),
    "cloudsso.group.dataapi.cloudsso.listgroups": (
        "cloudsso.directory.dataapi.cloudsso.listdirectories",
        "DirectoryId",
        "DirectoryId",
    ),
    "cloudsso.access_configuration.dataapi.cloudsso.listaccessconfigurations": (
        "cloudsso.directory.dataapi.cloudsso.listdirectories",
        "DirectoryId",
        "DirectoryId",
    ),
    "flow.connection.dataapi.devops2020.listserviceconnections": (
        "flow.organization.dataapi.devops2020.listjoinedorganizations",
        "OrganizationId",
        "organizationId",
    ),
    "ecs.launch_template_version.dataapi.ecs.describelaunchtemplateversions": (
        "ecs.launch_template.dataapi.ecs.describelaunchtemplates",
        "LaunchTemplateId",
        "LaunchTemplateId",
    ),
    "nas.mount_target.dataapi.nas.describemounttargets": (
        "nas.file_system.dataapi.nas.describefilesystems",
        "FileSystemId",
        "FileSystemId",
    ),
    "computenest.artifact_version.dataapi.computenest0521.listartifactversions": (
        "computenest.artifact.dataapi.computenest0521.listartifacts",
        "ArtifactId",
        "ArtifactId",
    ),
    "oos.template_version.dataapi.oos.listtemplateversions": (
        "oos.template.dataapi.oos.listtemplates",
        "TemplateName",
        "TemplateName",
    ),
    "oos.package_version.dataapi.oos.listtemplateversions": (
        "oos.package.dataapi.oos.listtemplates",
        "TemplateName",
        "TemplateName",
    ),
    "domain.domain.dataapi.cdn.checkcdndomainicp": (
        "domain.domain.dataapi.domain.querydomainlist",
        "DomainName",
        "DomainName",
    ),
}


async def _resolve_discoverable_parent_parameters(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    operation: QueryOperation,
    params: dict[str, Any],
) -> dict[str, Any]:
    binding = _LIVE_PARENT_BINDINGS.get(operation.key)
    if binding is None:
        return params
    parent_operation_key, response_field, parameter = binding
    params[parameter] = await _discover_context_value(
        service,
        contexts,
        parent_operation_key,
        response_field,
    )
    return params


async def _resolve_kms_parameters(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    operation: QueryOperation,
    params: dict[str, Any],
) -> dict[str, Any]:
    if operation.key not in {
        "kms.key.dataapi.kms.describekey",
        "kms.key.multiapi.kms.multiapi",
    }:
        return params
    key_id = await _discover_context_value(
        service,
        contexts,
        "kms.key.dataapi.kms.listkeys",
        "KeyId",
    )
    if operation.request_kind != "multiApi":
        params["KeyId"] = key_id
        return params
    requests = params.get("requests")
    if not isinstance(requests, list) or not requests:
        pytest.fail("missing live KMS alias request batch")
    for request in requests:
        request_params = request.get("parameters") if isinstance(request, Mapping) else None
        if isinstance(request_params, dict):
            request_params["KeyId"] = key_id
    return params


async def _resolve_other_live_parameters(
    service: ResourceSelectorQueryService,
    contexts: Mapping[str, Any],
    operation: QueryOperation,
    params: dict[str, Any],
) -> dict[str, Any]:
    params = await _resolve_discoverable_parent_parameters(service, contexts, operation, params)
    params = await _resolve_kms_parameters(service, contexts, operation, params)
    if operation.key.startswith("ram.service_role."):
        params["Service"] = "ecs.aliyuncs.com"
    if operation.key == "appflow.user_auth_config.dataapi.appflow.listuserauthconfigs":
        params.update(
            {
                "ConnectorId": "connector-88d2c03da8c9410e8a91",
                "ConnectorVersion": 6,
                "AuthType": "QQBotAccessToken",
            }
        )
    if operation.key in {
        "service_catalog.portfolio.dataapi.servicecatalog.listlaunchoptions",
        "service_catalog.product_version.dataapi.servicecatalog.listproductversions",
    }:
        products = await call_aliyun_api(
            "servicecatalog",
            "ListProductsAsEndUser",
            "cn-hangzhou",
            {"PageNumber": 1, "PageSize": 100},
            api_version="2021-09-01",
        )
        product_ids = _projected_field_values(products, "ProductId")
        if not product_ids:
            pytest.skip("configured account has no Service Catalog product available to the end user")
        params["ProductId"] = product_ids[0]
    if operation.key == "ecs.instance.cloud_assistant_status":
        instance_ids = await _discover_context_value(
            service,
            contexts,
            "ecs.instance.list",
            "InstanceId",
        )
        params["InstanceId"] = [instance_ids]
    return params


@pytest.mark.skipif(not _enabled(), reason="explicit live selector smoke is disabled")
@pytest.mark.parametrize(
    ("selector_id", "operation"),
    _REPRESENTATIVES,
    ids=lambda value: value.key if isinstance(value, QueryOperation) else value,
)
@pytest.mark.asyncio
async def test_every_unique_read_only_api_contract_against_the_configured_account(
    selector_id, operation, monkeypatch
) -> None:
    live_config_dir = os.environ.get("IAC_CODE_RESOURCE_SELECTOR_LIVE_CONFIG_DIR")
    if live_config_dir:
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", live_config_dir)
    readiness = configuration_readiness(model="")
    assert readiness["cloud"]["ready"], "configured Alibaba Cloud credentials are required"
    contexts = _contexts()
    context = contexts.get(operation.key)
    assert isinstance(context, dict), "missing reviewed live parameters for {}".format(operation.key)
    region_id = context.get("regionId")
    params = context.get("params")
    assert region_id is None or isinstance(region_id, str)
    assert isinstance(params, dict)

    service = ResourceSelectorQueryService(call_aliyun_api)
    params = await _resolve_resource_manager_parameters(service, contexts, operation, deepcopy(params))
    params = await _resolve_cr_parameters(service, contexts, operation, params)
    region_id, params = await _resolve_oss_parameters(service, contexts, operation, region_id, params)
    params = await _resolve_other_live_parameters(service, contexts, operation, params)
    try:
        response = await service._call_operation(operation, region_id=region_id, params=params)
    except ResourceSelectorQueryError as exc:
        message = str(exc)
        unavailable_reason = _environment_unavailable_reason(operation.key, message)
        if unavailable_reason:
            pytest.skip(unavailable_reason)
        if _is_missing_resource(message):
            pytest.skip("configured account has no real parent/source resource for this detail or child contract")
        raise
    assert isinstance(response, dict)
    projected = (
        project_ore_response(operation.response_projector, response)
        if operation.response_projector is not None
        else response
    )
    assert isinstance(projected, dict), "{} ({}) returned an invalid envelope".format(operation.key, selector_id)


def test_live_smoke_catalog_covers_every_unique_server_operation() -> None:
    operation_keys = {
        operation.key for profile in iter_profiles(include_disabled=False) for operation in profile.operations
    } | {operation.key for operation in SERVER_AUXILIARY_OPERATIONS}
    assert len(_REPRESENTATIVES) == len(operation_keys)


def test_environment_unavailable_classification_is_exact_and_does_not_hide_contract_errors() -> None:
    assert (
        _environment_unavailable_reason(
            "oss.bucket.list",
            "error code AccessDenied: You are forbidden to oss:ListBuckets",
        )
        == "configured credentials cannot call OSS ListBuckets"
    )
    assert _environment_unavailable_reason("oss.bucket.list", "selector_query_response_invalid") is None
    assert (
        _environment_unavailable_reason(
            "dashvector.cluster.dataapi.centaur_console.listclusters",
            "No valid Alibaba Cloud API version is available for centaur-console",
        )
        is None
    )
    assert _is_missing_resource("error code InvalidInstanceId.NotFound: instance does not exist")
    assert not _is_missing_resource("error code InvalidAction.NotFound: API operation is unavailable")
