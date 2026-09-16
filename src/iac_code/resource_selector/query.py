"""Controlled Web/Desktop query bridge for ORE selector operations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import Any, cast
from urllib.parse import quote

from iac_code.tools.base import ToolContext

from .profiles import SERVER_AUXILIARY_OPERATIONS, QueryOperation, SelectorProfile, get_profile
from .validation import validate_answer_value

ApiCaller = Callable[[str, str, str | None, dict[str, Any]], Awaitable[dict[str, Any]]]
Clock = Callable[[], float]

RECENT_OBSERVATION_TTL_SECONDS = 120.0
MAX_MULTI_API_REQUESTS = 100
MAX_MULTI_API_CONCURRENCY = 8


class ResourceSelectorQueryError(ValueError):
    pass


class ResourceSelectorQueryService:
    def __init__(
        self,
        caller: ApiCaller | None = None,
        *,
        observation_ttl_seconds: float = RECENT_OBSERVATION_TTL_SECONDS,
        clock: Clock = monotonic,
    ) -> None:
        self._caller = caller or call_aliyun_api
        self._observation_ttl_seconds = max(0.0, observation_ttl_seconds)
        self._clock = clock
        self._observed: dict[tuple[str, str], dict[str, tuple[str, dict[str, Any], float]]] = {}
        self._selection_queries: set[tuple[str, str]] = set()
        self._latest_selection_query_empty: dict[tuple[str, str], bool] = {}
        self._observed_parents: dict[tuple[str, str], set[str]] = {}
        self._page_tokens: dict[tuple[str, str, str], set[str]] = {}

    async def query(
        self,
        *,
        pending_payload: Mapping[str, Any],
        operation_key: str,
        dynamic_parameters: object,
    ) -> dict[str, Any]:
        selector = pending_payload.get("selector")
        if not isinstance(selector, Mapping):
            raise ResourceSelectorQueryError("selector_profile_mismatch")
        selector = cast(Mapping[str, Any], selector)
        selector_id = selector.get("id")
        profile = get_profile(selector_id) if isinstance(selector_id, str) else None
        if profile is None or not profile.enabled:
            raise ResourceSelectorQueryError("selector_profile_mismatch")
        operation = next((item for item in profile.operations if item.key == operation_key), None)
        if operation is None:
            raise ResourceSelectorQueryError("selector_operation_not_allowed")
        if not isinstance(dynamic_parameters, Mapping):
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")
        dynamic_parameters = cast(Mapping[str, Any], dynamic_parameters)
        unknown = set(dynamic_parameters) - set(operation.accepted_parameters)
        if unknown:
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")

        metadata = selector.get("associationPropertyMetadata")
        if not isinstance(metadata, Mapping):
            raise ResourceSelectorQueryError("selector_profile_mismatch")
        metadata = cast(Mapping[str, Any], metadata)
        _validate_source_parameter(profile, operation, selector.get("source"), dynamic_parameters)
        _validate_dynamic_fixed_constraints(operation, dynamic_parameters)
        _validate_dynamic_metadata_constraints(profile, operation, dynamic_parameters, metadata)
        input_id = pending_payload.get("inputId")
        if not isinstance(input_id, str):
            raise ResourceSelectorQueryError("selector_profile_mismatch")
        observed_key = (input_id, profile.selector_id)
        if operation.key == "oss.object.list":
            bucket_name = dynamic_parameters.get("BucketName")
            if not isinstance(bucket_name, str) or bucket_name not in self._observed_parents.get(observed_key, set()):
                raise ResourceSelectorQueryError("selector_parent_resource_not_observed")
        if operation.key == "cr.repository_tag.dataapi.cr.listrepotag":
            repo_id = dynamic_parameters.get("RepoId")
            if not isinstance(repo_id, str) or repo_id not in self._observed_parents.get(observed_key, set()):
                raise ResourceSelectorQueryError("selector_parent_resource_not_observed")
        if operation.key == "ecs.vswitch.dataapi.vpc.describevswitches":
            requested_vpc_id = dynamic_parameters.get("VpcId")
            trusted_vpc_ids = set(self._observed_parents.get(observed_key, set()))
            trusted_vpc_id = metadata.get("VpcId", metadata.get("VPCId"))
            if isinstance(trusted_vpc_id, str) and trusted_vpc_id:
                trusted_vpc_ids.add(trusted_vpc_id)
            if requested_vpc_id is not None and (
                not isinstance(requested_vpc_id, str) or requested_vpc_id not in trusted_vpc_ids
            ):
                raise ResourceSelectorQueryError("selector_parent_resource_not_observed")
        pagination_key = (input_id, profile.selector_id, operation.key)
        _validate_page_token(operation.key, dynamic_parameters, self._page_tokens.get(pagination_key, set()))

        params = _fixed_query_parameters(profile, operation, metadata, selector.get("source"))
        if profile.selector_id == "vpc.vswitch" and operation.key == "vpc.vswitch.list":
            resolved_vpc_id = await self._resolve_vswitch_vpc_id(profile, metadata)
            if resolved_vpc_id:
                params["VpcId"] = resolved_vpc_id
        params.update(_normalize_dynamic_parameters(operation, dynamic_parameters))
        region_id = _operation_region_id(metadata, params, operation=operation)
        raw = await self._call_operation(operation, region_id=region_id, params=params)
        if operation.key == "ecs.instance.list":
            raw = await self._apply_ecs_constraints(raw, metadata=metadata, region_id=region_id)
        projected = project_ore_response(operation.response_projector, raw)
        self._page_tokens.setdefault(pagination_key, set()).update(_next_page_tokens(operation.key, projected))
        observed = self._observed.setdefault(observed_key, {})
        recipe = (operation.key, dict(dynamic_parameters), self._clock())
        selection_values = _selection_values(profile, operation.key, projected, dynamic_parameters, metadata)
        if _operation_can_return_selection_values(profile, operation.key):
            self._selection_queries.add(observed_key)
            self._latest_selection_query_empty[observed_key] = not selection_values
        for value in selection_values:
            observed[value] = recipe
        if profile.selector_id == "oss.bucket_object" and operation.key == "oss.bucket.list":
            self._observed_parents.setdefault(observed_key, set()).update(_oss_bucket_values(projected, metadata))
        if operation.key == "cr.repository_tag.dataapi.cr.getrepository":
            repo_id = projected.get("RepoId")
            if isinstance(repo_id, str) and repo_id:
                self._observed_parents.setdefault(observed_key, set()).add(repo_id)
        if operation.key == "ecs.vswitch.dataapi.serverless.describenamespaceresources":
            data = projected.get("Data")
            vpc_id = data.get("VpcId") if isinstance(data, Mapping) else None
            if isinstance(vpc_id, str) and vpc_id:
                self._observed_parents.setdefault(observed_key, set()).add(vpc_id)
        return projected

    async def _resolve_vswitch_vpc_id(
        self,
        profile: SelectorProfile,
        metadata: Mapping[str, Any],
    ) -> str | None:
        namespace_id = metadata.get("SAENamespaceId")
        if isinstance(namespace_id, str) and namespace_id:
            operation = next(
                item
                for item in profile.operations
                if item.key == "vpc.vswitch.dataapi.serverless.describenamespaceresources"
            )
            params = _fixed_query_parameters(profile, operation, metadata, None)
            raw = await self._call_operation(
                operation,
                region_id=_operation_region_id(metadata, params, operation=operation),
                params=params,
            )
            data = raw.get("Data")
            vpc_id = data.get("VpcId") if isinstance(data, Mapping) else None
            if isinstance(vpc_id, str) and vpc_id:
                return vpc_id
            raise ResourceSelectorQueryError("selector_parent_resource_not_found")

        direct_vpc_id = metadata.get("VpcId", metadata.get("VPCId"))
        if isinstance(direct_vpc_id, str) and direct_vpc_id:
            return direct_vpc_id

        security_group_id = metadata.get("SecurityGroupId")
        if isinstance(security_group_id, str) and security_group_id:
            operation = next(
                item for item in profile.operations if item.key == "vpc.vswitch.dataapi.ecs.describesecuritygroups"
            )
            params = _fixed_query_parameters(profile, operation, metadata, None)
            raw = await self._call_operation(
                operation,
                region_id=_operation_region_id(metadata, params, operation=operation),
                params=params,
            )
            container = raw.get("SecurityGroups")
            groups = _as_item_list(container.get("SecurityGroup") if isinstance(container, Mapping) else None)
            vpc_id = groups[0].get("VpcId") if groups else None
            if isinstance(vpc_id, str) and vpc_id:
                return vpc_id
            raise ResourceSelectorQueryError("selector_parent_resource_not_found")
        return None

    async def _call_operation(
        self,
        operation: QueryOperation,
        *,
        region_id: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if operation.request_kind != "multiApi":
            return await self._invoke_caller(
                operation,
                region_id=region_id,
                params=_adapt_server_parameters(operation, params),
            )
        requests = params.get("requests")
        if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_MULTI_API_REQUESTS:
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")
        prepared: list[tuple[str, dict[str, Any]]] = []
        request_keys: set[str] = set()
        for index, request in enumerate(requests):
            if not isinstance(request, Mapping) or set(request) - {"parameters", "customRequestKey"}:
                raise ResourceSelectorQueryError("selector_query_parameters_invalid")
            request_map = cast(Mapping[str, Any], request)
            request_parameters = request_map.get("parameters")
            if not isinstance(request_parameters, Mapping):
                raise ResourceSelectorQueryError("selector_query_parameters_invalid")
            if set(request_parameters) - set(operation.batch_parameters):
                raise ResourceSelectorQueryError("selector_query_parameters_invalid")
            normalized = _normalize_parameter_mapping(request_parameters)
            for key, value in params.items():
                if key != "requests" and key in operation.batch_parameters:
                    normalized[key] = value
            custom_key = request_map.get("customRequestKey")
            key = custom_key if isinstance(custom_key, str) and custom_key else str(index)
            if len(key) > 256 or key in request_keys:
                raise ResourceSelectorQueryError("selector_query_parameters_invalid")
            request_keys.add(key)
            prepared.append((key, _adapt_server_parameters(operation, normalized)))

        semaphore = asyncio.Semaphore(MAX_MULTI_API_CONCURRENCY)

        async def invoke(key: str, parameters: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                response = await self._invoke_caller(operation, region_id=region_id, params=parameters)
            return key, _sanitize_response_value(response)

        responses = await asyncio.gather(*(invoke(key, parameters) for key, parameters in prepared))
        return dict(responses)

    async def _invoke_caller(
        self,
        operation: QueryOperation,
        *,
        region_id: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        # Preserve the small four-argument test seam while pinning the exact
        # OpenAPI version for the production caller.
        if self._caller is call_aliyun_api:
            wire_params, pathname = _prepare_transport_request(operation, params)
            return await call_aliyun_api(
                operation.product,
                operation.action,
                region_id,
                {} if operation.parameters_in_body else wire_params,
                api_version=operation.api_version,
                style=operation.style,
                method=operation.method,
                pathname=pathname,
                body=wire_params if operation.parameters_in_body else None,
            )
        return await self._caller(operation.product, operation.action, region_id, params)

    async def validate_selection(self, *, pending_payload: Mapping[str, Any], value: str) -> None:
        selector = pending_payload.get("selector")
        input_id = pending_payload.get("inputId")
        selector_id = selector.get("id") if isinstance(selector, Mapping) else None
        if not isinstance(input_id, str) or not isinstance(selector_id, str):
            raise ResourceSelectorQueryError("selector_profile_mismatch")
        recipe = self._observed.get((input_id, selector_id), {}).get(value)
        if recipe is None:
            raise ResourceSelectorQueryError("selector_value_not_observed")
        operation_key, dynamic_parameters, observed_at = recipe
        profile = get_profile(selector_id)
        metadata = selector.get("associationPropertyMetadata") if isinstance(selector, Mapping) else None
        if profile is None or not isinstance(metadata, Mapping):
            raise ResourceSelectorQueryError("selector_profile_mismatch")
        requires_live_constraint_check = (
            profile.selector_id == "vpc.vswitch" and bool(metadata.get("InstanceType")) and not metadata.get("ZoneId")
        )
        if (
            not requires_live_constraint_check
            and self._observation_ttl_seconds > 0
            and self._clock() - observed_at <= self._observation_ttl_seconds
        ):
            return
        response = await self.query(
            pending_payload=pending_payload,
            operation_key=operation_key,
            dynamic_parameters=dynamic_parameters,
        )
        fresh_values = _selection_values(profile, operation_key, response, dynamic_parameters, metadata)
        if value not in fresh_values:
            raise ResourceSelectorQueryError("selector_resource_not_found")
        if profile.selector_id == "vpc.vswitch":
            await self._validate_vswitch_instance_type(
                pending_payload=pending_payload,
                response=response,
                value=value,
                metadata=metadata,
            )

    def discard(self, *, pending_payload: Mapping[str, Any]) -> None:
        selector = pending_payload.get("selector")
        input_id = pending_payload.get("inputId")
        selector_id = selector.get("id") if isinstance(selector, Mapping) else None
        if isinstance(input_id, str) and isinstance(selector_id, str):
            self._observed.pop((input_id, selector_id), None)
            self._selection_queries.discard((input_id, selector_id))
            self._latest_selection_query_empty.pop((input_id, selector_id), None)
            self._observed_parents.pop((input_id, selector_id), None)
            for key in [key for key in self._page_tokens if key[:2] == (input_id, selector_id)]:
                self._page_tokens.pop(key, None)

    def options_empty(self, *, pending_payload: Mapping[str, Any]) -> bool | None:
        selector = pending_payload.get("selector")
        input_id = pending_payload.get("inputId")
        selector_id = selector.get("id") if isinstance(selector, Mapping) else None
        if not isinstance(input_id, str) or not isinstance(selector_id, str):
            return None
        key = (input_id, selector_id)
        if key not in self._selection_queries:
            return None
        return self._latest_selection_query_empty.get(key)

    async def _validate_vswitch_instance_type(
        self,
        *,
        pending_payload: Mapping[str, Any],
        response: Mapping[str, Any],
        value: str,
        metadata: Mapping[str, Any],
    ) -> None:
        requested = metadata.get("InstanceType")
        if not requested or metadata.get("ZoneId"):
            return
        container = response.get("VSwitches")
        switches = _as_item_list(container.get("VSwitch") if isinstance(container, Mapping) else None)
        zone_id = next(
            (
                item.get("ZoneId")
                for item in switches
                if item.get("VSwitchId") == value and isinstance(item.get("ZoneId"), str)
            ),
            None,
        )
        if not isinstance(zone_id, str):
            raise ResourceSelectorQueryError("selector_resource_not_found")
        available = await self.query(
            pending_payload=pending_payload,
            operation_key="vpc.instance_type.available",
            dynamic_parameters={},
        )
        requested_types = requested if isinstance(requested, list) else [requested]
        if zone_id not in _supported_vswitch_zones(available, requested_types):
            raise ResourceSelectorQueryError("selector_value_not_allowed")

    async def _apply_ecs_constraints(
        self,
        response: dict[str, Any],
        *,
        metadata: Mapping[str, Any],
        region_id: object,
    ) -> dict[str, Any]:
        container = response.get("Instances")
        if not isinstance(container, Mapping):
            return response
        instances = _as_item_list(container.get("Instance"))
        for metadata_key, response_key in (("Platform", "Platform"), ("OSType", "OSType")):
            expected = metadata.get(metadata_key)
            if isinstance(expected, str) and expected:
                instances = [item for item in instances if item.get(response_key) == expected]
        if metadata.get("PublicIpRequired") is True:
            instances = [item for item in instances if _ecs_has_public_ip(item)]
        if metadata.get("OnlyCloudAssistantExecutable") is True and instances:
            instance_ids = [item.get("InstanceId") for item in instances if isinstance(item.get("InstanceId"), str)]
            operation = next(
                item for item in SERVER_AUXILIARY_OPERATIONS if item.key == "ecs.instance.cloud_assistant_status"
            )
            status_response = await self._invoke_caller(
                operation,
                region_id=region_id if isinstance(region_id, str) and region_id else None,
                params={"InstanceId": instance_ids},
            )
            status_container = status_response.get("InstanceCloudAssistantStatusSet")
            statuses = (
                _as_item_list(status_container.get("InstanceCloudAssistantStatus"))
                if isinstance(status_container, Mapping)
                else []
            )
            executable = {
                item.get("InstanceId")
                for item in statuses
                if _truthy_cloud_assistant_status(item.get("CloudAssistantStatus"))
            }
            instances = [item for item in instances if item.get("InstanceId") in executable]
        return {**response, "Instances": {**container, "Instance": instances}}


def _fixed_query_parameters(
    profile: SelectorProfile,
    operation: QueryOperation,
    metadata: Mapping[str, Any],
    source: object,
) -> dict[str, Any]:
    params: dict[str, Any] = dict(operation.fixed_parameters)
    for metadata_key, api_parameter in operation.metadata_parameters:
        value = metadata.get(metadata_key)
        if value is not None:
            params[api_parameter] = value
    if operation.key in {
        "vpc.vswitch.dataapi.serverless.describenamespaceresources",
        "ecs.vswitch.dataapi.serverless.describenamespaceresources",
    }:
        namespace_id = params.get("NamespaceId")
        region_id = metadata.get("RegionId")
        if isinstance(namespace_id, str) and namespace_id and isinstance(region_id, str) and region_id:
            if not namespace_id.startswith("{}:".format(region_id)):
                params["NamespaceId"] = "{}:{}".format(region_id, namespace_id.rsplit(":", 1)[-1])
    if operation.key == "oss.object.list":
        for metadata_key, api_parameter in (("Prefix", "prefix"), ("Delimiter", "delimiter")):
            value = metadata.get(metadata_key)
            if isinstance(value, str) and value:
                params[api_parameter] = value
    if profile.selection_kind == "cloud_resource_derived_value":
        if not isinstance(source, Mapping):
            raise ResourceSelectorQueryError("selector_source_mismatch")
        source = cast(Mapping[str, Any], source)
        if source.get("selector_id") != profile.source_selector_id:
            raise ResourceSelectorQueryError("selector_source_mismatch")
        source_value = source.get("value")
        if not isinstance(source_value, str):
            raise ResourceSelectorQueryError("selector_source_mismatch")
        source_parameter = profile.source_parameter_for(operation.key)
        if source_parameter:
            params[source_parameter] = _source_parameter_value(profile, source_value)
    return params


def _source_parameter_value(profile: SelectorProfile, value: str) -> str | list[str]:
    return [value] if profile.selector_id == "ess.eci_container" else value


def _validate_source_parameter(
    profile: SelectorProfile,
    operation: QueryOperation,
    source: object,
    dynamic_parameters: Mapping[str, Any],
) -> None:
    source_parameter = profile.source_parameter_for(operation.key)
    if profile.selection_kind != "cloud_resource_derived_value" or source_parameter is None:
        return
    if not isinstance(source, Mapping):
        raise ResourceSelectorQueryError("selector_source_mismatch")
    source_mapping = cast(Mapping[str, Any], source)
    source_value = source_mapping.get("value")
    if not isinstance(source_value, str):
        raise ResourceSelectorQueryError("selector_source_mismatch")
    if source_parameter not in dynamic_parameters:
        return
    expected = _source_parameter_value(profile, source_value)
    actual = _normalize_parameter_value(dynamic_parameters.get(source_parameter))
    if actual != expected:
        raise ResourceSelectorQueryError("selector_source_mismatch")


def _normalize_dynamic_parameters(
    operation: QueryOperation,
    dynamic_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    if operation.request_kind == "multiApi":
        requests = dynamic_parameters.get("requests")
        if set(dynamic_parameters) != {"requests"} or not isinstance(requests, list):
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")
        return {
            "requests": [
                {
                    **(
                        {"customRequestKey": request["customRequestKey"]}
                        if isinstance(request, Mapping) and isinstance(request.get("customRequestKey"), str)
                        else {}
                    ),
                    "parameters": {
                        key: value
                        for key, value in request.get("parameters", {}).items()
                        if key in operation.batch_dynamic_parameters
                    },
                }
                for request in requests
                if isinstance(request, Mapping) and isinstance(request.get("parameters"), Mapping)
            ]
        }
    return _normalize_parameter_mapping(
        {key: value for key, value in dynamic_parameters.items() if key in operation.dynamic_parameters},
        operation=operation,
    )


def _normalize_parameter_mapping(
    dynamic_parameters: Mapping[str, Any],
    *,
    operation: QueryOperation | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dynamic_parameters.items():
        normalized = _normalize_parameter_value(value)
        if normalized is None:
            continue
        if key in {"Page", "PageNo", "PageNum", "PageNumber", "PageSize", "MaxItems", "MaxResults", "max-keys"}:
            maximum = 1000 if key in {"MaxItems", "max-keys"} else 100
            if not isinstance(normalized, int) or isinstance(normalized, bool) or not 1 <= normalized <= maximum:
                raise ResourceSelectorQueryError("selector_query_parameters_invalid")
        api_key = "bucket" if operation and operation.key == "oss.object.list" and key == "BucketName" else key
        result[api_key] = normalized
    return result


def _adapt_server_parameters(operation: QueryOperation, params: Mapping[str, Any]) -> dict[str, Any]:
    """Translate audited ORE console request shapes to fixed public APIs."""

    result = dict(params)
    if operation.key == "appflow.user_auth_config.dataapi.appflow.listuserauthconfigs":
        max_results = result.get("MaxResults")
        if isinstance(max_results, int) and not isinstance(max_results, bool):
            result["MaxResults"] = str(max_results)
        connector_version = result.get("ConnectorVersion")
        if isinstance(connector_version, (int, float)) and not isinstance(connector_version, bool):
            result["ConnectorVersion"] = str(connector_version)
        return result
    if operation.key == "oos.package.dataapi.ecs.describeinstances":
        instance_ids = result.get("InstanceIds")
        if isinstance(instance_ids, list):
            result["InstanceIds"] = json.dumps(instance_ids, ensure_ascii=False, separators=(",", ":"))
        return result
    if operation.key == "oos.application.dataapi.oos.listapplications":
        tags = result.get("Tags")
        if isinstance(tags, Mapping):
            result["Tags"] = json.dumps(tags, ensure_ascii=False, separators=(",", ":"))
        return result
    if operation.key == "hologres.instance.dataapi.hologram_2022.listinstances":
        result.pop("RegionId", None)
        return result
    if operation.key in {
        "oss.object.dataapi.oss.getbucket",
        "oss.object_version.dataapi.oss.listobjectversions",
    }:
        bucket_name = result.pop("BucketName", None)
        result.pop("RegionId", None)
        if isinstance(bucket_name, str) and bucket_name:
            result["bucket"] = bucket_name
        return result
    if operation.key not in {
        "ecs.vswitch.dataapi.ecs20160314.describeresources",
        "vpc.vpc.dataapi.ecs20160314.describeresources",
    }:
        return result

    region_id = result.get("RegionNo", result.get("RegionId"))
    page_size = result.get("MaxItems", 20)
    keyword = result.get("Keyword")
    adapted: dict[str, Any] = {}
    if isinstance(region_id, str) and region_id:
        adapted["RegionId"] = region_id
    if isinstance(page_size, int) and not isinstance(page_size, bool):
        adapted["PageSize"] = min(max(page_size, 1), 50)
    if isinstance(keyword, str) and keyword:
        adapted["VSwitchName" if operation.action == "DescribeVSwitches" else "VpcName"] = keyword
    return adapted


def _prepare_transport_request(
    operation: QueryOperation,
    params: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Materialize only server-declared path fields for an explicit ROA call."""

    wire_params = _adapt_server_parameters(operation, params)
    template = operation.pathname_template
    if template is None:
        return wire_params, None
    replacements: dict[str, str] = {}
    for name in operation.path_parameters:
        value = wire_params.pop(name, None)
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")
        replacements[name] = quote(value, safe="")
    try:
        pathname = template.format(**replacements)
    except (KeyError, ValueError) as exc:
        raise ResourceSelectorQueryError("selector_query_parameters_invalid") from exc
    # RegionId selects the trusted regional endpoint; it is not a query field
    # of the legacy ACR ROA operations.
    if operation.api_version == "2016-06-07" and operation.product.casefold() == "cr":
        wire_params.pop("RegionId", None)
    return wire_params, pathname


def _normalize_parameter_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        raise ResourceSelectorQueryError("selector_query_parameters_invalid")
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) > 2048:
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")
        return value
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        if len(value) > 100:
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")
        return [_normalize_parameter_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if value.get("__type__") == "__RepeatList__" and isinstance(value.get("list"), list):
            return _normalize_parameter_value(value["list"], depth=depth + 1)
        if len(value) > 100 or any(not isinstance(key, str) or len(key) > 128 for key in value):
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")
        return {key: _normalize_parameter_value(item, depth=depth + 1) for key, item in value.items()}
    raise ResourceSelectorQueryError("selector_query_parameters_invalid")


def _operation_region_id(
    metadata: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    operation: QueryOperation | None = None,
) -> str | None:
    selected_region: str | None = None
    for value in (
        metadata.get("RegionId"),
        params.get("RegionId"),
        params.get("regionId"),
        params.get("region"),
    ):
        if isinstance(value, str) and value:
            selected_region = value
            break
    if operation is None:
        return selected_region

    product = operation.product.casefold()
    if product == "cloudsso":
        if selected_region in {"cn-shanghai", "cn-hongkong", "us-west-1"}:
            return selected_region
        if selected_region and selected_region.startswith("us-"):
            return "us-west-1"
        if selected_region and not selected_region.startswith("cn-"):
            return "cn-hongkong"
        return "cn-shanghai"
    if product == "resourcedirectorymaster":
        if selected_region and not selected_region.startswith("cn-"):
            return "ap-southeast-1"
        return "cn-shanghai"
    return selected_region


def _validate_dynamic_metadata_constraints(
    profile: SelectorProfile,
    operation: QueryOperation,
    dynamic_parameters: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    parameter_sets: list[Mapping[str, Any]] = [dynamic_parameters]
    if operation.request_kind == "multiApi":
        requests = dynamic_parameters.get("requests")
        parameter_sets = []
        if isinstance(requests, list):
            parameter_sets = [
                cast(Mapping[str, Any], request["parameters"])
                for request in requests
                if isinstance(request, Mapping) and isinstance(request.get("parameters"), Mapping)
            ]
    for metadata_key, api_parameter in operation.metadata_parameters:
        expected = metadata.get(metadata_key)
        if (
            operation.key == "ecs.vswitch.dataapi.serverless.describenamespaceresources"
            and metadata_key == "SAENamespaceId"
            and isinstance(expected, str)
            and expected
        ):
            region_id = metadata.get("RegionId")
            if isinstance(region_id, str) and region_id and not expected.startswith("{}:".format(region_id)):
                expected = "{}:{}".format(region_id, expected.rsplit(":", 1)[-1])
        for parameters in parameter_sets:
            if api_parameter not in parameters:
                continue
            if expected is None:
                raise ResourceSelectorQueryError("selector_query_parameters_invalid")
            actual = _normalize_parameter_value(parameters.get(api_parameter))
            if actual != _normalize_parameter_value(expected):
                raise ResourceSelectorQueryError("selector_query_parameters_invalid")
    if profile.selector_id != "oss.bucket_object" or operation.key != "oss.object.list":
        return
    fixed_prefix = metadata.get("Prefix")
    requested_prefix = dynamic_parameters.get("prefix")
    if isinstance(fixed_prefix, str) and fixed_prefix:
        if requested_prefix is not None and (
            not isinstance(requested_prefix, str) or not requested_prefix.startswith(fixed_prefix)
        ):
            raise ResourceSelectorQueryError("selector_query_parameters_invalid")
    if (
        metadata.get("Delimiter") is not None
        and "delimiter" in dynamic_parameters
        and dynamic_parameters.get("delimiter") != metadata.get("Delimiter")
    ):
        raise ResourceSelectorQueryError("selector_query_parameters_invalid")


def _validate_dynamic_fixed_constraints(
    operation: QueryOperation,
    dynamic_parameters: Mapping[str, Any],
) -> None:
    parameter_sets: list[Mapping[str, Any]] = [dynamic_parameters]
    if operation.request_kind == "multiApi":
        requests = dynamic_parameters.get("requests")
        parameter_sets = []
        if isinstance(requests, list):
            parameter_sets = [
                cast(Mapping[str, Any], request["parameters"])
                for request in requests
                if isinstance(request, Mapping) and isinstance(request.get("parameters"), Mapping)
            ]
    for api_parameter, expected in operation.fixed_parameters:
        for parameters in parameter_sets:
            if api_parameter in parameters and _normalize_parameter_value(parameters[api_parameter]) != expected:
                raise ResourceSelectorQueryError("selector_query_parameters_invalid")


def project_ore_response(operation_key: str | None, response: Mapping[str, Any]) -> dict[str, Any]:
    if operation_key is None:
        raise ResourceSelectorQueryError("selector_query_response_invalid")
    if operation_key == "ecs.vswitch.dataapi.ecs20160314.describeresources":
        container = response.get("VSwitches")
        switches = _as_item_list(container.get("VSwitch") if isinstance(container, Mapping) else None)
        return {
            **_pick(response, "RequestId"),
            "EstimatedTotal": response.get("TotalCount", len(switches)),
            "Resources": {
                "Resource": [
                    {
                        **_pick(item, "CidrBlock", "Ipv6CidrBlock", "ZoneId", "VpcId", "Status"),
                        "ResourceId": item.get("VSwitchId"),
                        "ResourceName": item.get("VSwitchName"),
                    }
                    for item in switches
                ]
            },
            "Truncated": False,
        }
    if operation_key == "vpc.vpc.dataapi.ecs20160314.describeresources":
        container = response.get("Vpcs")
        vpcs = _as_item_list(container.get("Vpc") if isinstance(container, Mapping) else None)
        return {
            **_pick(response, "RequestId"),
            "EstimatedTotal": response.get("TotalCount", len(vpcs)),
            "Resources": {
                "Resource": [
                    {
                        **_pick(item, "CidrBlock", "Ipv6CidrBlock", "Status", "ResourceGroupId"),
                        "ResourceId": item.get("VpcId"),
                        "ResourceName": item.get("VpcName"),
                    }
                    for item in vpcs
                ]
            },
            "Truncated": False,
        }
    if operation_key == "ecs.instance.list":
        result = _pick(response, "PageNumber", "PageSize", "TotalCount", "NextToken", "RequestId")
        result["Instances"] = _project_nested_collection(
            response.get("Instances"),
            "Instance",
            ("InstanceId", "InstanceName", "Status", "InstanceType", "ZoneId"),
        )
        return result
    if operation_key == "vpc.vswitch.list":
        result = _pick(response, "PageNumber", "PageSize", "TotalCount", "NextToken", "RequestId")
        result["VSwitches"] = _project_nested_collection(
            response.get("VSwitches"),
            "VSwitch",
            ("VSwitchId", "VSwitchName", "CidrBlock", "Ipv6CidrBlock", "ZoneId", "VpcId", "Status"),
        )
        return result
    if operation_key == "vpc.instance_type.available":
        result = _pick(response, "RequestId")
        result["AvailableZones"] = _project_available_zones(response.get("AvailableZones"))
        return result
    if operation_key == "vpc.vswitch.dataapi.ecs.describesecuritygroups":
        result = _pick(response, "RequestId", "PageNumber", "PageSize", "TotalCount")
        result["SecurityGroups"] = _project_nested_collection(
            response.get("SecurityGroups"),
            "SecurityGroup",
            ("SecurityGroupId", "VpcId"),
        )
        return result
    if operation_key in {
        "vpc.vswitch.dataapi.serverless.describenamespaceresources",
        "ecs.vswitch.dataapi.serverless.describenamespaceresources",
    }:
        data = response.get("Data")
        return {
            **_pick(response, "RequestId"),
            "Data": _pick(data, "NamespaceId", "VpcId") if isinstance(data, Mapping) else {},
        }
    if operation_key == "rds.instance.list":
        result = _pick(response, "PageNumber", "PageRecordCount", "TotalRecordCount", "RequestId")
        for container_key in ("Items", "DBInstances"):
            if container_key in response:
                result[container_key] = _project_nested_collection(
                    response.get(container_key),
                    "DBInstance",
                    (
                        "DBInstanceId",
                        "DBInstanceDescription",
                        "DBInstanceStatus",
                        "Engine",
                        "EngineVersion",
                        "ZoneId",
                        "VpcId",
                    ),
                )
        return result
    if operation_key == "redis.instance.list":
        result = _pick(response, "PageNumber", "PageSize", "TotalCount", "RequestId")
        result["Instances"] = _project_nested_collection(
            response.get("Instances"),
            "KVStoreInstance",
            (
                "InstanceId",
                "InstanceName",
                "InstanceStatus",
                "InstanceClass",
                "ChargeType",
                "NetworkType",
                "VpcId",
                "ZoneId",
            ),
        )
        return result
    if operation_key == "redis.connection.list":
        result = _pick(response, "RequestId")
        result["NetInfoItems"] = _project_nested_collection(
            response.get("NetInfoItems"),
            "InstanceNetInfo",
            ("ConnectionString", "Port", "IPType", "VPCId"),
        )
        return result
    if operation_key == "oss.bucket.list":
        root = response.get("ListAllMyBucketsResult")
        if not isinstance(root, Mapping):
            if not any(key in response for key in ("buckets", "is_truncated", "next_marker", "prefix", "marker")):
                return _pick(response, "RequestId")
            result_root = _remap_fields(
                response,
                {
                    "is_truncated": "IsTruncated",
                    "marker": "Marker",
                    "max_keys": "MaxKeys",
                    "next_marker": "NextMarker",
                    "prefix": "Prefix",
                },
            )
            result_root["Buckets"] = {
                "Bucket": _remap_items(
                    response.get("buckets"),
                    {
                        "creation_date": "CreationDate",
                        "location": "Location",
                        "name": "Name",
                        "region": "Region",
                        "storage_class": "StorageClass",
                    },
                )
            }
            return {"ListAllMyBucketsResult": result_root}
        result_root = _pick(root, "IsTruncated", "Marker", "MaxKeys", "NextMarker", "Prefix")
        result_root["Buckets"] = _project_nested_collection(
            root.get("Buckets"),
            "Bucket",
            ("CreationDate", "Location", "Name", "Region", "StorageClass"),
        )
        return {"ListAllMyBucketsResult": result_root, **_pick(response, "RequestId")}
    if operation_key in {
        "oss.object.list",
        "oss.object.dataapi.oss.getbucket",
    }:
        root = response.get("ListBucketResult")
        if not isinstance(root, Mapping):
            if not any(
                key in response
                for key in ("contents", "common_prefixes", "is_truncated", "next_marker", "prefix", "marker")
            ):
                return _pick(response, "RequestId")
            result_root = _remap_fields(
                response,
                {
                    "delimiter": "Delimiter",
                    "is_truncated": "IsTruncated",
                    "marker": "Marker",
                    "max_keys": "MaxKeys",
                    "name": "Name",
                    "next_marker": "NextMarker",
                    "prefix": "Prefix",
                },
            )
            result_root["Contents"] = _remap_items(
                response.get("contents"),
                {
                    "etag": "ETag",
                    "key": "Key",
                    "last_modified": "LastModified",
                    "size": "Size",
                    "storage_class": "StorageClass",
                    "object_type": "Type",
                },
            )
            result_root["CommonPrefixes"] = _remap_items(
                response.get("common_prefixes"),
                {"prefix": "Prefix"},
            )
            return {"ListBucketResult": result_root}
        result_root = _pick(
            root,
            "Delimiter",
            "IsTruncated",
            "Marker",
            "MaxKeys",
            "Name",
            "NextMarker",
            "Prefix",
        )
        result_root["Contents"] = _project_items(
            root.get("Contents"),
            ("ETag", "Key", "LastModified", "Size", "StorageClass", "Type"),
        )
        result_root["CommonPrefixes"] = _project_items(root.get("CommonPrefixes"), ("Prefix",))
        return {"ListBucketResult": result_root, **_pick(response, "RequestId")}
    if operation_key == "oss.object_version.dataapi.oss.listobjectversions":
        root = response.get("ListVersionsResult")
        if not isinstance(root, Mapping):
            if not any(key in response for key in ("name", "prefix", "is_truncated", "version")):
                return _pick(response, "RequestId")
            result_root = _remap_fields(
                response,
                {
                    "name": "Name",
                    "prefix": "Prefix",
                    "is_truncated": "IsTruncated",
                },
            )
            result_root["Version"] = _project_oss_versions(response.get("version"), snake_case=True)
            if isinstance(result_root.get("IsTruncated"), bool):
                result_root["IsTruncated"] = str(result_root["IsTruncated"]).lower()
            return {"ListVersionsResult": result_root}
        result_root = _pick(root, "Name", "Prefix", "IsTruncated")
        result_root["Version"] = _project_oss_versions(root.get("Version"), snake_case=False)
        return {"ListVersionsResult": result_root, **_pick(response, "RequestId")}
    if operation_key == "acr.repo.list":
        result = _pick(response, "RequestId")
        for data_key in ("data", "Data"):
            data = response.get(data_key)
            if not isinstance(data, Mapping):
                continue
            projected_data = _pick(data, "page", "pageSize", "total", "Page", "PageSize", "TotalCount")
            repos = data.get("repos", data.get("Repos"))
            projected_repos = _project_items(
                repos,
                ("repoId", "repoName", "repoNamespace", "repoStatus", "regionId", "repoAuthorizeType"),
            )
            for item, original in zip(_as_item_list(projected_repos), _as_item_list(repos)):
                domains = original.get("repoDomainList") if isinstance(original, Mapping) else None
                if isinstance(domains, Mapping):
                    item["repoDomainList"] = _pick(domains, "internal", "public", "vpc")
            projected_data["repos" if "repos" in data else "Repos"] = projected_repos
            result[data_key] = projected_data
        return result
    schema = _response_projection_registry().get(operation_key)
    if not isinstance(schema, Mapping):
        raise ResourceSelectorQueryError("selector_query_response_invalid")
    projected = _project_response_value(response, schema)
    if not isinstance(projected, dict):
        raise ResourceSelectorQueryError("selector_query_response_invalid")
    return projected


@lru_cache(maxsize=1)
def _response_projection_registry() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "ore-response-projections.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    operations = payload.get("operations") if isinstance(payload, dict) else None
    if not isinstance(operations, dict):
        raise ResourceSelectorQueryError("selector_query_response_invalid")
    return operations


def _project_response_value(value: Any, schema: Any, *, depth: int = 0) -> Any:
    """Apply an operation-specific structural allowlist to an API response."""

    if depth > 12:
        raise ResourceSelectorQueryError("selector_query_response_invalid")
    if schema is True:
        return _project_response_scalar(value)
    if isinstance(schema, list):
        if not isinstance(value, list):
            return []
        if not schema:
            return []
        return [_project_response_value(item, schema[0], depth=depth + 1) for item in value[:200]]
    if isinstance(schema, Mapping):
        if not isinstance(value, Mapping):
            return {}
        wildcard = schema.get("*")
        if wildcard is not None:
            return {
                key: _project_response_value(item, wildcard, depth=depth + 1)
                for index, (key, item) in enumerate(value.items())
                if index < MAX_MULTI_API_REQUESTS and isinstance(key, str) and len(key) <= 256
            }
        return {
            key: _project_response_value(value[key], child, depth=depth + 1)
            for key, child in schema.items()
            if isinstance(key, str) and key in value
        }
    raise ResourceSelectorQueryError("selector_query_response_invalid")


def _project_response_scalar(value: Any) -> str | bool | int | float | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4096]
    return None


_SENSITIVE_RESPONSE_KEYS = {
    "accesskeyid",
    "accesskeysecret",
    "authorization",
    "cookie",
    "password",
    "privatekey",
    "secret",
    "securitytoken",
}


def _sanitize_response_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, list):
        return [_sanitize_response_value(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 300 or not isinstance(key, str) or len(key) > 256:
                break
            normalized_key = key.replace("_", "").replace("-", "").lower()
            if normalized_key in _SENSITIVE_RESPONSE_KEYS:
                continue
            result[key] = _sanitize_response_value(item, depth=depth + 1)
        return result
    return None


def _pick(value: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value}


def _remap_fields(value: Mapping[str, Any], field_map: Mapping[str, str]) -> dict[str, Any]:
    return {target: value[source] for source, target in field_map.items() if source in value}


def _remap_items(value: object, field_map: Mapping[str, str]) -> list[dict[str, Any]]:
    return [_remap_fields(item, field_map) for item in _as_item_list(value)]


def _project_oss_versions(value: object, *, snake_case: bool) -> list[dict[str, Any]] | dict[str, Any]:
    field_map = (
        {
            "etag": "ETag",
            "key": "Key",
            "last_modified": "LastModified",
            "size": "Size",
            "storage_class": "StorageClass",
            "version_id": "VersionId",
        }
        if snake_case
        else {
            "ETag": "ETag",
            "Key": "Key",
            "LastModified": "LastModified",
            "Size": "Size",
            "StorageClass": "StorageClass",
            "VersionId": "VersionId",
        }
    )
    owner_key = "owner" if snake_case else "Owner"
    owner_fields = (
        {"display_name": "DisplayName", "id": "ID"}
        if snake_case
        else {
            "DisplayName": "DisplayName",
            "ID": "ID",
        }
    )

    def project(item: Mapping[str, Any]) -> dict[str, Any]:
        result = _remap_fields(item, field_map)
        owner = item.get(owner_key)
        if isinstance(owner, Mapping):
            result["Owner"] = _remap_fields(owner, owner_fields)
        return result

    if isinstance(value, list):
        return [project(item) for item in value[:200] if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return project(cast(Mapping[str, Any], value))
    return []


def _project_nested_collection(value: object, key: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {key: []}
    typed_value = cast(Mapping[str, Any], value)
    return {key: _project_items(typed_value.get(key), allowed)}


def _project_available_zones(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"AvailableZone": []}
    value = cast(Mapping[str, Any], value)
    zones: list[dict[str, Any]] = []
    for zone in _as_item_list(value.get("AvailableZone")):
        projected_zone = _pick(zone, "ZoneId", "Status")
        resources = zone.get("AvailableResources")
        projected_resources: list[dict[str, Any]] = []
        if isinstance(resources, Mapping):
            for resource in _as_item_list(resources.get("AvailableResource")):
                projected_resource = _pick(resource, "Type", "Status")
                supported = resource.get("SupportedResources")
                if isinstance(supported, Mapping):
                    projected_resource["SupportedResources"] = {
                        "SupportedResource": _project_items(
                            supported.get("SupportedResource"),
                            ("Value", "Status", "Min", "Max", "Unit"),
                        )
                    }
                projected_resources.append(projected_resource)
        projected_zone["AvailableResources"] = {"AvailableResource": projected_resources}
        zones.append(projected_zone)
    return {"AvailableZone": zones}


def _project_items(value: object, allowed: tuple[str, ...]) -> Any:
    if isinstance(value, list):
        return [_pick(cast(Mapping[str, Any], item), *allowed) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return _pick(cast(Mapping[str, Any], value), *allowed)
    return []


def _as_item_list(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [cast(dict[str, Any], item) for item in value if isinstance(item, dict)]
    return [cast(dict[str, Any], value)] if isinstance(value, dict) else []


def _ecs_has_public_ip(item: Mapping[str, Any]) -> bool:
    public = item.get("PublicIpAddress")
    if isinstance(public, Mapping):
        ip_addresses = public.get("IpAddress")
        if isinstance(ip_addresses, list) and any(isinstance(value, str) and value for value in ip_addresses):
            return True
    eip = item.get("EipAddress")
    return isinstance(eip, Mapping) and isinstance(eip.get("IpAddress"), str) and bool(eip.get("IpAddress"))


def _truthy_cloud_assistant_status(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() in {"true", "1"})


def _operation_can_return_selection_values(profile: SelectorProfile, operation_key: str) -> bool:
    if operation_key in {
        "ecs.instance.list",
        "vpc.vswitch.list",
        "rds.instance.list",
        "redis.instance.list",
        "redis.connection.list",
        "acr.repo.list",
    }:
        return True
    if operation_key == "oss.bucket.list":
        return profile.selector_id == "oss.bucket"
    if operation_key == "oss.object.list":
        return profile.selector_id in {"oss.bucket_object", "oss.object"}
    if profile.selector_id in {"acr.repo_attribute", "ehpc.mount_target"}:
        return True
    operation = next((item for item in profile.operations if item.key == operation_key), None)
    return bool(
        operation is not None
        and operation.response_projector is not None
        and _candidate_paths(operation.response_projector, profile.value_fields)
    )


def _selection_values(
    profile: SelectorProfile,
    operation_key: str,
    response: Mapping[str, Any],
    dynamic_parameters: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> set[str]:
    values: set[str] = set()
    if operation_key == "ecs.instance.list":
        values.update(_nested_item_values(response, "Instances", "Instance", "InstanceId"))
    elif operation_key == "vpc.vswitch.list":
        values.update(_nested_item_values(response, "VSwitches", "VSwitch", "VSwitchId"))
    elif operation_key == "rds.instance.list":
        values.update(_nested_item_values(response, "Items", "DBInstance", "DBInstanceId"))
        values.update(_nested_item_values(response, "DBInstances", "DBInstance", "DBInstanceId"))
    elif operation_key == "redis.instance.list":
        values.update(_nested_item_values(response, "Instances", "KVStoreInstance", "InstanceId"))
    elif operation_key == "redis.connection.list":
        container = response.get("NetInfoItems")
        for item in _as_item_list(container.get("InstanceNetInfo") if isinstance(container, Mapping) else None):
            host = item.get("ConnectionString")
            port = item.get("Port")
            if isinstance(host, str) and host and isinstance(port, (str, int)):
                values.add("redis://{}:{}".format(host, port))
    elif operation_key == "oss.bucket.list" and profile.selector_id == "oss.bucket":
        values.update(_oss_bucket_values(response, metadata))
    elif operation_key == "oss.object.list":
        bucket = dynamic_parameters.get("BucketName")
        root = response.get("ListBucketResult")
        if isinstance(bucket, str) and isinstance(root, Mapping):
            object_type = metadata.get("ObjectType")
            collection_key, value_key = ("CommonPrefixes", "Prefix") if object_type == "dir" else ("Contents", "Key")
            for item in _as_item_list(root.get(collection_key)):
                object_name = item.get(value_key)
                if isinstance(object_name, str) and object_name:
                    values.add("oss://{}/{}".format(bucket, object_name))
    elif profile.selector_id == "oss.object":
        root = response.get("ListBucketResult")
        if isinstance(root, Mapping):
            bucket = metadata.get("BucketName")
            for item in _as_item_list(root.get("Contents")):
                object_name = item.get("Key")
                if not isinstance(object_name, str) or not object_name:
                    continue
                if metadata.get("ValueType") == "OSSUrl" and isinstance(bucket, str) and bucket:
                    values.add("oss://{}/{}".format(bucket, object_name))
                else:
                    values.add(object_name)
    elif operation_key == "acr.repo.list":
        attribute = metadata.get("Attribute")
        for data_key in ("data", "Data"):
            data = response.get(data_key)
            if not isinstance(data, Mapping):
                continue
            for item in _as_item_list(data.get("repos", data.get("Repos"))):
                value = _acr_value(item, attribute)
                if value:
                    values.add(value)
    elif profile.selector_id == "acr.repo_attribute":
        attribute = metadata.get("Attribute")
        for item in _iter_mappings(response):
            value = _acr_value(item, attribute)
            if value:
                values.add(value)
    elif profile.selector_id == "ehpc.mount_target":
        source_volume = metadata.get("VolumeId")
        file_system_list = response.get("FileSystemList")
        file_systems = (
            _as_item_list(file_system_list.get("FileSystems")) if isinstance(file_system_list, Mapping) else []
        )
        for file_system in file_systems:
            if file_system.get("FileSystemId") != source_volume:
                continue
            mount_target_list = file_system.get("MountTargetList")
            mount_targets = (
                _as_item_list(mount_target_list.get("MountTargets")) if isinstance(mount_target_list, Mapping) else []
            )
            values.update(
                value for item in mount_targets if isinstance((value := item.get("MountTargetDomain")), str) and value
            )
    else:
        operation = next((item for item in profile.operations if item.key == operation_key), None)
        if operation is not None and operation.response_projector is not None:
            for path in _candidate_paths(operation.response_projector, profile.value_fields):
                for candidate in _values_at_projection_path(response, path):
                    if isinstance(candidate, str) and candidate:
                        values.add(candidate)
                    elif isinstance(candidate, int) and not isinstance(candidate, bool):
                        values.add(str(candidate))
    return {value for value in values if validate_answer_value(profile, value, metadata=metadata) is None}


@lru_cache(maxsize=None)
def _candidate_paths(projector: str, value_fields: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Derive exact candidate paths from an explicit response projection."""

    schema = _response_projection_registry().get(projector)
    normalized_fields = {field.replace("_", "").replace("-", "").lower() for field in value_fields}
    matches: list[tuple[str, ...]] = []

    def visit(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, list):
            if node:
                visit(node[0], (*path, "*"))
            return
        if not isinstance(node, Mapping):
            return
        for key, child in node.items():
            if not isinstance(key, str):
                continue
            next_path = (*path, key)
            if child is True and key.replace("_", "").replace("-", "").lower() in normalized_fields:
                matches.append(next_path)
            else:
                visit(child, next_path)

    visit(schema, ())
    if not matches:
        return ()
    # A response model can repeat an identifier name inside subordinate
    # objects (for example an attachment's InstanceId).  The selector's row
    # value is the shallowest occurrence; deeper same-name fields are not
    # candidates unless a profile has a dedicated special-case extractor.
    minimum_depth = min(len(path) for path in matches)
    return tuple(path for path in matches if len(path) == minimum_depth)


def _values_at_projection_path(value: Any, path: tuple[str, ...]) -> list[Any]:
    if not path:
        return [value]
    head, *tail = path
    remainder = tuple(tail)
    if head == "*":
        if isinstance(value, list):
            items = value
        elif isinstance(value, Mapping):
            items = list(value.values())
        else:
            return []
        return [candidate for item in items for candidate in _values_at_projection_path(item, remainder)]
    if not isinstance(value, Mapping) or head not in value:
        return []
    return _values_at_projection_path(value[head], remainder)


def _iter_mappings(value: object, *, depth: int = 0):
    if depth > 10:
        return
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _iter_mappings(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value[:200]:
            yield from _iter_mappings(item, depth=depth + 1)


def _oss_bucket_values(response: Mapping[str, Any], metadata: Mapping[str, Any]) -> set[str]:
    root = response.get("ListAllMyBucketsResult")
    buckets = root.get("Buckets") if isinstance(root, Mapping) else None
    expected_region = metadata.get("RegionId")
    values: set[str] = set()
    for item in _as_item_list(buckets.get("Bucket") if isinstance(buckets, Mapping) else None):
        name = item.get("Name")
        if not isinstance(name, str):
            continue
        if isinstance(expected_region, str) and expected_region:
            if item.get("Region") != expected_region or item.get("Location") != "oss-{}".format(expected_region):
                continue
        values.add(name)
    return values


def _nested_item_values(response: Mapping[str, Any], container_key: str, item_key: str, value_key: str) -> set[str]:
    container = response.get(container_key)
    return {
        value
        for item in _as_item_list(container.get(item_key) if isinstance(container, Mapping) else None)
        if isinstance((value := item.get(value_key)), str) and value
    }


def _supported_vswitch_zones(response: Mapping[str, Any], requested_types: list[Any]) -> set[str]:
    container = response.get("AvailableZones")
    zones = _as_item_list(container.get("AvailableZone") if isinstance(container, Mapping) else None)
    supported: set[str] = set()
    for zone in zones:
        resources = zone.get("AvailableResources")
        resource_items = _as_item_list(resources.get("AvailableResource") if isinstance(resources, Mapping) else None)
        if any(
            item.get("Value") in requested_types
            for resource in resource_items
            for item in _as_item_list(
                resource.get("SupportedResources", {}).get("SupportedResource")
                if isinstance(resource.get("SupportedResources"), Mapping)
                else None
            )
        ):
            zone_id = zone.get("ZoneId")
            if isinstance(zone_id, str):
                supported.add(zone_id)
    return supported


def _acr_value(item: Mapping[str, Any], attribute: object) -> str | None:
    if isinstance(attribute, str) and attribute in {"repoId", "repoName"}:
        value = item.get(attribute)
        if attribute == "repoId" and isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value if isinstance(value, str) and value else None
    domains = item.get("repoDomainList")
    domain_key = {
        "internalDomain": "internal",
        "publicDomain": "public",
        "vpcDomain": "vpc",
    }.get(attribute if isinstance(attribute, str) else "")
    if domain_key is not None:
        value = domains.get(domain_key) if isinstance(domains, Mapping) else None
        return value if isinstance(value, str) and value else None
    public = domains.get("public") if isinstance(domains, Mapping) else None
    namespace = item.get("repoNamespace")
    name = item.get("repoName")
    if all(isinstance(value, str) and value for value in (public, namespace, name)):
        return "{}/{}/{}".format(public, namespace, name)
    return None


def _validate_page_token(operation_key: str, parameters: Mapping[str, Any], allowed: set[str]) -> None:
    token_key = {
        "ecs.instance.list": "NextToken",
        "oss.bucket.list": "marker",
        "oss.object.list": "marker",
    }.get(operation_key)
    token_keys = (
        (token_key,)
        if token_key
        else (
            "NextToken",
            "nextToken",
            "Marker",
            "marker",
        )
    )
    for key in token_keys:
        if key not in parameters or parameters.get(key) in (None, ""):
            continue
        token = parameters.get(key)
        if not isinstance(token, str) or token not in allowed:
            raise ResourceSelectorQueryError("selector_pagination_token_invalid")


def _next_page_tokens(operation_key: str, response: Mapping[str, Any]) -> set[str]:
    value: object = None
    if operation_key == "ecs.instance.list":
        value = response.get("NextToken")
    elif operation_key == "oss.bucket.list":
        root = response.get("ListAllMyBucketsResult")
        value = root.get("NextMarker") if isinstance(root, Mapping) else None
    elif operation_key == "oss.object.list":
        root = response.get("ListBucketResult")
        value = root.get("NextMarker") if isinstance(root, Mapping) else None
    values = {value} if isinstance(value, str) and value else set()
    for item in _iter_mappings(response):
        for key in ("NextToken", "nextToken", "NextMarker", "nextMarker", "NextPageToken", "nextPageToken"):
            candidate = item.get(key)
            if isinstance(candidate, str) and candidate:
                values.add(candidate)
    return values


async def call_aliyun_api(
    product: str,
    action: str,
    region_id: str | None,
    params: dict[str, Any],
    *,
    api_version: str | None = None,
    style: str | None = None,
    method: str | None = None,
    pathname: str | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the fixed API through the shared Alibaba Cloud runtime."""

    from iac_code.config import get_config_dir
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
    from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services

    services = create_aliyun_runtime_services(cache_dir=get_config_dir() / "openmeta-cache")
    credentials = CloudCredentials()

    def credential_provider() -> Any:
        credential = credentials.get_provider("aliyun")
        if credential is not None and credential.mode == "OAuth":
            from iac_code.services.providers.aliyun import AliyunCredentials

            credential = AliyunCredentials.refresh_oauth_if_needed(credential)
        return credential

    services.credential_provider = credential_provider
    api = AliyunApi(services=services)
    tool_input: dict[str, Any] = {"product": product, "action": action, "params": params}
    if api_version:
        tool_input["version"] = api_version
    if style:
        tool_input["style"] = style
    if method:
        tool_input["method"] = method
    if pathname:
        tool_input["pathname"] = pathname
    if body is not None:
        tool_input["body"] = body
    if region_id:
        tool_input["region_id"] = region_id
    try:
        result = await api._execute_internal_trusted(tool_input, ToolContext())
    finally:
        await services.aclose()
    if result.is_error:
        raise ResourceSelectorQueryError(result.content)
    try:
        value = json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise ResourceSelectorQueryError("selector_query_response_invalid") from exc
    if not isinstance(value, dict):
        raise ResourceSelectorQueryError("selector_query_response_invalid")
    return value
