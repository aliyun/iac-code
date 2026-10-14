"""Bridge visible Web events/history to one lazy, durable resource index.

Only dedicated resource metadata and reviewed stack/API projections enter the
index. Neither rendering nor recovery reads external tool files or credentials.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.resource_references import (
    SOURCE_VERSION_FIELDS,
    locator_text,
    resource_locator_text,
    resource_spec,
    sanitize_resource_reference,
    stack_resource_reference,
)
from iac_code.web.resource_accounts import clean_resource_account_context
from iac_code.web.resource_previews import PreviewSources, is_preview_source
from iac_code.web.resource_queries import (
    ResourceQueryAttempt,
    ResourceQueryCoordinator,
    ResourceQueryError,
    ResourceQueryPlan,
    plan_resource_query,
)
from iac_code.web.resource_references import (
    DECLARATION_SOURCE_PREFIX,
    ResourceReferenceStore,
    ResourceRevisionConflictError,
    resource_fact_from_web,
)
from iac_code.web.resource_sources import (
    VisibleResourceSource,
    canonical_source_json,
    clean_occurrence_group,
    clean_source,
    read_visible_source,
    resource_binding_source_key,
    source_contains_locator,
    tool_source_value,
)

logger = logging.getLogger(__name__)


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    return value if isinstance(value, dict) else {}


def _stack_facts(payload: dict[str, Any], tool_id: str, observed_at: float = 0) -> list[dict[str, Any]]:
    stack_id = payload.get("stackId") or payload.get("stack_id") or ""
    region_id = payload.get("regionId") or payload.get("region_id") or ""
    context = {
        "stack_id": stack_id,
        "region_id": region_id,
        "tool_use_id": tool_id,
        "account_scope": payload.get("accountScope") or payload.get("account_scope") or "",
        "console_partition": payload.get("consolePartition") or payload.get("console_partition") or "",
    }
    facts: list[dict[str, Any]] = []
    # Prior observations are not the terminal response. Retain their timestamps;
    # in particular a failed/empty terminal query cannot erase earlier locators.
    for field in ("resource_observations", "resources"):
        rows = payload.get(field)
        if field == "resources" and payload.get("resources_fresh") is False:
            continue
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            fact = stack_resource_reference(
                {"observed_at": observed_at, **row}, source_path=f"/{field}/{index}", **context
            )
            if fact is not None:
                facts.append(fact)
    if stack_id:
        fact = stack_resource_reference(
            {
                "resource_type": "ALIYUN::ROS::Stack",
                "physical_resource_id": stack_id,
                "status": payload.get("status"),
                "observed_at": observed_at,
            },
            source_path="/stack_id",
            **context,
        )
        if fact is not None:
            facts.append(fact)
    return facts


def _result_facts(payload: dict[str, Any], tool_id: str, observed_at: float = 0) -> list[dict[str, Any]]:
    items = payload.get("resourceReferences")
    facts = []
    for item in items if isinstance(items, list) else []:
        fact = sanitize_resource_reference(resource_fact_from_web(item))
        if fact is None:
            continue
        # A visible tool result must not import facts belonging to hidden tools.
        fact["sources"] = [source for source in fact["sources"] if source.get("tool_use_id") == tool_id]
        for source in fact["sources"]:
            # These fields prove a Web observation or explicit choice/query,
            # never an assertion that an external tool may make for itself.
            for key in (*SOURCE_VERSION_FIELDS, "query_source_digest", "query_parent_path", "source_id"):
                source.pop(key, None)
        if fact["sources"]:
            facts.append(fact)
    stack = payload.get("stackResult") or payload.get("stack_result")
    if isinstance(stack, dict):
        facts.extend(_stack_facts(stack, tool_id, observed_at))
    for artifact in payload.get("artifacts") or []:
        if isinstance(artifact, dict) and isinstance(artifact.get("stack_result"), dict):
            facts.extend(_stack_facts({**artifact, **artifact["stack_result"]}, tool_id, observed_at))
    return facts


def _tool_owner_key(tool: dict[str, Any]) -> tuple[str, str] | None:
    for field in ("turnId", "messageId"):
        value = tool.get(field)
        if isinstance(value, str) and value:
            return field, value
    return None


def _legacy_owner_contexts(tools: Any) -> dict[tuple[str, str], dict[str, str]]:
    """Reuse only one account/partition proved by another result in the same turn."""
    candidates: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for tool_id, tool in tools.items() if isinstance(tools, dict) else []:
        if not isinstance(tool_id, str) or not isinstance(tool, dict) or (owner := _tool_owner_key(tool)) is None:
            continue
        for result in tool.get("results") or []:
            if not isinstance(result, dict):
                continue
            for fact in _result_facts(result, tool_id):
                account = fact.get("account_scope")
                partition = fact.get("console_partition")
                account_text = locator_text(account)
                partition_text = locator_text(partition)
                if account_text and partition_text in {"aliyun", "alibabacloud"}:
                    candidates.setdefault(owner, set()).add((account_text, partition_text))
    return {
        owner: {"account_scope": next(iter(values))[0], "console_partition": next(iter(values))[1]}
        for owner, values in candidates.items()
        if len(values) == 1
    }


def _legacy_api_facts(
    tool: dict[str, Any], tool_id: str, owner_context: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Recover reviewed paths from already visible JSON, never from output files."""
    if tool.get("toolName") != "aliyun_api":
        return []
    request = _object(tool.get("input"))
    product, action = request.get("product"), request.get("action")
    if not isinstance(product, str) or not isinstance(action, str):
        return []
    params = _object(request.get("params"))
    region = request.get("region_id") or params.get("RegionId") or ""
    facts = []
    for result in tool.get("results") or []:
        if not isinstance(result, dict) or result.get("isError") or result.get("resultKind") == "error":
            continue
        if result.get("resourceReferences"):
            continue
        body = _object(result.get("content") or result.get("summary"))
        if not body:
            continue
        extracted = extract_api_resource_references(
            product=product,
            action=action,
            params=params,
            body=body,
            region_id=region,
            tool_use_id=tool_id,
            **(owner_context or {}),
        )
        # No historical observation time or account is implied by loading now.
        for fact in extracted:
            for source in fact["sources"]:
                source["observed_at"] = 0
        facts.extend(extracted)
    return facts


def _query_facts(tool: dict[str, Any], tool_id: str) -> list[dict[str, Any]]:
    # External/MCP results can display resource text, but cannot gain cloud
    # read authority by labeling their own metadata as an API observation.
    if tool.get("toolName") not in {"aliyun_api", "ros_deploy"}:
        return []
    facts = [
        fact
        for result in tool.get("results") or []
        if isinstance(result, dict)
        for fact in _result_facts(result, tool_id)
    ]
    if isinstance(tool.get("stackProgress"), dict):
        facts.extend(_stack_facts(tool["stackProgress"], tool_id))
    return facts


def _query_source_digest(tool: dict[str, Any], tool_id: str) -> str:
    # Include the authoritative facts as well as displayed text: a metadata-only
    # replacement or change of account must invalidate a cached attribute read.
    value = [tool_source_value(tool), _query_facts(tool, tool_id)]
    return hashlib.sha256(canonical_source_json(value).encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_source_json(value).encode("utf-8")).hexdigest()


def _request_digest(tool: dict[str, Any]) -> str:
    value = tool_source_value({"toolName": tool.get("toolName"), "input": tool.get("input")})
    # Absent and empty streamed input both mean no request yet. Object inputs
    # and their JSON stream representation must produce the same version.
    return _digest([value["toolName"], None if value["input"] == "" else value["input"]])


def _fact_versions(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = copy.deepcopy(facts)
    for value in values:
        for source in value["sources"]:
            source.pop("observed_at", None)
            for key in SOURCE_VERSION_FIELDS:
                source.pop(key, None)
    return values


def _stamp_facts(
    facts: list[dict[str, Any]],
    kind: str,
    request: str,
    revision: str,
    *,
    message_id: str = "",
    turn_id: str = "",
) -> list[dict[str, Any]]:
    for fact in facts:
        for source in fact["sources"]:
            source.update(source_kind=kind, source_request_digest=request, source_revision=revision)
            # Message/turn ownership is Web runtime provenance. Never trust an
            # optional producer-supplied owner, and keep it absent when the Web
            # bridge cannot prove one.
            source.pop("message_id", None)
            source.pop("turn_id", None)
            if message_id:
                source["message_id"] = message_id
            if turn_id:
                source["turn_id"] = turn_id
    return facts


def _versioned_result(
    result: dict[str, Any],
    tool: dict[str, Any],
    tool_id: str,
    request: str,
    observed_at: float = 0,
    *,
    message_id: str = "",
    turn_id: str = "",
    legacy_owner_context: dict[str, str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    facts = _result_facts(result, tool_id, observed_at)
    facts.extend(_legacy_api_facts({**tool, "results": [result]}, tool_id, legacy_owner_context))
    text = tool_source_value({"results": [result]})["results"]
    revision = _digest(
        [text, _fact_versions(facts), bool(result.get("isError") or result.get("resultKind") == "error")]
    )
    return revision, _stamp_facts(facts, "tool_result", request, revision, message_id=message_id, turn_id=turn_id)


def _snat_parent_facts(
    tools: dict[str, Any], incoming: list[dict[str, Any]], contexts: dict[tuple[str, str], dict[str, str]]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Join observed ROS and VPC SNAT IDs to exact, visible reads; never call cloud.

    A template property is not an existence observation. The ROS physical ID
    must instead equal the ID on a successful bounded SNAT row, whose table
    must belong to the exact gateway response. All three observations must have
    one Web turn, credential scope and region. A VPC fact must originate on
    its own exact SNAT read. Keep both types and all their base facts separate.
    """
    reads: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for tool_id, tool in tools.items():
        if not isinstance(tool, dict) or tool.get("toolName") != "aliyun_api" or not tool.get("turnId"):
            continue
        request = _object(tool.get("input"))
        action = request.get("action")
        if request.get("product") != "vpc" or action not in {"DescribeSnatTableEntries", "DescribeNatGateways"}:
            continue
        params = _object(request.get("params"))
        context = contexts.get(("turnId", tool["turnId"]))
        region = request.get("region_id") or params.get("RegionId")
        if (
            not context
            or not locator_text(region)
            or params.get("RegionId", region) != region
            or str(params.get("PageNumber")) != "1"
            or str(params.get("PageSize")) not in {str(size) for size in range(1, 11)}
        ):
            continue
        for result in tool.get("results") or []:
            if not isinstance(result, dict) or result.get("isError") or result.get("resultKind") == "error":
                continue
            body = _object(result.get("content") or result.get("summary"))
            if (
                not body.get("RequestId")
                or body.get("Code")
                or body.get("TotalCount") != 1
                or body.get("PageNumber") != 1
                or str(body.get("PageSize")) != str(params["PageSize"])
            ):
                continue
            container, field = (
                ("SnatTableEntries", "SnatTableEntry")
                if action == "DescribeSnatTableEntries"
                else ("NatGateways", "NatGateway")
            )
            rows = _object(body.get(container)).get(field)
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                continue
            row = rows[0]
            if row.get("Status") != "Available" or row.get("RegionId", region) != region:
                continue
            if action == "DescribeSnatTableEntries":
                if (
                    not locator_text(row.get("SnatEntryId"))
                    or row.get("SnatEntryId") != params.get("SnatEntryId")
                    or not locator_text(row.get("SnatTableId"))
                    or row.get("SnatTableId") != params.get("SnatTableId")
                    or not locator_text(row.get("NatGatewayId"))
                ):
                    continue
            elif (
                not locator_text(row.get("NatGatewayId"))
                or row.get("NatGatewayId") != params.get("NatGatewayId")
                or row.get("NetworkType") not in {"internet", "intranet"}
            ):
                continue
            revision, _ = _versioned_result(result, tool, tool_id, _request_digest(tool), legacy_owner_context=context)
            source = {
                "tool_use_id": tool_id,
                "source_path": f"/{container}/{field}/0",
                "product": "vpc",
                "action": action,
                "evidence": "api_response",
                "request_region_id": region,
                "turn_id": tool["turnId"],
                "source_kind": "tool_result",
                "source_request_digest": _request_digest(tool),
                "source_revision": revision,
            }
            reads.append((row, {**context, "region_id": region}, source))

    derived, current = [], set()
    for child in incoming:
        if child["resource_type"] not in {"ALIYUN::ECS::SNatEntry", "ALIYUN::VPC::SnatEntry"} or (
            child["evidence"] != "api_response"
        ):
            continue
        turns = {source.get("turn_id") for source in child["sources"]}
        if len(turns) != 1 or not next(iter(turns)):
            continue
        ros_sources = []
        for source in child["sources"]:
            tool = tools.get(source.get("tool_use_id"))
            if not isinstance(tool, dict) or tool.get("toolName") != "aliyun_api":
                continue
            request = _object(tool.get("input"))
            params = _object(request.get("params"))
            action = request.get("action")
            if (
                request.get("product") == "ros"
                and action in {"GetStackResource", "ListStackResources"}
                and source.get("product") == "ros"
                and source.get("action") == action
                and tool.get("turnId") in turns
                and child.get("stack_id")
                and params.get("StackId") == child["stack_id"]
                and (request.get("region_id") or params.get("RegionId")) == child.get("region_id")
                and (
                    action != "GetStackResource"
                    or (
                        child.get("logical_resource_id")
                        and params.get("LogicalResourceId") == child["logical_resource_id"]
                    )
                )
                and any(
                    isinstance(result, dict) and not result.get("isError") and result.get("resultKind") != "error"
                    for result in tool.get("results") or []
                )
            ):
                ros_sources.append(source)
        if child.get("status") in {"DELETE_COMPLETE", "Deleted"}:
            continue
        if child["resource_type"] == "ALIYUN::ECS::SNatEntry" and not ros_sources:
            continue
        matched = [
            (row, source)
            for row, context, source in reads
            if source["turn_id"] in turns
            and all(
                child.get(key) and child.get(key) == context.get(key)
                for key in ("account_scope", "console_partition", "region_id")
            )
        ]
        entries = [
            (row, source)
            for row, source in matched
            if source["action"] == "DescribeSnatTableEntries" and row.get("SnatEntryId") == child["identifier"]
        ]
        if child["resource_type"] == "ALIYUN::VPC::SnatEntry" and not any(
            source.get("product") == "vpc"
            and source.get("action") == "DescribeSnatTableEntries"
            and all(
                source.get(key) == entry_source.get(key)
                for key in ("tool_use_id", "source_request_digest", "source_revision", "turn_id")
            )
            for source in child["sources"]
            for _, entry_source in entries
        ):
            continue
        parents = {(row["NatGatewayId"], row["SnatTableId"]) for row, _ in entries}
        if len(parents) != 1:
            continue
        gateway_id, table_id = next(iter(parents))
        gateways = [
            (row, source)
            for row, source in matched
            if source["action"] == "DescribeNatGateways" and row.get("NatGatewayId") == gateway_id
        ]
        networks = {row["NetworkType"] for row, _ in gateways}
        if len(networks) != 1 or not all(
            isinstance(tables := _object(row.get("SnatTableIds")).get("SnatTableId"), list) and table_id in tables
            for row, _ in gateways
        ):
            continue
        attributes = {"NatGatewayId": gateway_id, "SnatTableId": table_id, "NetworkType": next(iter(networks))}
        if any(child["attributes"].get(key, value) != value for key, value in attributes.items()):
            continue
        enriched = copy.deepcopy(child)
        enriched["attributes"].update(attributes)
        enriched["sources"].extend(copy.deepcopy(source) for _, source in [*entries, *gateways])
        # A newly ambiguous chain must retract the old enrichment even while all
        # its old tool IDs remain visible. Rebuild this digest on every hydrate.
        chain = _digest([child, [list(pair) for pair in entries], [list(pair) for pair in gateways]])
        current.add(chain)
        for source in enriched["sources"][len(child["sources"]) :]:
            source["source_path"] = f"/snat-parent/{chain}" + source.get("source_path", "")
        derived.append(enriched)
    return derived, current


def _ots_parent_facts(
    tools: dict[str, Any], incoming: list[dict[str, Any]], contexts: dict[tuple[str, str], dict[str, str]]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Bind a native table's literal template parent to observed stack/table reads.

    A template alone proves no resource. Require its successful CreateStack ID,
    a live GetStack, its exact logical resource observation and the same table
    on ListTable against that literal parent. All sources stay in one Web turn,
    account scope and region. No cloud reads or template evaluation occur here.
    """
    reads = []
    for tool_id, tool in tools.items():
        if not isinstance(tool, dict) or tool.get("toolName") != "aliyun_api" or not tool.get("turnId"):
            continue
        context = contexts.get(("turnId", tool["turnId"]))
        request = _object(tool.get("input"))
        params = _object(request.get("params"))
        product, action = request.get("product"), request.get("action")
        if isinstance(product, str):
            product = product.casefold()
            if product == "ots":
                product = "tablestore"
        region = request.get("region_id") or params.get("RegionId")
        if (
            not context
            or not locator_text(region)
            or params.get("RegionId", region) != region
            or (product, action) not in {("ros", "CreateStack"), ("ros", "GetStack"), ("tablestore", "ListTable")}
        ):
            continue
        for result in tool.get("results") or []:
            if not isinstance(result, dict) or result.get("isError") or result.get("resultKind") == "error":
                continue
            body = _object(result.get("content"))
            if not body or body.get("Code") or body.get("ErrorCode"):
                continue
            if product == "ros" and not body.get("RequestId"):
                continue
            revision, _ = _versioned_result(result, tool, tool_id, _request_digest(tool), legacy_owner_context=context)
            source = {
                "tool_use_id": tool_id,
                "source_path": "/result",
                "product": product,
                "action": action,
                "evidence": "api_response",
                "request_region_id": region,
                "turn_id": tool["turnId"],
                "source_kind": "tool_result",
                "source_request_digest": _request_digest(tool),
                "source_revision": revision,
            }
            reads.append((params, body, {**context, "region_id": region}, source))

    derived, current = [], set()
    for child in incoming:
        if (
            child["resource_type"] != "ALIYUN::OTS::Table"
            or child["evidence"] != "api_response"
            or child.get("status") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            or not child.get("stack_id")
            or not child.get("logical_resource_id")
        ):
            continue
        turns = {source.get("turn_id") for source in child["sources"]}
        if len(turns) != 1 or not next(iter(turns)):
            continue
        valid_child = False
        for source in child["sources"]:
            tool = tools.get(source.get("tool_use_id"), {})
            request = _object(tool.get("input"))
            params = _object(request.get("params"))
            if (
                tool.get("toolName") == "aliyun_api"
                and request.get("product") == "ros"
                and request.get("action") in {"GetStackResource", "ListStackResources"}
                and params.get("StackId") == child["stack_id"]
                and (request.get("region_id") or params.get("RegionId")) == child.get("region_id")
                and params.get("RegionId", child.get("region_id")) == child.get("region_id")
                and (
                    request["action"] != "GetStackResource"
                    or params.get("LogicalResourceId") == child["logical_resource_id"]
                )
                and source.get("product") == "ros"
                and source.get("action") == request["action"]
            ):
                valid_child = True
        if not valid_child:
            continue
        matched = [
            (params, body, source)
            for params, body, context, source in reads
            if source["turn_id"] in turns
            and all(
                child.get(key) and child.get(key) == context.get(key)
                for key in ("account_scope", "console_partition", "region_id")
            )
        ]
        candidates = []
        for params, body, source in matched:
            if source["action"] != "CreateStack" or body.get("StackId") != child["stack_id"]:
                continue
            if params.get("TemplateURL") or params.get("TemplateId"):
                continue
            template = _object(params.get("TemplateBody"))
            if template.get("Transform") or template.get("ROSTemplateFormatVersion") != "2015-09-01":
                continue
            resource = _object(_object(template.get("Resources")).get(child["logical_resource_id"]))
            props = _object(resource.get("Properties"))
            parent, table = props.get("InstanceName"), props.get("TableName")
            if (
                resource.get("Type") != "ALIYUN::OTS::Table"
                or not locator_text(parent)
                or not locator_text(table)
                or child["identifier"] != table
                or any(
                    child["attributes"].get(key, value) != value
                    for key, value in {"InstanceName": parent, "TableName": table}.items()
                )
            ):
                continue
            candidates.append((parent, table, source))
        if len({(parent, table) for parent, table, _ in candidates}) != 1:
            continue
        parent, table, _ = candidates[0]
        stack_reads = [
            (_object(body.get("Stack")) or body, source)
            for params, body, source in matched
            if source["action"] == "GetStack" and params.get("StackId") == child["stack_id"]
        ]
        if not stack_reads:
            continue
        # Tool history preserves observation order. Earlier polling states do
        # not override the final read, and an old live read cannot hide deletion.
        stack, stack_source = stack_reads[-1]
        if (
            stack.get("StackId") != child["stack_id"]
            or stack.get("Status") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            or stack.get("RegionId") != child["region_id"]
            or stack.get("StackType") not in {None, "ROS"}
        ):
            continue
        table_reads = [
            (body, source)
            for params, body, source in matched
            if source["action"] == "ListTable" and params.get("InstanceName") == parent
        ]
        if not table_reads:
            continue
        body, table_source = table_reads[-1]
        if (
            body.get("InstanceName") != parent
            or not isinstance(body.get("Tables"), list)
            or len(
                [
                    row
                    for row in body["Tables"]
                    if isinstance(row, dict) and row.get("TableName") == table and row.get("InstanceName") == parent
                ]
            )
            != 1
        ):
            continue
        enriched = copy.deepcopy(child)
        enriched["attributes"].update(InstanceName=parent, TableName=table)
        support = [source for _, _, source in candidates] + [stack_source, table_source]
        chain = _digest([child, parent, table, support])
        current.add(chain)
        for source in copy.deepcopy(support):
            source["source_path"] = f"/ots-parent/{chain}" + source["source_path"]
            enriched["sources"].append(source)
        derived.append(enriched)
    return derived, current


def _versioned_progress(
    payload: dict[str, Any],
    tool_id: str,
    request: str,
    observed_at: float = 0,
    *,
    message_id: str = "",
    turn_id: str = "",
) -> list[dict[str, Any]]:
    facts = _stack_facts(payload, tool_id, observed_at)
    # A later frame may omit an earlier resource. These are historical
    # observations of the same request, not replacement terminal result text.
    for fact in facts:
        _stamp_facts(
            [fact],
            "stack_progress",
            request,
            _digest(_fact_versions([fact])),
            message_id=message_id,
            turn_id=turn_id,
        )
    return facts


class ResourceReferenceController:
    """One bridge per session, attached without reading sidecars during listing."""

    def __init__(
        self,
        path: Path,
        publish: Callable[[str, dict[str, Any]], Any],
        *,
        writable: Callable[[], bool] = lambda: True,
        batch_seconds: float = 0.15,
    ) -> None:
        self.path = path
        self.publish = publish
        self.writable = writable
        self.batch_seconds = batch_seconds
        self._store: ResourceReferenceStore | None = None
        self._pending: list[dict[str, Any]] = []
        self._timer: asyncio.TimerHandle | None = None
        self._writer: asyncio.Task[bool] | None = None
        self._closed = False
        self._live_tool_ids: set[str] = set()
        self._revoked_tool_ids: set[str] = set()
        self._tool_contexts: dict[str, dict[str, Any]] = {}
        self._request_versions: dict[str, str] = {}
        self._sources_dirty = False
        self.queries = ResourceQueryCoordinator()
        self._account_context: tuple[str, dict[str, str]] | None = None
        self.preview_sources = PreviewSources()

    @property
    def store(self) -> ResourceReferenceStore:
        if self._store is None:
            self._store = ResourceReferenceStore(self.path)
        return self._store

    def on_event(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        kind = event.get("type")
        if kind == "assistant.message.tombstone":
            removed = payload.get("affectedToolUseIds") or []
            self._revoked_tool_ids.update(removed)
            self._live_tool_ids.difference_update(removed)
            self._pending = [
                fact
                for fact in self._pending
                if all(source.get("tool_use_id") not in removed for source in fact["sources"])
            ]
            if self._store is not None:
                delta = self.store.retain_sources(lambda source: source.get("tool_use_id") not in removed)
                if delta:
                    self.publish("resource.references", delta)
                    self._schedule()
                message_id = payload.get("messageId")
                invalid = self.store.retain_bindings(
                    lambda binding: (
                        binding.get("source", {}).get("tool_use_id") not in removed
                        and (not message_id or binding.get("source", {}).get("message_id") != message_id)
                    )
                )
                if invalid:
                    self.publish("resource.references", invalid)
                    self._prune_declarations()
                    self._schedule()
            return
        tool_id = payload.get("toolUseId")
        if not isinstance(tool_id, str) or not tool_id:
            return
        if tool_id in self._revoked_tool_ids:
            return
        if kind in {"tool.started", "tool.result", "pipeline.event"}:
            self._live_tool_ids.add(tool_id)
        context = self._tool_contexts.setdefault(tool_id, {})
        old_request = _request_digest(context)
        if kind == "tool.started":
            if payload.get("toolName"):
                context["toolName"] = payload["toolName"]
            if "input" in payload:
                context["input"] = copy.deepcopy(payload["input"])
        elif kind == "tool.input.delta":
            context["input"] = str(context.get("input") or "") + str(payload.get("delta") or "")
        elif kind == "tool.finished" and payload.get("status") == "input_complete":
            summary = payload.get("summary")
            if isinstance(summary, dict):
                context.update({key: copy.deepcopy(summary[key]) for key in ("toolName", "input") if key in summary})
        for key in ("messageId", "turnId"):
            if isinstance(payload.get(key), str) and payload[key]:
                context[key] = payload[key]
        request = _request_digest(context)
        self._request_versions[tool_id] = request
        if request != old_request and (self._store is not None or self._pending):
            self._sources_dirty = True
        timestamp = event.get("createdAt") or event.get("timestamp") or ""
        try:
            observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            observed_at = 0
        if kind == "tool.result":
            self._pending.extend(
                _versioned_result(
                    payload,
                    context,
                    tool_id,
                    request,
                    observed_at,
                    message_id=str(context.get("messageId") or ""),
                    turn_id=str(context.get("turnId") or ""),
                )[1]
            )
        elif kind == "pipeline.event" and payload.get("kind") == "stack.progress":
            self._pending.extend(
                _versioned_progress(
                    payload,
                    tool_id,
                    request,
                    observed_at,
                    message_id=str(context.get("messageId") or ""),
                    turn_id=str(context.get("turnId") or ""),
                )
            )
        if self._pending or self._sources_dirty:
            self._schedule()

    def _schedule(self) -> None:
        if self._closed or self._timer is not None or self._writer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Synchronous callers drain explicitly on snapshot/flush.
        self._timer = loop.call_later(self.batch_seconds, self._start_writer)

    def _start_writer(self) -> None:
        self._timer = None
        if self._closed:
            return
        self._writer = asyncio.create_task(self.flush())
        self._writer.add_done_callback(self._writer_done)

    def _writer_done(self, task: asyncio.Task[bool]) -> None:
        if self._writer is task:
            self._writer = None
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                # Do not log raw locators, attributes or cloud response bodies.
                logger.warning("Resource reference batch failed")
        if self._pending or self._sources_dirty:
            self._schedule()

    def _live_source_current(self, source: dict[str, Any]) -> bool:
        tool_id = source.get("tool_use_id", "")
        if tool_id in self._revoked_tool_ids:
            return False
        current = self._request_versions.get(tool_id)
        request = source.get("source_request_digest")
        return not request or current is None or request == current

    def _drain(self) -> None:
        if (not self._pending and not self._sources_dirty) or self._closed:
            return
        pending, self._pending = self._pending, []
        self._sources_dirty = False
        pending = [fact for fact in pending if all(self._live_source_current(source) for source in fact["sources"])]
        delta = self.store.reconcile(pending, self._live_source_current)
        if delta is not None:
            self.publish("resource.references", delta)
        invalid = self.store.retain_bindings(lambda binding: True)
        if invalid:
            self.publish("resource.references", invalid)

    def hydrate(self, transcript: dict[str, Any]) -> dict[str, Any]:
        """Only traverse the already-authorized, visible transcript projection."""
        transcript = self.preview_sources.project(transcript)
        if self._closed:
            return self.store.snapshot()
        tools = transcript.get("tools")
        visible_tool_ids = set(tools) if isinstance(tools, dict) else set()
        self._live_tool_ids.difference_update(visible_tool_ids)
        allowed_tool_ids = visible_tool_ids | self._live_tool_ids
        declaration_sources = self._declaration_sources()
        query_digests: dict[str, str] = {}
        result_versions: dict[str, set[str]] = {}
        incoming = []
        legacy_contexts = _legacy_owner_contexts(tools)
        for tool_id, tool in tools.items() if isinstance(tools, dict) else []:
            if not isinstance(tool, dict) or tool_id in self._revoked_tool_ids:
                continue
            self._tool_contexts[tool_id] = {
                key: copy.deepcopy(tool[key]) for key in ("toolName", "input", "messageId", "turnId") if key in tool
            }
            request = _request_digest(tool)
            owner = _tool_owner_key(tool)
            self._request_versions[tool_id] = request
            result_versions[tool_id] = set()
            for result in tool.get("results") or []:
                if isinstance(result, dict):
                    version, facts = _versioned_result(
                        result,
                        tool,
                        tool_id,
                        request,
                        message_id=str(tool.get("messageId") or ""),
                        turn_id=str(tool.get("turnId") or ""),
                        legacy_owner_context=legacy_contexts.get(owner) if owner is not None else None,
                    )
                    result_versions[tool_id].add(version)
                    incoming.extend(facts)
            progress = tool.get("stackProgress")
            if isinstance(progress, dict):
                incoming.extend(
                    _versioned_progress(
                        progress,
                        tool_id,
                        request,
                        message_id=str(tool.get("messageId") or ""),
                        turn_id=str(tool.get("turnId") or ""),
                    )
                )

        visible_tools = {
            key: value
            for key, value in (tools.items() if isinstance(tools, dict) else [])
            if key not in self._revoked_tool_ids
        }
        snat_facts, snat_chains = _snat_parent_facts(visible_tools, incoming, legacy_contexts)
        incoming.extend(snat_facts)
        ots_facts, ots_chains = _ots_parent_facts(visible_tools, incoming, legacy_contexts)
        incoming.extend(ots_facts)

        def retained(source: dict[str, Any]) -> bool:
            path = source.get("source_path", "")
            if path.startswith("/ots-parent/") and path.split("/", 3)[2] not in ots_chains:
                return False
            if path.startswith("/snat-parent/") and path.split("/", 3)[2] not in snat_chains:
                return False
            tool_id = source.get("tool_use_id", "")
            if not self._live_source_current(source):
                return False
            if source.get("query_source_digest"):
                tool = tools.get(tool_id) if isinstance(tools, dict) else None
                if not isinstance(tool, dict):
                    return False
                if tool_id not in query_digests:
                    query_digests[tool_id] = _query_source_digest(tool, tool_id)
                return source["query_source_digest"] == query_digests[tool_id]
            if source.get("source_id") in declaration_sources:
                return True
            if tool_id not in allowed_tool_ids or not source.get("source_kind"):
                # Unversioned legacy contributions must be rebuilt from current
                # visible facts; a surviving tool ID cannot validate old fields.
                return False
            if source["source_kind"] == "stack_progress" or tool_id not in visible_tool_ids:
                return True
            return source.get("source_revision") in result_versions.get(tool_id, set())

        pending, self._pending = self._pending, []
        self._sources_dirty = False
        incoming.extend(fact for fact in pending if all(retained(source) for source in fact["sources"]))
        delta = self.store.reconcile(incoming, retained)
        if delta:
            self.publish("resource.references", delta)
        # Many appearance groups can share one large message/tool output. Read
        # and hash that source once per hydration, never once per occurrence.
        source_cache: dict[tuple[tuple[str, str], ...], VisibleResourceSource] = {}
        proof_cache: dict[tuple[str, str, str, str], bool] = {}
        invalid = self.store.retain_bindings(
            lambda binding: self._binding_source_current(binding, transcript, source_cache, proof_cache)
        )
        if invalid:
            self.publish("resource.references", invalid)
        self._prune_declarations()
        if self._store is not None and self.store.persisted_revision != self.store.revision:
            self._schedule()
        return self.store.snapshot()

    def _declaration_sources(self) -> set[str]:
        return {
            value
            for binding in self.store.bindings.values()
            if isinstance(value := binding.get("declaration_source_id"), str)
            and value.startswith(DECLARATION_SOURCE_PREFIX)
        }

    def _prune_declarations(self) -> None:
        retained = self._declaration_sources()
        delta = self.store.retain_sources(
            lambda source: (
                not source.get("source_id", "").startswith(DECLARATION_SOURCE_PREFIX) or source["source_id"] in retained
            )
        )
        if delta:
            self.publish("resource.references", delta)

    def _binding_source_current(
        self,
        binding: dict[str, Any],
        transcript: dict[str, Any],
        source_cache: dict[tuple[tuple[str, str], ...], VisibleResourceSource],
        proof_cache: dict[tuple[str, str, str, str], bool],
    ) -> bool:
        try:
            descriptor = clean_source(binding.get("source"))
            source_id = tuple(sorted(descriptor.items()))
            source = source_cache.get(source_id)
            if source is None:
                source = read_visible_source(transcript, descriptor)
                source_cache[source_id] = source
            reference_id = binding.get("reference_id")
            reference = self.store.references.get(reference_id) if isinstance(reference_id, str) else None
            if reference is None or source.digest != binding.get("source_digest"):
                return False
            spec = resource_spec(reference["resource_type"])
            if spec is None:
                return False
            declaration = binding.get("declaration_source_id")
            if declaration is not None and not any(
                item.get("source_id") == declaration for item in reference["sources"]
            ):
                return False
            group = clean_occurrence_group(
                binding.get("group"), binding["identifier"], binding["identifier_kind"], spec
            )
            if binding.get("source_key") != resource_binding_source_key(source.digest, group):
                return False
            proof_key = (
                source.digest,
                binding["identifier"],
                binding["identifier_kind"],
                spec.resource_type + (":declared" if declaration else ""),
            )
            if proof_key not in proof_cache:
                proof_cache[proof_key] = source_contains_locator(
                    source, binding["identifier"], binding["identifier_kind"], spec, require_type=bool(declaration)
                )
            return proof_cache[proof_key]
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
            return False

    def offer_account_context(self, context: dict[str, str]) -> dict[str, Any]:
        """Offer one server-read context without creating a resource observation."""
        if self._closed or not self.writable():
            raise ValueError("resource_session_unavailable")
        clean = clean_resource_account_context(context)
        if not clean:
            self._account_context = None
            return {"status": "unavailable"}
        if self._account_context is None or self._account_context[1] != clean:
            self._account_context = ("account-context:" + secrets.token_hex(16), clean)
        token, context = self._account_context
        return {
            "status": "available",
            "choice": {
                "accountContextId": token,
                "accountScope": context["account_scope"],
                **({"consolePartition": context["console_partition"]} if context.get("console_partition") else {}),
            },
        }

    def has_account_scope(self, account_scope: Any) -> bool:
        """Whether a durable fact in this session carries the opaque scope."""
        if self._closed or not locator_text(account_scope):
            return False
        self._drain()
        return any(item.get("account_scope") == account_scope for item in self.store.references.values())

    def _update_preview_choices(self, choices: dict[str, dict[str, Any]]) -> None:
        delta = self.store.update_preview_choices(choices)
        if delta:
            self.publish("resource.references", delta)
            self._schedule()

    def forget_preview(self, source: dict[str, str], transcript: dict[str, Any]) -> None:
        self.preview_sources.forget(source)
        self._update_preview_choices(
            {key: value for key, value in self.store.preview_choices.items() if value["source"] != source}
        )
        self.hydrate(transcript)

    async def observe_preview(self, source: dict[str, str], content: str, transcript: dict[str, Any]) -> dict[str, Any]:
        """Activate bookmarks only after the existing endpoint actually read a file."""
        if self._closed:
            raise ValueError("resource_session_unavailable")
        if content == "":
            # An empty file is a valid preview, but proves no occurrence.
            self.forget_preview(source, transcript)
            return self.hydrate(transcript)
        self.preview_sources.observe(source, content)
        visible = self.preview_sources.project(transcript)
        current = read_visible_source(visible, source)
        self._update_preview_choices(
            {
                key: value
                for key, value in self.store.preview_choices.items()
                if value["source"] != source or value["source_digest"] == current.digest
            }
        )
        self.hydrate(visible)
        for key, choice in list(self.store.preview_choices.items()):
            # A temporarily unavailable writer is not evidence that a saved
            # choice became invalid. Leave it dormant for a later retry.
            if self._closed or not self.writable():
                break
            if choice["source"] != source or any(
                binding["source_key"] == key for binding in self.store.bindings.values()
            ):
                continue
            payload = {
                "expectedRevision": self.store.revision,
                "source": choice["source"],
                "sourceDigest": choice["source_digest"],
                "group": choice["group"],
                "identifier": choice["identifier"],
                "identifierKind": choice["identifier_kind"],
                "context": choice["context"],
            }
            if not choice["declared"]:
                payload["referenceId"] = choice["reference_id"]
            try:
                await self.resolve_choice(visible, payload, _preview_restore=choice)
            except (ValueError, ResourceRevisionConflictError):
                # A missing/changed API identity or newly observed lifecycle is
                # not an invitation to retarget the bookmark by name.
                if self.store.preview_choices.get(key) == choice:
                    self._update_preview_choices({k: v for k, v in self.store.preview_choices.items() if k != key})
        return self.hydrate(visible)

    async def resolve_choice(
        self,
        transcript: dict[str, Any],
        raw: Any,
        *,
        load_account_context: Callable[[], Awaitable[dict[str, str]]] | None = None,
        _preview_restore: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save explicit context; only an offered credential choice is re-read."""
        if self._closed or not self.writable():
            raise ValueError("resource_session_unavailable")
        transcript = self.preview_sources.project(transcript)
        if not isinstance(raw, dict) or set(raw) - {
            "expectedRevision",
            "source",
            "sourceDigest",
            "group",
            "identifier",
            "identifierKind",
            "referenceId",
            "context",
            "accountContextId",
        }:
            raise ValueError("resource_choice_invalid")
        expected = raw.get("expectedRevision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("resource_choice_invalid")
        self.hydrate(transcript)
        if expected != self.store.revision:
            raise ResourceRevisionConflictError("resource_revision_conflict")
        reference_id = raw.get("referenceId")
        reference: dict[str, Any] | None = (
            self.store.references.get(reference_id) if isinstance(reference_id, str) else None
        )
        if reference is None and reference_id is not None:
            raise ValueError("resource_reference_unavailable")
        identifier, kind = raw.get("identifier"), raw.get("identifierKind")
        if kind not in {"id", "name"}:
            raise ValueError("resource_occurrence_invalid")
        if reference is not None and not any(
            item["identifier"] == identifier and item["identifier_kind"] == kind
            for item in [reference, *reference["aliases"]]
        ):
            raise ValueError("resource_occurrence_invalid")
        raw_group = raw.get("group")
        spec = resource_spec(
            reference["resource_type"]
            if reference
            else raw_group[3]
            if isinstance(raw_group, list) and len(raw_group) == 10
            else None
        )
        if spec is None:
            raise ValueError("resource_occurrence_invalid")
        field = raw_group[2] if isinstance(raw_group, list) and len(raw_group) == 10 else ""
        if not resource_locator_text(identifier, spec, field):
            raise ValueError("resource_occurrence_invalid")
        group = clean_occurrence_group(raw.get("group"), identifier, kind, spec)
        source = read_visible_source(transcript, resource_fact_from_web(raw.get("source")))
        if source.digest != raw.get("sourceDigest"):
            raise ResourceRevisionConflictError("resource_source_changed")
        declaring = reference is None or all(
            item.get("source_id", "").startswith(DECLARATION_SOURCE_PREFIX) for item in reference["sources"]
        )
        if not source_contains_locator(source, identifier, kind, spec, require_type=declaring):
            raise ValueError("resource_occurrence_unverified")
        context = resource_fact_from_web(raw.get("context") or {})
        if not isinstance(context, dict) or set(context) - {
            "account_scope",
            "console_partition",
            "region_id",
            "attributes",
        }:
            raise ValueError("resource_context_invalid")
        attributes = context.get("attributes", {})
        if not isinstance(attributes, dict) or set(attributes) - set(spec.parent_fields):
            raise ValueError("resource_context_invalid")
        if any(not locator_text(value) for key, value in context.items() if key != "attributes"):
            raise ValueError("resource_context_invalid")
        if any(not locator_text(value) for value in attributes.values()):
            raise ValueError("resource_context_invalid")
        account = context.get("account_scope")
        account_context_id = raw.get("accountContextId")
        restoring_preview = _preview_restore is not None
        if restoring_preview and (
            self.store.preview_choices.get(_preview_restore["source_key"]) != _preview_restore
            or _preview_restore["source"] != source.descriptor
            or _preview_restore["source_digest"] != source.digest
            or _preview_restore["group"] != group
            or _preview_restore["context"] != context
        ):
            raise ValueError("resource_preview_choice_invalid")
        if account_context_id is not None:
            offered = self._account_context
            if offered is None or account_context_id != offered[0] or load_account_context is None:
                raise ResourceRevisionConflictError("resource_account_context_changed")
            if account != offered[1]["account_scope"]:
                raise ValueError("resource_context_invalid")
            if offered[1].get("console_partition") not in {None, context.get("console_partition")}:
                raise ValueError("resource_context_conflict")
            fresh = clean_resource_account_context(await load_account_context())
            if self._closed or not self.writable():
                raise ValueError("resource_session_unavailable")
            if self._account_context != offered or fresh != offered[1]:
                raise ResourceRevisionConflictError("resource_account_context_changed")
            self._drain()
            if self.store.revision != expected:
                raise ResourceRevisionConflictError("resource_revision_conflict")
        elif (
            account
            and not restoring_preview
            and not any(item.get("account_scope") == account for item in self.store.references.values())
        ):
            raise ValueError("resource_context_invalid")
        if context.get("console_partition") not in {None, "aliyun", "alibabacloud"}:
            raise ValueError("resource_context_invalid")
        if spec.scope_kind == "account":
            context.pop("region_id", None)
        if declaring:
            required = ["account_scope", "console_partition", *([] if spec.scope_kind == "account" else ["region_id"])]
            if any(not context.get(field) for field in required) or any(
                not attributes.get(field) for field in spec.parent_fields
            ):
                raise ValueError("resource_context_incomplete")
            reference = {
                "resource_type": spec.resource_type,
                "identifier": identifier,
                "identifier_kind": kind,
                "identifier_field": group[2],
                "attributes": {},
            }
        assert reference is not None
        group_scope = dict(zip(("account_scope", "console_partition", "region_id"), group[4:7]))
        for field, value in group_scope.items():
            if spec.scope_kind == "account" and field == "region_id":
                continue
            selected = reference.get(field) or context.get(field)
            if value and selected and value != selected:
                raise ValueError("resource_context_conflict")
        for field, value in group[7]:
            selected = reference["attributes"].get(field) or attributes.get(field)
            if selected and value != selected:
                raise ValueError("resource_context_conflict")
        source_key = resource_binding_source_key(source.digest, group)
        provenance: dict[str, Any] = {"source": source.descriptor, "source_digest": source.digest, "group": group}
        if declaring:
            # Deterministic for idempotent retries. This is a source version,
            # not an account ID, and contains no credential-derived material.
            declaration_id = (
                DECLARATION_SOURCE_PREFIX
                + hashlib.sha256(canonical_source_json([source_key, context]).encode("utf-8")).hexdigest()
            )
            fact = sanitize_resource_reference(
                {
                    **reference,
                    **context,
                    "evidence": "user_context",
                    "sources": [{"source_id": declaration_id, "evidence": "user_context"}],
                }
            )
            if fact is None:
                raise ValueError("resource_occurrence_invalid")
            for known in self.store.references.values():
                if all(value.get("source_id", "").startswith(DECLARATION_SOURCE_PREFIX) for value in known["sources"]):
                    continue
                if resource_spec(known["resource_type"]) != spec or not any(
                    item["identifier"] == identifier and item["identifier_kind"] == kind
                    for item in [known, *known["aliases"]]
                ):
                    continue
                fields = [
                    "account_scope",
                    "console_partition",
                    *([] if spec.scope_kind == "account" else ["region_id"]),
                ]
                if all(known.get(field) == fact.get(field) for field in fields) and all(
                    known["attributes"].get(field) == fact["attributes"].get(field) for field in spec.parent_fields
                ):
                    # A late observation (or a deleted/recreated name) must be
                    # explicitly selected, not bypassed with a new declaration.
                    raise ResourceRevisionConflictError("resource_reference_required")
            observed = self.store.observe(
                [fact], restored_declaration_id=_preview_restore["reference_id"] if _preview_restore else None
            )
            if observed:
                self.publish("resource.references", observed)
            reference_id = next(
                item["reference_id"]
                for item in self.store.references.values()
                if any(value.get("source_id") == declaration_id for value in item["sources"])
            )
            provenance["declaration_source_id"] = declaration_id
            expected = self.store.revision
        else:
            # Re-selecting a declared identity retains its source dependency;
            # otherwise a refresh could remove it before loading this binding.
            existing = next(
                (
                    item
                    for item in self.store.bindings.values()
                    if item["source_key"] == source_key and item["reference_id"] == reference_id
                ),
                None,
            )
            if existing and existing.get("declaration_source_id"):
                provenance["declaration_source_id"] = existing["declaration_source_id"]
        delta = self.store.bind(
            source_key=source_key,
            identifier=identifier,
            identifier_kind=kind,
            reference_id=reference_id,
            expected_revision=expected,
            provenance=provenance,
            context=context,
        )
        if delta:
            self.publish("resource.references", delta)
        if is_preview_source(source.descriptor):
            choice = {
                **provenance,
                "source_key": source_key,
                "identifier": identifier,
                "identifier_kind": kind,
                "reference_id": reference_id,
                "resource_type": spec.resource_type,
                "context": context,
                "declared": declaring,
            }
            choice.pop("declaration_source_id", None)
            self._update_preview_choices({**self.store.preview_choices, source_key: choice})
        self._prune_declarations()
        saved = await self.flush()
        latest = next(
            (
                binding
                for binding in self.store.bindings.values()
                if binding["source_key"] == source_key
                and binding["identifier"] == identifier
                and binding["identifier_kind"] == kind
            ),
            None,
        )
        if latest is None or latest["reference_id"] != reference_id or latest.get("context", {}) != context:
            # Another tab can choose a different target while this write is
            # awaiting disk I/O. Acknowledge only the choice actually requested.
            raise ResourceRevisionConflictError("resource_choice_changed")
        # A source can be invalidated while the write is in flight. The caller
        # revalidates history before exposing this response to the browser.
        return {
            "status": "saved" if saved else "not_saved",
            "revision": self.store.revision,
            "sourceKey": source_key,
            "referenceId": reference_id,
            "persistenceStatus": self.store.persistence_status,
            "snapshot": self.store.snapshot(),
        }

    def snapshot(self, *, cursor: str | None = None, limit: int = 500) -> dict[str, Any]:
        self._drain()
        return self.store.snapshot(cursor=cursor, limit=limit)

    async def resolve_attributes(
        self,
        raw: Any,
        *,
        load_current: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]],
        reader: Callable[[ResourceQueryPlan, ResourceQueryAttempt], Awaitable[list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        """An explicit read, independent from saving a user's context choice."""
        if self._closed or not self.writable():
            raise ValueError("resource_session_unavailable")
        if (
            not isinstance(raw, dict)
            or set(raw)
            - {
                "expectedRevision",
                "source",
                "sourceDigest",
                "group",
                "identifier",
                "identifierKind",
                "referenceId",
                "queryMissingAttributes",
            }
            or raw.get("queryMissingAttributes") is not True
        ):
            raise ValueError("resource_query_invalid")

        async def current_source():
            value = load_current()
            value = await value if inspect.isawaitable(value) else value
            if not isinstance(value, dict):
                raise ValueError("resource_source_unavailable")
            return self.preview_sources.project(cast(dict[str, Any], value))

        transcript = await current_source()
        self.hydrate(transcript)
        expected = raw.get("expectedRevision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != self.store.revision:
            raise ResourceRevisionConflictError("resource_revision_conflict")
        reference_id = raw.get("referenceId")
        reference = self.store.references.get(reference_id) if isinstance(reference_id, str) else None
        if reference is None:
            raise ValueError("resource_reference_unavailable")
        identifier, kind = raw.get("identifier"), raw.get("identifierKind")
        if not any(
            item["identifier"] == identifier and item["identifier_kind"] == kind
            for item in [reference, *reference["aliases"]]
        ):
            raise ValueError("resource_occurrence_invalid")
        spec = resource_spec(reference["resource_type"])
        assert spec is not None
        clean_occurrence_group(raw.get("group"), identifier, kind, spec)
        source = read_visible_source(transcript, resource_fact_from_web(raw.get("source")))
        if source.digest != raw.get("sourceDigest"):
            raise ResourceRevisionConflictError("resource_source_changed")
        if not source_contains_locator(source, identifier, kind, spec):
            raise ValueError("resource_occurrence_unverified")
        # Restrict the proof to one actual current tool, never a mixture of
        # stale sidecar fields and a user's selected stack/account parameters.
        tools = transcript.get("tools", {})
        candidates = []
        for tool_id in sorted({item.get("tool_use_id", "") for item in reference["sources"]}):
            tool = tools.get(tool_id)
            if not isinstance(tool, dict) or tool_id in self._revoked_tool_ids:
                continue
            try:
                plan = plan_resource_query(reference, _query_facts(tool, tool_id))
            except ResourceQueryError:
                continue
            candidates.append((plan, tool_id, _query_source_digest(tool, tool_id)))
        if not candidates:
            raise ResourceQueryError("resource_query_source_unverified")
        if len({plan for plan, _, _ in candidates}) != 1:
            raise ResourceQueryError("resource_query_ambiguous")
        plan, tool_id, digest = candidates[0]
        parent_path = None
        for fact in _query_facts(tools[tool_id], tool_id):
            try:
                if plan_resource_query(reference, [fact]) == plan:
                    parent_path = fact["sources"][0].get("source_path", "")
                    break
            except ResourceQueryError:
                continue
        if parent_path is None:
            raise ResourceQueryError("resource_query_source_unverified")
        plan = replace(plan, source_version=digest)

        async def current() -> bool:
            if self._closed or not self.writable() or tool_id in self._revoked_tool_ids:
                return False
            try:
                latest = await current_source()
                if self._closed or not self.writable() or tool_id in self._revoked_tool_ids:
                    return False
                tool = latest.get("tools", {}).get(tool_id)
                if not isinstance(tool, dict) or _query_source_digest(tool, tool_id) != digest:
                    return False
                if read_visible_source(latest, source.descriptor).digest != source.digest:
                    return False
                self._drain()
                latest_reference = self.store.references.get(reference_id)
                return (
                    latest_reference is not None
                    and replace(
                        plan_resource_query(latest_reference, _query_facts(tool, tool_id)), source_version=digest
                    )
                    == plan
                )
            except (ValueError, TypeError, KeyError):
                return False

        fact = await self.queries.query(plan, reader, is_current=current)
        # Store only whitelisted result facts with an independently revocable
        # contribution. Replaying the original observation cannot overwrite it.
        fact["sources"] = [
            {
                "tool_use_id": tool_id,
                "source_path": "/resource-query/" + reference_id,
                "query_source_digest": digest,
                "query_parent_path": parent_path,
                "source_kind": "attribute_query",
                "source_request_digest": _request_digest(tools[tool_id]),
                "source_revision": digest,
                "product": plan.product,
                "action": plan.action,
                "request_region_id": plan.region_id,
                "stack_id": plan.stack_id,
                "logical_resource_id": plan.logical_resource_id,
                "evidence": "api_response",
                "observed_at": max((item.get("observed_at", 0) for item in fact["sources"]), default=0),
            }
        ]
        delta = self.store.observe([fact])
        if delta:
            self.publish("resource.references", delta)
        saved = await self.flush()
        latest = await current_source()
        latest_snapshot = self.hydrate(latest)
        resolved_ids = [
            value["reference_id"]
            for value in self.store.references.values()
            if any(
                item.get("query_source_digest") == digest
                and item.get("tool_use_id") == tool_id
                and item.get("source_path") == "/resource-query/" + reference_id
                for item in value["sources"]
            )
        ]
        if read_visible_source(latest, source.descriptor).digest != source.digest or len(resolved_ids) != 1:
            raise ResourceRevisionConflictError("resource_source_changed")
        return {
            "status": "attributes_loaded",
            "persistenceStatus": "saved" if saved else "not_saved",
            "referenceId": resolved_ids[0],
            "snapshot": latest_snapshot,
            "revision": latest_snapshot["revision"],
        }

    async def flush(self) -> bool:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._drain()
        if self._closed or self._store is None:
            return True
        if not self.writable():
            return False
        # The store serializes writes. A concurrent batch may add facts while
        # an atomic replace is running; flush until that revision is durable.
        while not self._closed:
            saved = await self.store.flush()
            self._drain()
            if saved and self.store.persisted_revision == self.store.revision:
                return True
            if self.store.persistence_status in {"write_failed", "unsupported_version"}:
                return False
        return False

    async def close(self) -> bool:
        saved = await self.flush()
        self.close_for_delete()
        return saved

    def close_for_delete(self) -> None:
        self._closed = True
        self._account_context = None
        self.preview_sources.clear()
        self.queries.close()
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending.clear()
        self._tool_contexts.clear()
        self._request_versions.clear()
        if self._store is not None:
            self._store.close_for_delete()
