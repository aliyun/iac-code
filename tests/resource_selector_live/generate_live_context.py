"""Generate a reviewable, non-secret starting context for the opt-in live selector smoke."""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from iac_code.resource_selector.profiles import (
    SERVER_AUXILIARY_OPERATIONS,
    QueryOperation,
    SelectorProfile,
    iter_profiles,
)
from iac_code.resource_selector.query import (
    _fixed_query_parameters,
    _normalize_dynamic_parameters,
    _operation_region_id,
)

ROOT = Path(__file__).resolve().parents[1] / "resource_selector"
CASES = {
    item["selectorId"]: item for item in json.loads((ROOT / "e2e-cases.json").read_text(encoding="utf-8"))["cases"]
}

_PAGINATION_PARAMETERS = {
    "Page",
    "PageNo",
    "PageNum",
    "PageNumber",
    "PageSize",
    "MaxItems",
    "MaxRecords",
    "MaxResults",
    "max-keys",
    "pageNumber",
    "pageSize",
}
_RUNTIME_PARENT_PARAMETERS = {"ParentFolderId"}
_REPEATED_FILTER_PARAMETER = re.compile(r"^Filter\.(\d+)\.(Name|Values?\.\d+)$")


def _minimum_metadata(profile: SelectorProfile, case: dict[str, Any]) -> dict[str, Any]:
    """Keep only context that a real live request cannot derive on its own.

    Offline browser cases intentionally contain deterministic values for the
    required context and audited request branches. Replaying placeholder
    resource ids against a cloud account would produce misleading NotFound
    results, so a live starting context keeps only RegionId, required parent
    context, and component-owned fixed/default parameters.
    """

    required = set(profile.metadata_schema.get("required", ()))
    if "RegionId" in profile.metadata_schema.get("properties", {}):
        required.add("RegionId")
    return {key: deepcopy(value) for key, value in case["metadata"].items() if key in required}


def _is_offline_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.casefold()
    return lowered == "test-value" or lowered == "test-source" or lowered.startswith("value-")


def _minimum_request_parameters(
    operation: QueryOperation,
    parameters: dict[str, Any],
    *,
    profile: SelectorProfile,
) -> dict[str, Any]:
    metadata_parameters = {api_parameter.casefold() for _, api_parameter in operation.metadata_parameters}
    wire_source_parameter = profile.source_parameter_for(operation.key)
    source_parameter = wire_source_parameter.casefold() if wire_source_parameter else None
    result: dict[str, Any] = {}
    for key, value in parameters.items():
        folded = key.casefold()
        # Trusted metadata/source values are rebound by _fixed_query_parameters.
        if folded in metadata_parameters or folded == source_parameter:
            continue
        if value in (None, "", [], {}):
            continue
        if _is_offline_placeholder(value) and key not in _RUNTIME_PARENT_PARAMETERS:
            continue
        if key in _PAGINATION_PARAMETERS or not _is_offline_placeholder(value):
            result[key] = deepcopy(value)
    # ORE represents ComputeNest filters as repeated Name/Value pairs.  Once an
    # offline placeholder Value is removed, retaining its Name would produce an
    # invalid live request with an incomplete filter pair.
    for key in tuple(result):
        if not key.endswith(".Name"):
            continue
        prefix = key[: -len(".Name")]
        source_supplies_value = bool(
            wire_source_parameter
            and (
                wire_source_parameter.startswith("{}.Value.".format(prefix))
                or wire_source_parameter.startswith("{}.Values.".format(prefix))
            )
        )
        if (
            not any(
                candidate.startswith("{}.Value.".format(prefix)) or candidate.startswith("{}.Values.".format(prefix))
                for candidate in result
            )
            and not source_supplies_value
        ):
            result.pop(key)
    # convertRequestToArray emits a continuous 1-based sequence.  The offline
    # case contains every optional filter; after removing placeholder pairs,
    # compact the survivors to reproduce what the component emits with the
    # minimal live metadata.
    groups: dict[int, list[tuple[str, Any]]] = {}
    for key, value in tuple(result.items()):
        match = _REPEATED_FILTER_PARAMETER.fullmatch(key)
        if match:
            groups.setdefault(int(match.group(1)), []).append((match.group(2), value))
            result.pop(key)
    for new_index, old_index in enumerate(sorted(groups), start=1):
        for suffix, value in groups[old_index]:
            result["Filter.{}.{}".format(new_index, suffix)] = value
    return result


def dynamic_parameters(
    operation: QueryOperation,
    case: dict[str, Any],
    profile: SelectorProfile,
) -> dict[str, Any]:
    request = next(item for item in case["expectedRequests"] if item["operationKey"] == operation.key)
    parameters = deepcopy(request["parameters"])
    if operation.request_kind != "multiApi":
        return _minimum_request_parameters(operation, parameters, profile=profile)
    requests = parameters.get("requests")
    if not isinstance(requests, list):
        return {"requests": []}
    return {
        "requests": [
            {
                **(
                    {"customRequestKey": item["customRequestKey"]}
                    if isinstance(item.get("customRequestKey"), str)
                    else {}
                ),
                "parameters": _minimum_request_parameters(
                    operation,
                    item["parameters"],
                    profile=profile,
                ),
            }
            for item in requests
            if isinstance(item, dict) and isinstance(item.get("parameters"), dict)
        ]
    }


def generate() -> dict[str, Any]:
    operations: dict[str, Any] = {}
    for profile in iter_profiles(include_disabled=False):
        case = CASES[profile.selector_id]
        metadata = _minimum_metadata(profile, case)
        for operation in profile.operations:
            params = _fixed_query_parameters(
                profile,
                operation,
                metadata,
                case["source"],
            )
            params.update(_normalize_dynamic_parameters(operation, dynamic_parameters(operation, case, profile)))
            if operation.action == "DescribeNamespaceResources" and not params.get("NamespaceId"):
                params["NamespaceId"] = "cn-hangzhou:test-namespace"
            if operation.action == "CheckCdnDomainICP" and not params.get("DomainName"):
                # The API requires a domain even when the configured account
                # owns none.  A neutral public example lets the smoke classify
                # that account-context limitation without skipping the call.
                params["DomainName"] = "example.com"
            operations[operation.key] = {
                "selectorId": profile.selector_id,
                "operationKey": operation.key,
                "apiVersion": operation.api_version,
                "regionId": _operation_region_id(metadata, params, operation=operation),
                "params": params,
            }
    for operation in SERVER_AUXILIARY_OPERATIONS:
        operations[operation.key] = {
            "selectorId": "ecs.instance",
            "operationKey": operation.key,
            "apiVersion": operation.api_version,
            "regionId": "cn-hangzhou",
            "params": {"InstanceId": ["i-test0001"]},
        }
    return {"schemaVersion": 1, "operations": operations}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = generate()
    args.output.write_text("{}\n".format(json.dumps(value, ensure_ascii=False, indent=2)), encoding="utf-8")
    print("wrote {} live operation contexts".format(len(value["operations"])))


if __name__ == "__main__":
    main()
