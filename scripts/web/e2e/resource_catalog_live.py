#!/usr/bin/env python3
"""Collect real ROS and direct-product samples for production link types.

This is an opt-in live E2E helper. It performs read-only ROS and Alibaba Cloud
product API calls through the installed CLI and writes only the resource facts
needed by the browser verifier. It never reads or serializes credential material.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iac_code.tools.cloud.aliyun.resource_reference_extractors import (
    direct_api_response_contracts,
    extract_api_resource_references,
)
from iac_code.tools.cloud.resource_references import locator_text, resource_spec
from iac_code.web.resource_link_catalog import resource_link_catalog
from iac_code.web.resource_references import resource_fact_to_web

_SAFE_REGION = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
_SAFE_ENDPOINT = re.compile(r"[a-zA-Z0-9.-]{1,253}\Z")
_SAFE_API_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}\Z")
_SAFE_SSH_HOST = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+\Z")
_SAFE_EXECUTABLE = re.compile(r"[A-Za-z0-9_./-]+\Z")
_HELP_PARAMETER = re.compile(r"^\s+--([A-Za-z][A-Za-z0-9]*)\s+\S+\s+(Optional|Required)\s*$", re.MULTILINE)
_SECTION_HELP_PARAMETER = re.compile(r"^\s+--([A-Za-z][A-Za-z0-9.]*)\s+\S+\s*$")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_REQUEST_ID = re.compile(
    r"(?i)((?:request[ -]?id|x-acs-(?:request|trace)-id)[\s:=\[]+)[a-zA-Z0-9-]+"
)
_REQUEST_URL = re.compile(r"https?://\S+")
_CREDENTIAL_VALUE = re.compile(
    r"(?i)((?:AccessKeyId|AccessKeySecret|SecurityToken|Signature|SignatureNonce|BearerToken)[\s:=]+)[^&\s,}\]]+"
)
_STS_ACCESS_KEY = re.compile(r"\bSTS\.[A-Za-z0-9._-]+")
_RETRYABLE = ("throttl", "timeout", "temporar", "connection reset", "try again")
_UNSUPPORTED_REGION = (
    "invalidregion",
    "regionid.*invalid",
    "not support.*region",
    "endpoint.*not found",
    "unknown endpoint",
    "no such host",
)

# This acceptance suite must never inherit the Alibaba Cloud CLI `current`
# profile.  The domestic-site test account is intentionally isolated in this
# named profile so every live read is attributable to the same identity.
ACCEPTANCE_CLI_PROFILE = "resource-link-test"


class LiveCatalogError(RuntimeError):
    """A bounded live-scan failure without credential material."""


def _progress(phase: str, **values: Any) -> None:
    print(json.dumps({"phase": phase, **values}, ensure_ascii=False), file=sys.stderr, flush=True)


def _append_checkpoint(path: Path, event: dict[str, Any]) -> None:
    """Persist one redacted scan result before starting the next contract."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class Region:
    region_id: str
    endpoint: str


class AliyunCli:
    def __init__(
        self,
        executable: str,
        *,
        profile: str = ACCEPTANCE_CLI_PROFILE,
        timeout: float = 45,
        ssh_host: str = "",
        ssh_executable: str = "ssh",
    ) -> None:
        if profile != ACCEPTANCE_CLI_PROFILE:
            raise LiveCatalogError(
                f"Aliyun CLI profile must be {ACCEPTANCE_CLI_PROFILE!r} for this acceptance suite"
            )
        self.executable = executable
        self.profile = profile
        self.timeout = timeout
        self.ssh_host = ssh_host
        self.ssh_executable = ssh_executable

    def call(self, arguments: list[str]) -> dict[str, Any]:
        return self._run(arguments, parse_json=True)

    def help_parameters(self, product: str, action: str) -> tuple[set[str], set[str]]:
        if not _SAFE_API_NAME.fullmatch(product) or not _SAFE_API_NAME.fullmatch(action):
            raise LiveCatalogError("invalid direct API contract")
        text = self._run(["help", product, action], parse_json=False)
        parameters, required = _parse_help_parameters(text)
        if not parameters and not ("API Version:" in text and "Usage:" in text):
            raise LiveCatalogError("Alibaba Cloud CLI returned no API parameter metadata")
        return parameters, required

    def _run(self, arguments: list[str], *, parse_json: bool) -> Any:
        command = [self.executable, "--profile", self.profile or ACCEPTANCE_CLI_PROFILE]
        command.extend(arguments)
        environment = {**os.environ, "ALIBABA_CLOUD_CLI_NO_COLOR": "true"}
        if self.ssh_host:
            command = [
                self.ssh_executable,
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                self.ssh_host,
                "env ALIBABA_CLOUD_CLI_NO_COLOR=true " + shlex.join(command),
            ]
        last = ""
        for attempt in range(3):
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=self.timeout,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                last = "Alibaba Cloud CLI request timed out"
            else:
                if completed.returncode == 0:
                    if not parse_json:
                        return completed.stdout
                    try:
                        payload = json.loads(completed.stdout)
                    except (json.JSONDecodeError, RecursionError) as error:
                        raise LiveCatalogError("Alibaba Cloud CLI returned invalid JSON") from error
                    if not isinstance(payload, dict):
                        raise LiveCatalogError("Alibaba Cloud CLI returned a non-object response")
                    return payload
                last = _public_error(completed.stderr or completed.stdout)
            if attempt < 2 and any(marker in last.casefold() for marker in _RETRYABLE):
                time.sleep(0.5 * (2**attempt))
                continue
            break
        raise LiveCatalogError(last or "Alibaba Cloud CLI request failed")


def _parse_help_parameters(value: str) -> tuple[set[str], set[str]]:
    """Parse both legacy tabular help and CLI 3.5 sectioned help."""
    legacy = _HELP_PARAMETER.findall(value)
    if legacy:
        return ({name for name, _ in legacy}, {name for name, kind in legacy if kind == "Required"})
    parameters: set[str] = set()
    required: set[str] = set()
    section = ""
    for line in value.splitlines():
        heading = line.strip()
        if heading == "Required Parameters:":
            section = "required"
            continue
        if heading == "Optional Parameters:":
            section = "optional"
            continue
        if heading.endswith("Parameters:"):
            section = ""
            continue
        if not section or not (match := _SECTION_HELP_PARAMETER.fullmatch(line)):
            continue
        name = match.group(1)
        parameters.add(name)
        if section == "required":
            required.add(name)
    return parameters, required


def _public_error(value: str) -> str:
    text = " ".join(_ANSI_ESCAPE.sub("", value).split())
    text = _REQUEST_URL.sub("[request URL redacted]", text)
    text = _CREDENTIAL_VALUE.sub(r"\1[redacted]", text)
    text = _STS_ACCESS_KEY.sub("[redacted]", text)
    text = _REQUEST_ID.sub(r"\1[redacted]", text)
    return text[:500] or "Alibaba Cloud CLI request failed"


def _regions(payload: dict[str, Any]) -> list[Region]:
    result: dict[str, Region] = {}
    for item in payload.get("Regions") or []:
        if not isinstance(item, dict):
            continue
        region_id = item.get("RegionId")
        endpoint = item.get("RegionEndpoint")
        if not isinstance(region_id, str) or not _SAFE_REGION.fullmatch(region_id):
            continue
        if not isinstance(endpoint, str) or not _SAFE_ENDPOINT.fullmatch(endpoint):
            continue
        result[region_id] = Region(region_id, endpoint)
    if not result:
        raise LiveCatalogError("ROS DescribeRegions returned no usable regions")
    return [result[key] for key in sorted(result)]


def _list_stacks(client: AliyunCli, region: Region, signing_region: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = client.call(
            [
                "ros",
                "list-stacks",
                "--endpoint",
                region.endpoint,
                "--region",
                signing_region,
                "--biz-region-id",
                region.region_id,
                "--page-size",
                "50",
                "--page-number",
                str(page),
                "--show-nested-stack",
                "true",
            ]
        )
        rows = payload.get("Stacks") or []
        if not isinstance(rows, list):
            raise LiveCatalogError("ROS ListStacks returned an invalid stack list")
        result.extend(row for row in rows if isinstance(row, dict))
        total = payload.get("TotalCount")
        if not isinstance(total, int) or len(result) >= total or not rows:
            return result
        page += 1


def _list_resources(
    client: AliyunCli, region: Region, signing_region: str, stack: dict[str, Any]
) -> list[dict[str, Any]]:
    stack_id = locator_text(stack.get("StackId"))
    if stack_id is None:
        raise LiveCatalogError("ROS ListStacks returned a stack without StackId")
    payload = client.call(
        [
            "ros",
            "list-stack-resources",
            "--endpoint",
            region.endpoint,
            "--region",
            signing_region,
            "--biz-region-id",
            region.region_id,
            "--stack-id",
            stack_id,
        ]
    )
    rows = payload.get("Resources") or []
    if not isinstance(rows, list):
        raise LiveCatalogError("ROS ListStackResources returned an invalid resource list")
    return [
        {**row, "StackId": row.get("StackId") or stack_id, "RegionId": region.region_id}
        for row in rows
        if isinstance(row, dict)
    ]


def _requested_attributes(resource_type: str) -> list[str]:
    catalog = resource_link_catalog()
    spec = resource_spec(resource_type)
    if spec is None:
        return []
    fields = {*catalog.attributes.get(resource_type, ()), *spec.parent_fields, *spec.attribute_fields}
    if spec.identifier_field != "PhysicalResourceId":
        fields.add(spec.identifier_field)
    return sorted(fields)


def _get_resource(
    client: AliyunCli, region: Region, signing_region: str, row: dict[str, Any]
) -> dict[str, Any]:
    stack_id = locator_text(row.get("StackId"))
    logical_id = locator_text(row.get("LogicalResourceId"))
    resource_type = row.get("ResourceType")
    if stack_id is None or logical_id is None or not isinstance(resource_type, str):
        raise LiveCatalogError("ROS stack resource is missing its stable lookup fields")
    arguments = [
        "ros",
        "get-stack-resource",
        "--endpoint",
        region.endpoint,
        "--region",
        signing_region,
        "--biz-region-id",
        region.region_id,
        "--stack-id",
        stack_id,
        "--logical-resource-id",
        logical_id,
    ]
    attributes = _requested_attributes(resource_type)
    if attributes:
        arguments.extend(("--resource-attributes", *attributes))
    else:
        arguments.extend(("--show-resource-attributes", "true"))
    return client.call(arguments)


def _preferred_row(left: dict[str, Any] | None, right: dict[str, Any]) -> dict[str, Any]:
    if left is None:
        return right

    def score(row: dict[str, Any]) -> tuple[int, int, str]:
        status = str(row.get("Status") or "")
        identifier = locator_text(row.get("PhysicalResourceId"))
        return (
            int(status.endswith("_COMPLETE") and not status.startswith("DELETE_")),
            int(identifier is not None),
            status,
        )

    return max((left, right), key=score)


def _reference_for(
    detail: dict[str, Any], row: dict[str, Any], *, partition: str, ordinal: int
) -> dict[str, Any] | None:
    region_id = str(row["RegionId"])
    stack_id = str(row["StackId"])
    logical_id = str(row["LogicalResourceId"])
    tool_id = f"live-catalog-{ordinal}"
    # GetStackResource omits some stable fields for deleted resources and on
    # older ROS endpoints. ListStackResources is the authoritative source for
    # those lookup fields, so preserve them while keeping response attributes.
    observed = {
        **detail,
        "ResourceType": detail.get("ResourceType") or row.get("ResourceType"),
        "StackId": detail.get("StackId") or stack_id,
        "LogicalResourceId": detail.get("LogicalResourceId") or logical_id,
    }
    facts = extract_api_resource_references(
        product="ros",
        action="GetStackResource",
        params={"StackId": stack_id, "LogicalResourceId": logical_id, "RegionId": region_id},
        body=observed,
        region_id=region_id,
        tool_use_id=tool_id,
        account_scope="live-catalog-account",
        console_partition=partition,
    )
    resource_type = row.get("ResourceType")
    fact = next(
        (
            item
            for item in facts
            if item.get("resource_type") == resource_type
            and item.get("evidence") == "api_response"
            and item.get("logical_resource_id") == logical_id
        ),
        None,
    )
    if fact is None:
        return None
    for source in fact["sources"]:
        source.update(message_id=tool_id, turn_id="live-catalog-turn")
    digest = hashlib.sha256(
        json.dumps([resource_type, region_id, stack_id, logical_id], separators=(",", ":")).encode()
    ).hexdigest()
    fact["reference_id"] = "ref-" + digest
    return resource_fact_to_web(fact)


def _resource_shape(detail: dict[str, Any]) -> str:
    raw_attributes = detail.get("ResourceAttributes")
    states = sorted(
        "{}:{}".format(
            item["ResourceAttributeKey"],
            "present" if locator_text(item.get("ResourceAttributeValue")) else "missing",
        )
        for item in (raw_attributes if isinstance(raw_attributes, list) else [])
        if isinstance(item, dict) and isinstance(item.get("ResourceAttributeKey"), str)
    )
    physical = detail.get("PhysicalResourceId")
    return "physical={}, attributes={}, status={}".format(
        "present" if locator_text(physical) else "missing",
        ",".join(states) if states else "none",
        str(detail.get("Status") or "missing")[:64],
    )


def _has_reviewed_locator(detail: dict[str, Any], resource_type: str) -> bool:
    if locator_text(detail.get("PhysicalResourceId")):
        return True
    spec = resource_spec(resource_type)
    if spec is None:
        return False
    attributes = detail.get("ResourceAttributes")
    values = {
        item.get("ResourceAttributeKey"): item.get("ResourceAttributeValue")
        for item in (attributes if isinstance(attributes, list) else [])
        if isinstance(item, dict) and isinstance(item.get("ResourceAttributeKey"), str)
    }
    return any(
        locator_text(values.get(field))
        for field in (spec.identifier_field, *spec.parent_fields, *spec.attribute_fields)
    )


def _path_values(value: Any, path: tuple[str, ...]) -> list[Any]:
    values = [value]
    for segment in path:
        children: list[Any] = []
        if segment == "*":
            for item in values:
                if isinstance(item, list):
                    children.extend(item)
        else:
            for item in values:
                if isinstance(item, dict) and segment in item:
                    children.append(item[segment])
        values = children
    return values


def _direct_page_arguments(
    contract: dict[str, Any],
    *,
    region_id: str,
    signing_region: str,
    parameters: set[str],
    required: set[str],
    context: dict[str, str],
    page: int,
    token: str,
) -> tuple[list[str], list[str]]:
    product = str(contract["product"])
    action = str(contract["action"])
    arguments = ["--region", region_id or signing_region, product, action]
    available = {key.casefold(): key for key in parameters}
    supplied: set[str] = set()

    def add(name: str, value: str) -> None:
        actual = available.get(name.casefold())
        if actual and value:
            arguments.extend(("--" + actual, value))
            supplied.add(actual)

    if contract["regional"]:
        add("RegionId", region_id)
    for name in sorted(required):
        value = context.get(name.casefold(), "")
        if value:
            add(name, value)
    # The first page is sufficient: a non-empty reviewed list either yields a
    # fact or a response-contract mismatch immediately. Avoid optional paging
    # values because products enforce incompatible ranges for PageSize.
    if "MaxResults" in required and "NextToken" in parameters:
        add("MaxResults", "10")
    elif "MaxItems" in required and "Marker" in parameters:
        add("MaxItems", "100")
    elif "PageSize" in required:
        add("PageSize", "30")
        for name in ("PageNumber", "PageNo", "PageNum", "CurrentPage"):
            if name in required:
                add(name, str(page))
                break
    elif "Size" in required:
        add("Size", "10")
    if token:
        for name in ("NextToken", "Marker", "NextMarker"):
            add(name, token)
    missing = sorted(name for name in required if name not in supplied)
    return arguments, missing


def _next_direct_page(
    body: dict[str, Any], *, parameters: set[str], page: int, row_count: int
) -> tuple[int, str] | None:
    for response_name, request_name in (("NextToken", "NextToken"), ("NextMarker", "Marker"), ("Marker", "Marker")):
        token = locator_text(body.get(response_name))
        if token and request_name in parameters:
            return page, token
    if body.get("IsTruncated") is True and "Marker" in parameters:
        return page, locator_text(body.get("Marker")) or ""
    total = body.get("TotalCount") or body.get("Total")
    page_size = body.get("PageSize") or body.get("MaxResults") or body.get("Size")
    page_number = body.get("PageNumber") or body.get("PageNo") or body.get("PageNum") or page
    if (
        isinstance(total, int)
        and isinstance(page_size, int)
        and isinstance(page_number, int)
        and row_count
        and page_number * page_size < total
        and any(name in parameters for name in ("PageNumber", "PageNo", "PageNum", "CurrentPage"))
    ):
        return page + 1, ""
    return None


def _direct_context(samples: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for sample in samples:
        reference = sample.get("reference")
        if not isinstance(reference, dict):
            continue
        values: dict[str, str] = {}
        attributes = reference.get("attributes")
        if isinstance(attributes, dict):
            values.update(
                (str(key).casefold(), value)
                for key, value in attributes.items()
                if isinstance(value, str) and value
            )
        field = reference.get("identifierField")
        identifier = reference.get("identifier")
        if isinstance(field, str) and isinstance(identifier, str):
            values[field.casefold()] = identifier
        existing = result.setdefault(str(sample["resourceType"]), {})
        for key, value in values.items():
            existing.setdefault(key, value)
    return result


def _direct_reference(
    body: dict[str, Any],
    contract: dict[str, Any],
    *,
    region_id: str,
    partition: str,
    ordinal: int,
) -> dict[str, Any] | None:
    tool_id = f"live-direct-{ordinal}"
    facts = extract_api_resource_references(
        product=str(contract["product"]),
        action=str(contract["action"]),
        params={},
        body=body,
        region_id=region_id,
        tool_use_id=tool_id,
        account_scope="live-catalog-account",
        console_partition=partition,
    )
    fact = next((item for item in facts if item.get("resource_type") == contract["resource_type"]), None)
    if fact is None:
        return None
    for source in fact["sources"]:
        source.update(message_id=tool_id, turn_id="live-catalog-turn")
    digest = hashlib.sha256(
        json.dumps(
            [contract["product"], contract["action"], contract["resource_type"], fact["identifier"]],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    fact["reference_id"] = "ref-" + digest
    return resource_fact_to_web(fact)


def _scan_direct_contract(
    client: AliyunCli,
    contract: dict[str, Any],
    *,
    regions: list[Region],
    signing_region: str,
    partition: str,
    context: dict[str, str],
    ordinal: int,
    max_duration: float = 180.0,
) -> dict[str, Any]:
    product, action = str(contract["product"]), str(contract["action"])
    try:
        parameters, required = client.help_parameters(product, action)
    except LiveCatalogError as error:
        detail = str(error)
        status = "scan_failed" if "timed out" in detail.casefold() else "contract_unavailable"
        return {"status": status, "error": detail, "scannedRegions": 0}
    ordered_regions = sorted(
        regions,
        key=lambda item: (item.region_id != signing_region, item.region_id != "cn-hangzhou", item.region_id),
    )
    targets = ordered_regions if contract["regional"] else [Region("", "")]
    successful_regions = 0
    unsupported_regions = 0
    errors: Counter[str] = Counter()
    deadline = time.monotonic() + max_duration
    for region in targets:
        if time.monotonic() >= deadline:
            return {
                "status": "scan_timeout",
                "error": "direct contract wall-clock budget exceeded",
                "scannedRegions": successful_regions,
                "unsupportedRegions": unsupported_regions,
            }
        page, token = 1, ""
        while page <= 1000:
            if time.monotonic() >= deadline:
                return {
                    "status": "scan_timeout",
                    "error": "direct contract wall-clock budget exceeded",
                    "scannedRegions": successful_regions,
                    "unsupportedRegions": unsupported_regions,
                }
            arguments, missing = _direct_page_arguments(
                contract,
                region_id=region.region_id,
                signing_region=signing_region,
                parameters=parameters,
                required=required,
                context=context,
                page=page,
                token=token,
            )
            if missing:
                return {
                    "status": "missing_required_context",
                    "missing": missing,
                    "scannedRegions": successful_regions,
                }
            try:
                body = client.call(arguments)
            except LiveCatalogError as error:
                detail = str(error)
                folded = detail.casefold()
                if any(re.search(marker, folded) for marker in _UNSUPPORTED_REGION):
                    unsupported_regions += 1
                    break
                errors[detail] += 1
                if any(
                    marker in folded
                    for marker in (
                        "forbidden",
                        "unauthorized",
                        "notauthorized",
                        "not open",
                        "invalidaction",
                        "missingparameter",
                        "invalidparameter",
                    )
                ):
                    return {
                        "status": "inaccessible",
                        "error": detail,
                        "scannedRegions": successful_regions,
                    }
                break
            successful_regions += int(page == 1)
            reference = _direct_reference(
                body,
                contract,
                region_id=region.region_id or signing_region,
                partition=partition,
                ordinal=ordinal,
            )
            if reference is not None:
                return {
                    "status": "found",
                    "regionId": region.region_id,
                    "reference": reference,
                    "scannedRegions": successful_regions,
                    "unsupportedRegions": unsupported_regions,
                }
            rows = _path_values(body, tuple(contract["path"]))
            if rows:
                return {
                    "status": "response_mismatch",
                    "scannedRegions": successful_regions,
                    "regionId": region.region_id,
                }
            next_page = _next_direct_page(body, parameters=parameters, page=page, row_count=len(rows))
            if next_page is None:
                break
            page, token = next_page
    if errors:
        return {
            "status": "scan_failed",
            "error": errors.most_common(1)[0][0],
            "failedRegions": sum(errors.values()),
            "scannedRegions": successful_regions,
            "unsupportedRegions": unsupported_regions,
        }
    if not successful_regions and unsupported_regions:
        return {
            "status": "unsupported_all_regions",
            "scannedRegions": 0,
            "unsupportedRegions": unsupported_regions,
        }
    return {
        "status": "no_sample",
        "scannedRegions": successful_regions,
        "unsupportedRegions": unsupported_regions,
    }


def _deleted_status(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("DELETE_")


def _not_found_error(value: str) -> bool:
    folded = value.casefold()
    return any(
        marker in folded
        for marker in (
            "notfound",
            "not found",
            "notexist",
            "not exist",
            "does not exist",
            "不存在",
            "未找到",
        )
    )


def _recheck_ros_sample(
    client: AliyunCli,
    sample: dict[str, Any],
    *,
    endpoints: dict[str, Region],
    signing_region: str,
) -> dict[str, Any]:
    reference = sample.get("reference")
    sources = reference.get("sources") if isinstance(reference, dict) else None
    source = sources[0] if isinstance(sources, list) and sources and isinstance(sources[0], dict) else {}
    region_id = locator_text(sample.get("regionId"))
    stack_id = locator_text(source.get("stackId"))
    logical_id = locator_text(source.get("logicalResourceId") or sample.get("logicalResourceId"))
    resource_type = sample.get("resourceType")
    identifier = locator_text(sample.get("identifier"))
    region = endpoints.get(region_id or "")
    if (
        region is None
        or stack_id is None
        or logical_id is None
        or identifier is None
        or not isinstance(resource_type, str)
    ):
        return {"status": "inconclusive", "reason": "invalid_ros_recheck_locator"}
    row = {
        "StackId": stack_id,
        "LogicalResourceId": logical_id,
        "ResourceType": resource_type,
        "RegionId": region_id,
    }
    try:
        detail = _get_resource(client, region, signing_region, row)
    except LiveCatalogError as error:
        detail = str(error)
        if _not_found_error(detail):
            return {"status": "resource_absent", "reason": "ros_resource_not_found"}
        return {"status": "inconclusive", "reason": "ros_recheck_failed", "detail": detail}
    current = _reference_for(detail, row, partition="aliyun", ordinal=0)
    if _deleted_status(detail.get("Status")):
        return {"status": "resource_absent", "reason": "ros_resource_deleted"}
    if current is None:
        return {"status": "inconclusive", "reason": "ros_reference_unavailable"}
    if current.get("identifier") == identifier:
        return {"status": "route_error", "reason": "resource_still_exists"}
    return {"status": "resource_absent", "reason": "ros_resource_replaced"}


def _direct_facts(
    body: dict[str, Any],
    contract: dict[str, Any],
    *,
    region_id: str,
) -> list[dict[str, Any]]:
    return [
        fact
        for fact in extract_api_resource_references(
            product=str(contract["product"]),
            action=str(contract["action"]),
            params={},
            body=body,
            region_id=region_id,
            tool_use_id="live-direct-recheck",
            account_scope="live-catalog-account",
            console_partition="aliyun",
        )
        if fact.get("resource_type") == contract["resource_type"]
    ]


def _recheck_direct_sample(
    client: AliyunCli,
    sample: dict[str, Any],
    *,
    signing_region: str,
    context: dict[str, str],
) -> dict[str, Any]:
    product = str(sample.get("product") or "")
    action = str(sample.get("action") or "")
    resource_type = str(sample.get("resourceType") or "")
    identifier = locator_text(sample.get("identifier"))
    contract = next(
        (
            item
            for item in direct_api_response_contracts()
            if item["product"] == product
            and item["action"] == action
            and item["resource_type"] == resource_type
        ),
        None,
    )
    if contract is None or identifier is None:
        return {"status": "inconclusive", "reason": "direct_contract_unavailable"}
    try:
        parameters, required = client.help_parameters(product, action)
    except LiveCatalogError as error:
        return {"status": "inconclusive", "reason": "direct_help_failed", "detail": str(error)}
    region_id = str(sample.get("regionId") or "") if contract["regional"] else ""
    if contract["regional"] and not _SAFE_REGION.fullmatch(region_id):
        return {"status": "inconclusive", "reason": "invalid_direct_region"}
    page, token = 1, ""
    while page <= 1000:
        arguments, missing = _direct_page_arguments(
            contract,
            region_id=region_id,
            signing_region=signing_region,
            parameters=parameters,
            required=required,
            context=context,
            page=page,
            token=token,
        )
        if missing:
            return {"status": "inconclusive", "reason": "missing_required_context", "missing": missing}
        # Optional page-number parameters are omitted during discovery because
        # any row is enough. A targeted existence recheck must traverse every
        # page before it may conclude that the exact old identifier is absent.
        if page > 1:
            available = {key.casefold(): key for key in parameters}
            for candidate in ("PageNumber", "PageNo", "PageNum", "CurrentPage"):
                if actual := available.get(candidate.casefold()):
                    option = "--" + actual
                    if option not in arguments:
                        arguments.extend((option, str(page)))
                    break
        try:
            body = client.call(arguments)
        except LiveCatalogError as error:
            detail = str(error)
            # A failed list call cannot prove that the sampled resource is
            # absent.  The error may describe an endpoint, action, parent
            # resource, or required context rather than the target identifier.
            # Only a successful, completely traversed response may establish
            # absence for a direct inventory contract.
            return {"status": "inconclusive", "reason": "direct_recheck_failed", "detail": detail}
        if any(fact.get("identifier") == identifier for fact in _direct_facts(body, contract, region_id=region_id)):
            return {"status": "route_error", "reason": "resource_still_exists"}
        rows = _path_values(body, tuple(contract["path"]))
        next_page = _next_direct_page(body, parameters=parameters, page=page, row_count=len(rows))
        if next_page is None:
            return {"status": "resource_absent", "reason": "direct_resource_not_listed"}
        page, token = next_page
    return {"status": "inconclusive", "reason": "direct_recheck_page_limit"}


def recheck_destination_failures(
    client: AliyunCli,
    catalog_report: dict[str, Any],
    browser_report: dict[str, Any],
    *,
    signing_region: str,
) -> dict[str, Any]:
    samples = catalog_report.get("samples")
    destination_failures = (browser_report.get("destinations") or {}).get("failures")
    if not isinstance(samples, list) or not isinstance(destination_failures, list):
        raise LiveCatalogError("invalid live E2E reports for destination recheck")
    requested = {
        (item.get("source"), item.get("resourceType"), item.get("identifier")): item
        for item in destination_failures
        if isinstance(item, dict) and item.get("reason") == "console_resource_not_found"
    }
    selected = [
        sample
        for sample in samples
        if isinstance(sample, dict)
        and (sample.get("source"), sample.get("resourceType"), sample.get("identifier")) in requested
    ]
    if len(selected) != len(requested):
        raise LiveCatalogError("destination failures do not match the source catalog samples")
    ros_samples = [sample for sample in selected if sample.get("source") == "ros"]
    endpoints: dict[str, Region] = {}
    if ros_samples:
        endpoints = {
            region.region_id: region
            for region in _regions(client.call(["ros", "describe-regions", "--region", signing_region]))
        }
    contexts = _direct_context(samples)
    results = []
    for sample in selected:
        key = (sample.get("source"), sample.get("resourceType"), sample.get("identifier"))
        failure = requested[key]
        if sample.get("source") == "ros":
            outcome = _recheck_ros_sample(client, sample, endpoints=endpoints, signing_region=signing_region)
        elif sample.get("source") == "direct":
            outcome = _recheck_direct_sample(
                client,
                sample,
                signing_region=signing_region,
                context=contexts.get(str(sample.get("resourceType") or ""), {}),
            )
        else:
            outcome = {"status": "inconclusive", "reason": "unsupported_sample_source"}
        results.append(
            {
                "source": sample.get("source"),
                "resourceType": sample.get("resourceType"),
                "identifier": sample.get("identifier"),
                "href": failure.get("href"),
                "ruleId": failure.get("ruleId"),
                "consoleObservation": "console_resource_not_found",
                **outcome,
            }
        )
    statuses = Counter(str(item["status"]) for item in results)
    return {
        "schemaVersion": 1,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "routeErrorCount": statuses["route_error"],
            "resourceAbsentCount": statuses["resource_absent"],
            "inconclusiveCount": statuses["inconclusive"],
        },
        "results": results,
    }


def collect_catalog(
    client: AliyunCli,
    *,
    partition: str,
    signing_region: str,
    concurrency: int,
    include_direct: bool = True,
    direct_contract_timeout: float = 180.0,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    catalog = resource_link_catalog()
    run_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()
    if checkpoint_path is not None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text("", encoding="utf-8")
        _append_checkpoint(checkpoint_path, {"phase": "scan_start", "runId": run_id, "startedAt": started})
    regions = _regions(client.call(["ros", "describe-regions", "--region", signing_region]))
    region_stacks: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []

    def scan_region(region: Region) -> tuple[Region, list[dict[str, Any]] | None, str]:
        try:
            return region, _list_stacks(client, region, signing_region), ""
        except LiveCatalogError as error:
            return region, None, str(error)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        region_errors: dict[str, str] = {}
        for region, stacks, error in pool.map(scan_region, regions):
            if stacks is None:
                region_errors[region.region_id] = error
            else:
                region_stacks[region.region_id] = stacks
    # A parallel endpoint timeout must not turn a healthy region into a false
    # coverage gap. Retry only the failed regions sequentially before reporting.
    for region in (item for item in regions if item.region_id in region_errors):
        _, stacks, error = scan_region(region)
        if stacks is None:
            failures.append({"phase": "ListStacks", "regionId": region.region_id, "error": error})
        else:
            region_stacks[region.region_id] = stacks
    _progress(
        "ros_regions",
        completed=len(region_stacks),
        total=len(regions),
        stacks=sum(len(rows) for rows in region_stacks.values()),
    )

    resources: list[dict[str, Any]] = []
    stack_tasks = [
        (region, stack)
        for region in regions
        for stack in region_stacks.get(region.region_id, [])
    ]

    def scan_stack(task: tuple[Region, dict[str, Any]]) -> tuple[Region, list[dict[str, Any]] | None, str]:
        region, stack = task
        try:
            return region, _list_resources(client, region, signing_region, stack), ""
        except LiveCatalogError as error:
            return region, None, str(error)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        stack_errors: list[tuple[Region, dict[str, Any], str]] = []
        for (region, stack), (_, rows, error) in zip(stack_tasks, pool.map(scan_stack, stack_tasks), strict=True):
            if rows is None:
                stack_errors.append((region, stack, error))
            else:
                resources.extend(rows)
    for region, stack, _first_error in stack_errors:
        _, rows, error = scan_stack((region, stack))
        if rows is None:
            failures.append(
                {
                    "phase": "ListStackResources",
                    "regionId": region.region_id,
                    "stackIdHash": hashlib.sha256(str(stack.get("StackId") or "").encode()).hexdigest()[:16],
                    "error": error,
                }
            )
        else:
            resources.extend(rows)
    _progress("ros_resources", stacks=len(stack_tasks), resources=len(resources))

    catalog_types = catalog.resource_types
    counts: Counter[str] = Counter()
    candidates: dict[str, dict[str, Any]] = {}
    outside_catalog: Counter[str] = Counter()
    for row in resources:
        resource_type = row.get("ResourceType")
        if not isinstance(resource_type, str):
            continue
        if resource_type not in catalog_types:
            outside_catalog[resource_type] += 1
            continue
        counts[resource_type] += 1
        candidates[resource_type] = _preferred_row(candidates.get(resource_type), row)

    endpoints = {region.region_id: region for region in regions}
    samples: list[dict[str, Any]] = []
    detail_failures: list[dict[str, str]] = []
    unavailable_samples: list[dict[str, str]] = []

    def load_detail(item: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any], dict[str, Any] | None, str]:
        resource_type, row = item
        region = endpoints[str(row["RegionId"])]
        try:
            return resource_type, row, _get_resource(client, region, signing_region, row), ""
        except LiveCatalogError as error:
            return resource_type, row, None, str(error)

    sample_rows = sorted(candidates.items())
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for ordinal, (resource_type, row, detail, error) in enumerate(pool.map(load_detail, sample_rows), start=1):
            if detail is None:
                _, _, detail, error = load_detail((resource_type, row))
                if detail is None:
                    detail_failures.append(
                        {
                            "phase": "GetStackResource",
                            "resourceType": resource_type,
                            "regionId": str(row["RegionId"]),
                            "error": error,
                        }
                    )
                    continue
            reference = _reference_for(detail, row, partition=partition, ordinal=ordinal)
            if reference is None:
                target = detail_failures if _has_reviewed_locator(detail, resource_type) else unavailable_samples
                target.append(
                    {
                        **({"phase": "ReferenceExtraction"} if target is detail_failures else {}),
                        "resourceType": resource_type,
                        "regionId": str(row["RegionId"]),
                        "error": "GetStackResource did not produce a usable resource fact ({})".format(
                            _resource_shape(detail)
                        ),
                    }
                )
                continue
            samples.append(
                {
                    "source": "ros",
                    "resourceType": resource_type,
                    "regionId": str(row["RegionId"]),
                    "logicalResourceId": str(row["LogicalResourceId"]),
                    "identifier": str(reference["identifier"]),
                    "toolUseId": reference["sources"][0]["toolUseId"],
                    "reference": reference,
                }
            )
    _progress("ros_samples", discoveredTypes=len(candidates), usableSamples=len(samples))

    failures.extend(detail_failures)
    ros_failure_count = len(failures)
    found_types = set(counts)
    sampled_types = {item["resourceType"] for item in samples if item["source"] == "ros"}
    direct_contracts = list(direct_api_response_contracts()) if include_direct else []
    direct_context = _direct_context(samples)
    direct_results: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def scan_direct(task: tuple[int, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        ordinal, contract = task
        return contract, _scan_direct_contract(
            client,
            contract,
            regions=regions,
            signing_region=signing_region,
            partition=partition,
            context=direct_context.get(str(contract["resource_type"]), {}),
            ordinal=ordinal,
            max_duration=direct_contract_timeout,
        )

    direct_tasks = list(enumerate(direct_contracts, start=1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        pending = {pool.submit(scan_direct, task): task[1] for task in direct_tasks}
        for completed, future in enumerate(concurrent.futures.as_completed(pending), start=1):
            contract, result = future.result()
            direct_results.append((contract, result))
            _progress(
                "direct_api",
                completed=completed,
                total=len(direct_tasks),
                product=contract["product"],
                action=contract["action"],
                resourceType=contract["resource_type"],
                status=result["status"],
            )
            if checkpoint_path is not None:
                _append_checkpoint(
                    checkpoint_path,
                    {
                        "phase": "direct_api",
                        "runId": run_id,
                        "contractId": "{}/{}/{}".format(
                            contract["product"], contract["action"], ".".join(contract["path"])
                        ),
                        "resourceType": contract["resource_type"],
                        "product": contract["product"],
                        "action": contract["action"],
                        "status": result["status"],
                        "scannedRegions": result.get("scannedRegions", 0),
                    },
                )
    direct_results.sort(key=lambda item: (item[0]["product"], item[0]["action"], item[0]["resource_type"]))

    direct_statuses: Counter[str] = Counter()
    direct_no_samples: list[str] = []
    direct_sampled_types: set[str] = set()
    for contract, result in direct_results:
        status = str(result["status"])
        direct_statuses[status] += 1
        resource_type = str(contract["resource_type"])
        reference = result.get("reference")
        if status == "found" and isinstance(reference, dict):
            direct_sampled_types.add(resource_type)
            samples.append(
                {
                    "source": "direct",
                    "product": contract["product"],
                    "action": contract["action"],
                    "resourceType": resource_type,
                    "regionId": str(result.get("regionId") or ""),
                    "identifier": str(reference["identifier"]),
                    "toolUseId": reference["sources"][0]["toolUseId"],
                    "reference": reference,
                }
            )
        elif status == "no_sample":
            direct_no_samples.append(resource_type)
        else:
            failure = {
                "phase": "DirectDescribe",
                "product": str(contract["product"]),
                "action": str(contract["action"]),
                "resourceType": resource_type,
                "error": status,
            }
            if result.get("error"):
                failure["detail"] = str(result["error"])
            if result.get("missing"):
                failure["missing"] = ",".join(result["missing"])
            failures.append(failure)

    return {
        "schemaVersion": 2,
        "runId": run_id,
        "startedAt": started,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "partition": partition,
        "catalog": {
            "checkedAt": catalog.checked_at,
            "resourceCount": len(catalog_types),
            "sha256": catalog.hashes[partition],
        },
        "scan": {
            "regionCount": len(regions),
            "scannedRegionCount": len(region_stacks),
            "stackCount": sum(len(rows) for rows in region_stacks.values()),
            "stackResourceCount": len(resources),
            "complete": ros_failure_count == 0,
        },
        "directScan": {
            "contractCount": len(direct_contracts),
            "sampledContractCount": direct_statuses["found"],
            "noSampleContractCount": direct_statuses["no_sample"],
            "complete": sum(direct_statuses[value] for value in ("found", "no_sample")) == len(direct_contracts),
            "statuses": dict(sorted(direct_statuses.items())),
        },
        "coverage": {
            "discoveredTypeCount": len(found_types),
            "sampledTypeCount": len(sampled_types),
            "noSampleTypeCount": len(catalog_types - found_types),
            "discoveredWithoutUsableSample": sorted(found_types - sampled_types),
            "noSampleTypes": sorted(catalog_types - found_types),
            "countsByType": dict(sorted(counts.items())),
            "outsideCatalog": dict(sorted(outside_catalog.items())),
            "directSampledTypeCount": len(direct_sampled_types),
            "directSampledTypes": sorted(direct_sampled_types),
            "directNoSampleTypes": sorted(set(direct_no_samples)),
            "rosUnavailableSamples": unavailable_samples,
        },
        "samples": samples,
        "failures": failures,
    }


def collect_direct_contract(
    client: AliyunCli,
    *,
    contract_id: str,
    region_id: str,
    partition: str,
    direct_contract_timeout: float,
    checkpoint_path: Path,
) -> dict[str, Any]:
    """Capture one current direct source without rewriting the full scan.

    The report and checkpoint share a fresh run ID.  This is source evidence
    only; P3 registration and browser acceptance remain separate operations.
    """
    contracts = [
        item
        for item in direct_api_response_contracts()
        if "{}/{}/{}/{}".format(
            item["product"], item["action"], item["resource_type"], ".".join(item["path"])
        ) == contract_id
    ]
    if len(contracts) != 1:
        raise LiveCatalogError("targeted direct contract is absent or ambiguous")
    contract = contracts[0]
    if not _SAFE_REGION.fullmatch(region_id):
        raise LiveCatalogError("invalid targeted direct region")
    if checkpoint_path.exists():
        raise LiveCatalogError("targeted direct checkpoint already exists")
    run_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()
    _append_checkpoint(checkpoint_path, {"phase": "scan_start", "runId": run_id, "startedAt": started})
    result = _scan_direct_contract(
        client,
        contract,
        regions=[Region(region_id, "")],
        signing_region=region_id,
        partition=partition,
        context={},
        ordinal=1,
        max_duration=direct_contract_timeout,
    )
    _append_checkpoint(
        checkpoint_path,
        {
            "phase": "direct_api",
            "runId": run_id,
            "contractId": "{}/{}/{}".format(contract["product"], contract["action"], ".".join(contract["path"])),
            "resourceType": contract["resource_type"],
            "product": contract["product"],
            "action": contract["action"],
            "status": result["status"],
            "scannedRegions": result.get("scannedRegions", 0),
        },
    )
    samples: list[dict[str, Any]] = []
    reference = result.get("reference")
    if result["status"] == "found" and isinstance(reference, dict):
        samples.append(
            {
                "source": "direct",
                "product": contract["product"],
                "action": contract["action"],
                "resourceType": contract["resource_type"],
                "regionId": str(result.get("regionId") or region_id),
                "identifier": str(reference["identifier"]),
                "toolUseId": reference["sources"][0]["toolUseId"],
                "reference": reference,
            }
        )
    return {
        "schemaVersion": 2,
        "scanMode": "targeted_direct",
        "runId": run_id,
        "startedAt": started,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "partition": partition,
        "target": {"contractId": contract_id, "regionId": region_id},
        "directScan": {
            "contractCount": 1,
            "sampledContractCount": len(samples),
            "statuses": {str(result["status"]): 1},
        },
        "samples": samples,
        "failures": [] if samples else [{"phase": "DirectDescribe", "status": result["status"]}],
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recheck-catalog", type=Path)
    parser.add_argument("--recheck-browser", type=Path)
    parser.add_argument("--aliyun-cli", default=shutil.which("aliyun") or "")
    parser.add_argument("--ssh-host", default="")
    parser.add_argument("--remote-aliyun-cli", default="aliyun")
    parser.add_argument(
        "--profile",
        choices=(ACCEPTANCE_CLI_PROFILE,),
        default=ACCEPTANCE_CLI_PROFILE,
        help="Alibaba Cloud CLI profile; fixed for this domestic-site acceptance suite",
    )
    parser.add_argument("--partition", choices=("aliyun", "alibabacloud"), default="aliyun")
    parser.add_argument("--signing-region", default="cn-hangzhou")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--direct-contract-timeout", type=float, default=180)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--skip-direct", action="store_true")
    parser.add_argument("--direct-contract-id", default="", help="capture only this exact direct-query contract")
    parser.add_argument("--direct-region", default="", help="one region for --direct-contract-id")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if bool(args.recheck_catalog) != bool(args.recheck_browser):
        raise LiveCatalogError("--recheck-catalog and --recheck-browser must be used together")
    if bool(args.direct_contract_id) != bool(args.direct_region):
        raise LiveCatalogError("--direct-contract-id and --direct-region must be used together")
    if args.direct_contract_id and (args.recheck_catalog or args.skip_direct):
        raise LiveCatalogError("targeted direct scan cannot be combined with recheck or --skip-direct")
    if args.ssh_host:
        if not _SAFE_SSH_HOST.fullmatch(args.ssh_host):
            raise LiveCatalogError("invalid SSH host")
        if not _SAFE_EXECUTABLE.fullmatch(args.remote_aliyun_cli):
            raise LiveCatalogError("invalid remote Alibaba Cloud CLI path")
        executable = args.remote_aliyun_cli
        ssh_executable = shutil.which("ssh") or ""
        if not ssh_executable:
            raise LiveCatalogError("SSH client is required")
    else:
        if not args.aliyun_cli or not Path(args.aliyun_cli).is_file():
            raise LiveCatalogError("Alibaba Cloud CLI is required")
        executable = args.aliyun_cli
        ssh_executable = "ssh"
    if not _SAFE_REGION.fullmatch(args.signing_region):
        raise LiveCatalogError("invalid signing region")
    if not 1 <= args.concurrency <= 16:
        raise LiveCatalogError("concurrency must be between 1 and 16")
    if not 5 <= args.request_timeout <= 120:
        raise LiveCatalogError("request timeout must be between 5 and 120 seconds")
    if not 15 <= args.direct_contract_timeout <= 900:
        raise LiveCatalogError("direct contract timeout must be between 15 and 900 seconds")
    client = AliyunCli(
        executable,
        profile=args.profile,
        timeout=args.request_timeout,
        ssh_host=args.ssh_host,
        ssh_executable=ssh_executable,
    )
    if args.direct_contract_id:
        checkpoint_path = args.checkpoint or args.output.with_name(args.output.name + ".events.jsonl")
        if args.output.exists() or checkpoint_path.exists():
            raise LiveCatalogError("targeted direct output or checkpoint already exists")
        report = collect_direct_contract(
            client,
            contract_id=args.direct_contract_id,
            region_id=args.direct_region,
            partition=args.partition,
            direct_contract_timeout=args.direct_contract_timeout,
            checkpoint_path=checkpoint_path,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {"status": next(iter(report["directScan"]["statuses"])), "output": str(args.output)},
                ensure_ascii=False,
            )
        )
        return 0
    if args.recheck_catalog and args.recheck_browser:
        try:
            catalog_report = json.loads(args.recheck_catalog.read_text(encoding="utf-8"))
            browser_report = json.loads(args.recheck_browser.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LiveCatalogError("unable to read live E2E reports for destination recheck") from error
        if not isinstance(catalog_report, dict) or not isinstance(browser_report, dict):
            raise LiveCatalogError("invalid live E2E reports for destination recheck")
        report = recheck_destination_failures(
            client,
            catalog_report,
            browser_report,
            signing_region=args.signing_region,
        )
    else:
        report = collect_catalog(
            client,
            partition=args.partition,
            signing_region=args.signing_region,
            concurrency=args.concurrency,
            include_direct=not args.skip_direct,
            direct_contract_timeout=args.direct_contract_timeout,
            checkpoint_path=args.checkpoint or args.output.with_name(args.output.name + ".events.jsonl"),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.recheck_catalog:
        print(json.dumps({**report["summary"], "output": str(args.output)}, ensure_ascii=False))
        return 0 if report["summary"]["inconclusiveCount"] == 0 else 1
    summary = {
        "catalog": report["catalog"]["resourceCount"],
        "regions": "{}/{}".format(report["scan"]["scannedRegionCount"], report["scan"]["regionCount"]),
        "stacks": report["scan"]["stackCount"],
        "resources": report["scan"]["stackResourceCount"],
        "discoveredTypes": report["coverage"]["discoveredTypeCount"],
        "rosSamples": report["coverage"]["sampledTypeCount"],
        "directContracts": report["directScan"]["contractCount"],
        "directSamples": report["directScan"]["sampledContractCount"],
        "failures": len(report["failures"]),
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LiveCatalogError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None
