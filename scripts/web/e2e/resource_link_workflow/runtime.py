"""Program-owned operations; models receive narrow tools, not shell/HTTP credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.request
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator

from . import (
    additional_nas,
    additional_source,
    cleanup_recovery,
    console_sources,
    creation,
    delays,
    existing_query,
    hourly_budget,
    mse_correction,
    nas_correction,
    nas_rejection,
    native_retry,
    ownership,
    phases,
    plan_handoff,
    plans,
    provider_trial,
    remaining_review,
    reports,
    selection,
    temporary_samples,
    wire_evidence,
    worker_hosts,
)
from . import store as store_module
from .files import WorkflowError, atomic_write, contained, digest, json_write, read_json
from .phases import (
    assert_cleanup_reads_complete,
    assert_navigation_before_cleanup,
    assert_stack_source_ready,
    execution_request,
    readonly_continuation,
)
from .store import Store

RUNTIME_LOADED_SHA = digest(Path(__file__))


def preparation_read_api(product: str, action: str) -> bool:
    """Named preparation reads, including one officially reviewed non-prefix API.

    ONS uses OnsInstanceInServiceList for its regional instance list. This is
    not an Ons* exception: resource-specific candidate and official-evidence
    checks still apply at registration, and each read remains once-only.
    """
    return isinstance(action, str) and (
        action.startswith(("List", "Get", "Describe"))
        or (product, action) == ("ons", "OnsInstanceInServiceList")
    )


def refreshed_query_candidates(resource: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Refresh a missing preparation input only from this type's reviewed read contracts.

    This is not an execution grant or a change to frozen scenes/history. Existing
    nonempty candidates are preserved; writes and unrelated type/alias mappings
    can never enter the preparation allowlist through this projection.
    """
    existing = resource.get("query_candidates", [])
    if existing:
        return existing
    source_types = {item["source_type"] for item in resource["dimensions"]["QUERY"]["contracts"]}
    identities = {item["resourceType"]: item for item in catalog["resources"]}
    canonical = {
        name for name, item in identities.items()
        if source_types & {name, *item.get("aliases", [])}
    }
    candidates = []
    for item in catalog["responseContracts"]:
        product, action = item["product"], item["action"]
        if (item["resourceType"] not in canonical or not product
                or not preparation_read_api(product, action)):
            continue
        candidates.append({"product": product, "action": action,
                           "contract": "/".join((product, action, item["resourceType"], ".".join(item["path"])))})
    return candidates


def query_source_card_binding(
    references: list[dict[str, Any]], identifier: str, resource_type: str,
) -> dict[str, str] | None:
    """Reveal only one exact card from the already wire-proven QUERY references.

    A model-authored Markdown URL is not a structured resource link. Its own
    API result card may still contain the correct link behind native details.
    Never choose between multiple source owners or expose thinking as a source.
    """
    owners = {
        (source.get("toolUseId"), source.get("turnId"))
        for reference in references
        if reference.get("identifier") == identifier and reference.get("resourceType") == resource_type
        for source in reference.get("sources", [])
        if source.get("evidence") == "api_response" and source.get("sourceKind") == "tool_result"
    }
    if len(owners) != 1:
        return None
    owner, turn = next(iter(owners))
    if (
        not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", owner)
        or not isinstance(turn, str) or not re.fullmatch(r"[A-Za-z0-9-]+", turn)
    ):
        raise WorkflowError("invalid exact QUERY source card identity")
    return {"source_card_tool_use_id": owner, "source_card_turn_id": turn}


def managed_source_card_binding(
    references: list[dict[str, Any]], identifier: str, resource_type: str,
) -> dict[str, str] | None:
    """Reveal only this wire/creation-proven member's original ROS result card.

    Ordinary managed TF execution needs the same native card expansion as a
    navigation recovery. A query, stack summary or cleanup response cannot own
    the member link. Missing/ambiguous provenance never selects an arbitrary card.
    """
    members = []
    for reference in references:
        if reference.get("identifier") != identifier or reference.get("resourceType") != resource_type:
            continue
        stack, logical = reference.get("stackId"), reference.get("logicalResourceId")
        if not stack or not logical:
            continue
        sources = [source for source in reference.get("sources", [])
                   if source.get("product") == "ros"
                   and source.get("action") in {"GetStackResource", "ListStackResources"}
                   and source.get("stackId") == stack and source.get("logicalResourceId") == logical]
        members.append({**reference, "sources": sources})
    return query_source_card_binding(members, identifier, resource_type)


def reference_region_matches(reference: dict[str, Any], region: str, tool_use_id: str | None = None) -> bool:
    """Keep regional identity exact; account resources bind the actual request region.

    Account-level locators deliberately have no regionId. Trust only a reviewed
    resource identity spec and the same original API source, never a model's
    scopeKind flag alone or another source's unrelated request region.
    """
    from iac_code.tools.cloud.resource_references import resource_spec

    spec = resource_spec(reference.get("resourceType"))
    if spec and spec.scope_kind == "account":
        return (
            reference.get("scopeKind") == "account"
            and not reference.get("regionId")
            and any(
                source.get("evidence") == "api_response"
                and source.get("requestRegionId") == region
                and source.get("toolUseId")
                and (tool_use_id is None or source["toolUseId"] == tool_use_id)
                for source in reference.get("sources", [])
            )
        )
    return reference.get("scopeKind") != "account" and reference.get("regionId") == region


def console_inspection(binding: dict[str, Any], source_type: str, identifier: str) -> dict[str, str] | None:
    """Reviewed source-bound read-only UI paths, never arbitrary selectors or writes."""
    kind = binding.get("console_inspection")
    if kind is None:
        return None
    markers = binding.get("render_text", [])
    if kind == "rocketmq_instance_notice":
        if (source_type != "ALIYUN::ROCKETMQ::Instance" or binding.get("mode") != "open"
                or binding.get("dimension") not in {"ROS", "QUERY"}
                or not re.fullmatch(r"[0-9]{16}", str(binding.get("account", "")))
                or not re.fullmatch(r"MQ_INST_" + binding["account"] + r"_[A-Za-z0-9]+", identifier)
                or not isinstance(markers, list) or not markers
                or not isinstance(markers[-1], str) or not markers[-1].strip()):
            raise WorkflowError("RocketMQ announcement inspection requires exact native open identity")
        # Runtime already joined the exact current-dimension wire tool above.
        # Never replace that bound inspector with a second unbound UI profile.
        return None
    if kind == "nas_dataflow_metadata":
        if (source_type != "ALIYUN::NAS::DataFlow" or not re.fullmatch(r"df-[a-z0-9]+", identifier)
                or not re.fullmatch(r"cpfs-[a-z0-9]+", str(binding.get("file_system_id", "")))
                or not re.fullmatch(r"fset-[a-z0-9]+", str(binding.get("fileset_id", "")))
                or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
                or not isinstance(markers, list) or len(markers) != 3 or markers[0] != identifier
                or not isinstance(markers[1], str) or not markers[1].startswith("/")
                or not re.fullmatch(r"oss://[a-z0-9][a-z0-9-]{1,62}(?:/[^\s]*)?", str(markers[2]))):
            raise WorkflowError("NAS DataFlow metadata requires exact parent, child, Fileset, path and OSS")
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "file_system_id": binding["file_system_id"], "fileset_id": binding["fileset_id"],
                "region": binding["region"], "path": markers[1], "source_storage": markers[2]}
    if kind == "sae_application_metadata":
        if (
            source_type != "ALIYUN::SAE::Application"
            or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identifier)
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not re.fullmatch(r"[0-9]{16}", str(binding.get("account", "")))
            or not isinstance(markers, list) or len(markers) != 2
            or any(not isinstance(item, str) or not item.strip() for item in markers)
        ):
            raise WorkflowError("SAE metadata requires own AppId, name, description, account and region")
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "region": binding["region"], "account": binding["account"],
                "name": markers[0], "description": markers[1]}
    if kind == "nas_mount_target_address":
        parent = binding.get("file_system_id", "")
        if (
            source_type not in {"ALIYUN::NAS::MountTarget", "alicloud_nas_mount_target"}
            or not isinstance(parent, str) or not re.fullmatch(r"[a-z0-9]+", parent)
            or binding.get("region") != "cn-hangzhou"
            or not re.fullmatch(r"[0-9]{16}", str(binding.get("account", "")))
            or not re.fullmatch(r"[a-z0-9-]+\.cn-hangzhou\.nas\.aliyuncs\.com", identifier)
            or not identifier.startswith(parent + "-")
            or not isinstance(markers, list) or len(markers) != 2 or markers[0] != identifier
            or not re.fullmatch(r"vpc-[a-z0-9]+", str(markers[1]))
        ):
            raise WorkflowError("NAS mount address inspection requires own parent, domain, VPC, account and region")
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "file_system_id": parent, "region": binding["region"], "account": binding["account"],
                "vpc_id": markers[1]}
    if kind == "sls_project_metadata":
        if (
            source_type not in {"ALIYUN::SLS::Project", "alicloud_log_project"}
            or not re.fullmatch(r"[a-z][a-z0-9-]{2,62}", identifier)
            or binding.get("region") != "cn-hangzhou"
            or not re.fullmatch(r"[0-9]{16}", str(binding.get("account", "")))
            or not isinstance(markers, list) or len(markers) != 2 or markers[0] != identifier
            or not isinstance(markers[1], str) or not markers[1].strip() or markers[1] == identifier
        ):
            raise WorkflowError(
                "SLS project inspection requires own full name, description, account and reviewed region"
            )
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "region": binding["region"], "account": binding["account"], "description": markers[1]}
    if kind == "ots_vpc_binding_row":
        if (
            source_type != "ALIYUN::OTS::VpcBinder"
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", identifier)
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", str(binding.get("instance_name", "")))
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not isinstance(markers, list) or len(markers) != 2 or markers[0] != identifier
            or not re.fullmatch(r"vpc-[a-z0-9]+", str(markers[1]))
        ):
            raise WorkflowError("OTS VPC binding inspection requires own parent, binding name, VPC ID and region")
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "instance_name": binding["instance_name"], "region": binding["region"], "vpc_id": markers[1]}
    if kind == "nas_protocol_service_identity":
        if (
            source_type != "ALIYUN::NAS::ProtocolService"
            or not re.fullmatch(r"ptc-[a-z0-9]+", identifier)
            or not re.fullmatch(r"cpfs-[a-z0-9]+", str(binding.get("file_system_id", "")))
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not isinstance(markers, list) or len(markers) != 2 or markers[1] != identifier
            or not isinstance(markers[0], str) or not markers[0].strip() or markers[0] == identifier
        ):
            raise WorkflowError("NAS ProtocolService inspection requires own exact parent, description, ID and region")
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "file_system_id": binding["file_system_id"], "region": binding["region"],
                "description": markers[0]}
    if kind == "nas_fileset_description":
        if (
            source_type != "ALIYUN::NAS::Fileset"
            or not re.fullmatch(r"fset-[a-z0-9]+", identifier)
            or not re.fullmatch(r"cpfs-[a-z0-9]+", str(binding.get("file_system_id", "")))
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not isinstance(markers, list) or len(markers) != 2
            or not isinstance(markers[0], str) or not markers[0].strip()
            or not isinstance(markers[1], str) or not markers[1].startswith("/")
        ):
            raise WorkflowError("NAS Fileset inspection requires own exact parent, description, path and region")
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "file_system_id": binding["file_system_id"], "region": binding["region"],
                "description": markers[0], "path": markers[1]}
    if kind == "ots_instance_endpoint":
        if (
            source_type != "ALIYUN::OTS::Instance"
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", identifier)
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not isinstance(markers, list) or len(markers) != 2 or markers[0] != identifier
            or markers[1] != f"https://{identifier}.{binding['region']}.ots.aliyuncs.com"
        ):
            raise WorkflowError("OTS endpoint inspection requires own ROS instance, region and full public endpoint")
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "region": binding["region"], "public_endpoint": markers[1]}
    if kind == "nas_protocol_mount_target_child":
        if (
            source_type != "ALIYUN::NAS::ProtocolMountTarget"
            or not re.fullmatch(r"cpfs-[a-z0-9-]+\.cn-[a-z0-9-]+\.cpfs\.aliyuncs\.com", identifier)
            or not re.fullmatch(r"cpfs-[a-z0-9]+", str(binding.get("file_system_id", "")))
            or not re.fullmatch(r"ptc-[a-z0-9]+", str(binding.get("protocol_service_id", "")))
            or not re.fullmatch(r"exp-[a-z0-9]+", str(binding.get("export_id", "")))
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not isinstance(markers, list) or len(markers) != 2
            or not isinstance(markers[0], str) or not markers[0].strip()
            or not isinstance(markers[1], str) or not re.fullmatch(r"/[A-Za-z0-9/_-]+/", markers[1])
        ):
            raise WorkflowError("NAS export inspection requires its exact parent, export, domain, name and path")
        return {"kind": kind, "resource_type": source_type, "identifier": identifier,
                "file_system_id": binding["file_system_id"], "protocol_service_id": binding["protocol_service_id"],
                "export_id": binding["export_id"], "region": binding["region"], "name": markers[0], "path": markers[1]}
    if kind == "oos_state_configuration_list":
        fields = (binding.get("rationale") or {}).get("signed_source_required_fields", {})
        if (
            source_type != "ALIYUN::OOS::StateConfiguration"
            or not re.fullmatch(r"sc-[a-z0-9]+", identifier)
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not re.fullmatch(r"[0-9]{16}", str(binding.get("account", "")))
            or markers != [identifier]
            or not isinstance(fields, dict) or fields.get("StateConfigurationId") != identifier
            or any(not isinstance(fields.get(key), str) or not fields[key].strip()
                   for key in ("Description", "TemplateName"))
        ):
            raise WorkflowError("OOS state list requires own full ID, signed fields, account and domestic region")
        return {"kind": kind, "identifier": identifier, "resource_type": source_type,
                "region": binding["region"], "account": binding["account"],
                "description": fields["Description"], "template_name": fields["TemplateName"]}
    if kind == "oos_template_metadata":
        if (
            source_type != "ALIYUN::OOS::Template"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identifier)
            or binding.get("name_prefix") not in (None, "", identifier)
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not isinstance(markers, list) or len(markers) != 2 or markers[0] != identifier
            or not isinstance(markers[1], str) or not markers[1].strip() or markers[1] == identifier
        ):
            raise WorkflowError("OOS template inspection must bind exact name, description, canonical type and region")
        return {"kind": kind, "identifier": identifier, "resource_type": source_type,
                "description": markers[1], "region": binding["region"]}
    if kind == "ess_scheduled_task_metadata":
        if (
            source_type not in {"ALIYUN::ESS::ScheduledTask", "alicloud_ess_scheduled_task"}
            or not re.fullmatch(r"[A-Za-z0-9]{1,128}", identifier)
            or binding.get("name_prefix") not in (None, "")
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not isinstance(markers, list) or len(markers) != 2
            or not isinstance(markers[0], str) or not markers[0].strip() or markers[0] == identifier
            or not isinstance(markers[1], str) or not re.fullmatch(r"asg-[a-z0-9]+", markers[1])
        ):
            raise WorkflowError("ESS task inspection must bind exact task ID, name, full group ID, type and region")
        return {"kind": kind, "identifier": identifier, "resource_type": source_type,
                "name": markers[0], "scaling_group_id": markers[1], "region": binding["region"]}
    if kind == "router_interface_vbr_child":
        if (
            source_type not in {"ALIYUN::VPC::RouterInterface", "alicloud_router_interface"}
            or not re.fullmatch(r"ri-[a-z0-9]+", identifier)
            or binding.get("name_prefix") not in (None, "", "ri-")
            or not re.fullmatch(r"vbr-[a-z0-9]+", str(binding.get("parent_router_id", "")))
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(binding.get("region", "")))
            or not isinstance(markers, list) or len(markers) != 2
            or not isinstance(markers[0], str) or not markers[0].strip() or markers[0] == identifier
            or not isinstance(markers[1], str) or not re.fullmatch(r"vpc-[a-z0-9]+", markers[1])
        ):
            raise WorkflowError("RouterInterface inspection must bind exact child, own VBR, name, peer VPC and region")
        return {"kind": kind, "identifier": identifier, "resource_type": source_type,
                "parent_router_id": binding["parent_router_id"], "name": markers[0], "peer_vpc": markers[1],
                "region": binding["region"]}
    if kind == "resource_group_metadata":
        if (
            source_type not in {"ALIYUN::ResourceManager::ResourceGroup", "alicloud_resource_manager_resource_group"}
            or not re.fullmatch(r"rg-[a-z0-9]+", identifier)
            or binding.get("name_prefix") not in (None, "", "rg-")
            or binding.get("region") != "global"
            or not isinstance(binding.get("api_region"), str)
            or not re.fullmatch(r"cn-[a-z0-9-]+", binding["api_region"])
            or not isinstance(markers, list) or len(markers) != 2
            or not isinstance(markers[0], str) or not re.fullmatch(r"[a-z][a-z0-9]{2,11}", markers[0])
            or not isinstance(markers[1], str) or not markers[1].strip() or markers[1] == identifier
        ):
            raise WorkflowError("ResourceGroup metadata inspection must bind exact ID, Name, DisplayName and scope")
        return {"kind": kind, "identifier": identifier, "name": markers[0], "display_name": markers[1],
                "region": "global", "api_region": binding["api_region"]}
    if kind == "ecs_keypair_metadata":
        if (
            source_type not in {"ALIYUN::ECS::SSHKeyPair", "alicloud_ecs_key_pair"}
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,127}", identifier)
            or binding.get("name_prefix") not in (None, "", identifier)
            or not isinstance(markers, list) or len(markers) != 2 or markers[0] != identifier
            or not isinstance(markers[1], str) or not re.fullmatch(r"[a-fA-F0-9]{32}", markers[1])
            or not isinstance(binding.get("region"), str)
            or not re.fullmatch(r"cn-[a-z0-9-]+", binding.get("region", ""))
        ):
            raise WorkflowError(
                "ECS KeyPair metadata inspection must bind exact approved name, full fingerprint, type and region"
            )
        return {"kind": kind, "identifier": identifier, "fingerprint": markers[1], "region": binding["region"]}
    if kind == "slb_acl_metadata":
        if (
            source_type not in {"ALIYUN::SLB::AccessControl", "alicloud_slb_acl"}
            or not re.fullmatch(r"acl-[a-z0-9]+", identifier)
            or binding.get("name_prefix") not in (None, "")
            or not isinstance(markers, list) or len(markers) != 2
            or not isinstance(markers[0], str) or not markers[0].strip() or markers[0] == identifier
            or not isinstance(markers[1], str) or markers[1] not in {"IPv4", "IPv6"}
            or not isinstance(binding.get("region"), str)
            or not re.fullmatch(r"cn-[a-z0-9-]+", binding.get("region", ""))
        ):
            raise WorkflowError(
                "SLB metadata inspection must bind exact approved ID, name, IP version, type and region"
            )
        return {"kind": kind, "identifier": identifier, "name": markers[0],
                "address_family": markers[1], "region": binding["region"]}
    if (
        kind != "nas_access_group_metadata"
        or source_type not in {"ALIYUN::NAS::AccessGroup", "alicloud_nas_access_group"}
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,63}", identifier)
        or binding.get("name_prefix") != identifier
        or len(markers) != 2 or markers[0] != identifier
        or not isinstance(markers[1], str) or not markers[1].strip() or markers[1] == identifier
        or not re.fullmatch(r"cn-[a-z0-9-]+", binding.get("region", ""))
    ):
        raise WorkflowError("NAS metadata inspection must bind exact approved name, description, type and region")
    return {"kind": kind, "identifier": identifier, "description": markers[1], "region": binding["region"]}


def bind_oos_template_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Bind native disclosure to one own successful ListTemplates row, not invented attributes."""
    matches = []
    for reference in references:
        attributes = reference.get("attributes") or {}
        if (
            reference.get("resourceType") != inspection["resource_type"]
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or attributes.get("TemplateName") != inspection["identifier"]
            or not isinstance(attributes.get("TemplateId"), str) or not attributes["TemplateId"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("product") != "oos" or source.get("action") != "ListTemplates"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "oos" or request.get("action") != "ListTemplates"
                or request.get("version") != "2019-06-01" or request.get("region_id") != inspection["region"]
                or request.get("params", {}).get("RegionId") != inspection["region"]
                or request.get("params", {}).get("ShareType") != "Private"
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    rows = json.loads(result["content"])["Templates"]
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict) and all(row.get(key) == value for key, value in {
                        "TemplateName": inspection["identifier"], "TemplateId": attributes["TemplateId"],
                        "Description": inspection["description"],
                    }.items()):
                        matches.append((tool_id, attributes["TemplateId"]))
    if len(matches) != 1:
        raise WorkflowError("OOS template fields differ from one own signed product source row")
    return {**inspection, "source_tool_use_id": matches[0][0], "template_id": matches[0][1]}


def bind_ess_scheduled_task_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Require all three ESS fields in one original successful signed API row.

    References passed here have already been proven against this job's wire
    responses. ScalingGroupId need not be manufactured as a locator attribute.
    """
    matches = []
    for reference in references:
        if (
            reference.get("resourceType") != inspection["resource_type"]
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or (reference.get("attributes") or {}).get("ScheduledTaskName") != inspection["name"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("product") != "ess" or source.get("action") != "DescribeScheduledTasks"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "ess" or request.get("action") != "DescribeScheduledTasks"
                or request.get("version") != "2014-08-28"
                or request.get("region_id") != inspection["region"]
                or request.get("params", {}).get("RegionId") != inspection["region"]
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    rows = json.loads(result["content"])["ScheduledTasks"]["ScheduledTask"]
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict) and all(row.get(key) == value for key, value in {
                        "ScheduledTaskId": inspection["identifier"], "ScheduledTaskName": inspection["name"],
                        "ScalingGroupId": inspection["scaling_group_id"],
                    }.items()):
                        matches.append(tool_id)
    if len(matches) != 1:
        raise WorkflowError("ESS task fields differ from one own signed product source row")
    return {**inspection, "source_tool_use_id": matches[0]}


def bind_nas_export_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Join a single signed API export row, never a sibling or model-authored field."""
    matches = []
    for reference in references:
        if (reference.get("resourceType") != inspection["resource_type"]
                or reference.get("identifier") != inspection["identifier"]
                or reference.get("regionId") != inspection["region"]
                or any((reference.get("attributes") or {}).get(key) != inspection[field] for key, field in {
                    "FileSystemId": "file_system_id", "ProtocolServiceId": "protocol_service_id",
                    "ExportId": "export_id",
                }.items())):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (not tool_id or source.get("evidence") != "api_response"
                    or source.get("product") != "nas" or source.get("action") != "DescribeProtocolMountTarget"
                    or source.get("requestRegionId") != inspection["region"]
                    or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                    or request.get("product") != "nas" or request.get("action") != "DescribeProtocolMountTarget"
                    or request.get("version") != "2017-06-26" or request.get("region_id") != inspection["region"]
                    or request.get("params", {}).get("FileSystemId") != inspection["file_system_id"]):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    rows = json.loads(result["content"])["ProtocolMountTargets"]
                except (KeyError, TypeError, ValueError):
                    continue
                if isinstance(rows, list):
                    matches.extend(tool_id for row in rows if isinstance(row, dict) and all(
                        row.get(key) == inspection[field] for key, field in {
                            "ProtocolServiceId": "protocol_service_id", "ExportId": "export_id",
                            "ProtocolMountTargetDomain": "identifier", "Description": "name", "Path": "path",
                        }.items()
                    ))
    if len(matches) != 1:
        raise WorkflowError("NAS export fields differ from one own signed original source row")
    return {**inspection, "source_tool_use_id": matches[0]}


def bind_router_interface_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Require the child, own parent and render markers in one proven original API row.

    Callers pass only references already proven against this job's successful
    signed wire observations. Do not infer a parent from an opposite router or
    manufacture Name/peer attributes absent from the product's actual locator.
    """
    matches = []
    for reference in references:
        if (
            reference.get("resourceType") != inspection["resource_type"]
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or (reference.get("attributes") or {}).get("RouterType") != "VBR"
            or (reference.get("attributes") or {}).get("RouterId") != inspection["parent_router_id"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("product") != "vpc" or source.get("action") != "DescribeRouterInterfaces"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "vpc" or request.get("action") != "DescribeRouterInterfaces"
                or request.get("region_id") != inspection["region"]
                or request.get("params", {}).get("RegionId") != inspection["region"]
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    value = json.loads(result["content"])
                    rows = value["RouterInterfaceSet"]["RouterInterfaceType"]
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict) and all(row.get(key) == value for key, value in {
                        "RouterInterfaceId": inspection["identifier"], "RouterType": "VBR",
                        "RouterId": inspection["parent_router_id"], "Name": inspection["name"],
                        "OppositeVpcInstanceId": inspection["peer_vpc"],
                    }.items()):
                        matches.append(tool_id)
    if len(matches) != 1:
        raise WorkflowError("RouterInterface child fields differ from one own signed product source row")
    return {**inspection, "source_tool_use_id": matches[0]}


def navigation_identifier(reference: dict[str, Any], tools: dict[str, Any] | None = None) -> str:
    """Project only a proven locator; never replace its source/ownership identity.

    Callers must first bind the reference to this signed session and creation.
    NAS Terraform physical IDs can be composite; reviewed console locators use
    the proven mount domain or bare access-group name. OOS SecretParameter uses its
    exact signed response row's Name, while retaining its primary p-* source ID.
    Other locators remain unchanged; there is no generic alias fallback.
    """
    identifier = reference.get("identifier")
    if not isinstance(identifier, str) or not identifier:
        raise WorkflowError("precise reference identifier required")
    if reference.get("resourceType") == "ALIYUN::OOS::SecretParameter":
        from .console_sources import oos_secret_parameter_navigation_name

        return oos_secret_parameter_navigation_name(reference, tools or {})
    if (reference.get("resourceType") == "alicloud_nas_mount_target"
            and (":" in identifier or reference.get("identifierField") == "PhysicalResourceId")):
        return console_sources.managed_nas_mount_identity(reference, tools or {})[1]
    if reference.get("resourceType") != "alicloud_nas_access_group":
        return identifier
    parts = identifier.split(":")
    attributes = reference.get("attributes") or {}
    if (
        reference.get("identifierField") != "PhysicalResourceId"
        or reference.get("evidence") != "api_response"
        or not reference.get("accountScope") or not reference.get("regionId")
        or not reference.get("stackId")
        or not any(
            source.get("product") == "ros"
            and source.get("action") in {"ListStackResources", "GetStackResource"}
            and source.get("evidence") == "api_response" and source.get("toolUseId")
            for source in reference.get("sources", [])
        )
        or len(parts) != 2 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,63}", parts[0])
        or parts[1] not in {"standard", "extreme", "cpfs"}
        or attributes.get("AccessGroupType") not in {"Vpc", "Classic"}
        or attributes.get("AccessGroupName", parts[0]) != parts[0]
        or attributes.get("FileSystemType", parts[1]) != parts[1]
        or any(
            alias.get("identifierField") == "AccessGroupName" and alias.get("identifier") != parts[0]
            for alias in reference.get("aliases", [])
        )
    ):
        raise WorkflowError("precise NAS composite reference context is missing or conflicting")
    return parts[0]


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except (BlockingIOError, PermissionError) as error:
        raise WorkflowError("operation already owned; do not start another Server") from error
    finally:
        handle.close()


class Runtime:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.root
        config = self.root / "environment.json"
        self.config = read_json(config) if config.exists() else {}

    def queue_navigation(self, job_id: str, evidence: str) -> dict[str, Any]:
        proof = read_json(contained(self.root, evidence))
        slot = next((slot for slot in self.store.status()["slots"]
                     if slot["target"] == proof.get("target") and slot["origin"] == proof.get("origin")), None)
        if not slot:
            raise WorkflowError("original navigation slot unavailable")
        self.preflight(slot)
        snapshot = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
        if snapshot.get("busy") or snapshot.get("session_id") != proof.get("session_id"):
            raise WorkflowError("original navigation full session must remain idle and active")
        return self.store.queue_navigation(job_id, evidence)

    def inspect_cleanup(self, job_id: str, dimension: str) -> dict[str, Any]:
        """OPS-only read; retain original task/slot/turn and self-created UI session."""
        if not re.fullmatch(r"[a-f0-9]{32}", job_id) or dimension not in {"ROS", "TF", "CREATE"}:
            raise WorkflowError("exact original GLM job and creation dimension required")
        self.attest_control()
        with self.store.transaction() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["role"] != "glm" or not job["thread_id"] or not job["turn_id"]:
                raise WorkflowError("original persisted GLM thread/turn binding required")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE task_id=? AND state!='complete'", (job["task_id"],)
            ).fetchone():
                raise WorkflowError("recovery inspection requires same task model jobs terminal")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
            slot = connection.execute("SELECT * FROM slots WHERE id=?", (task["slot"],)).fetchone()
            if not slot or slot["task_id"] != task["id"] or task["state"] == "complete":
                raise WorkflowError("original task must retain its owned execution slot for cleanup recovery")
        packet = read_json(contained(self.root, task["packet"]))
        session_path = self.root / "jobs" / job_id / "output/self-created-session.json"
        binding = read_json(session_path)
        if any(binding.get(key) != value for key, value in (
            ("job_id", job_id), ("origin", slot["origin"]), ("target", slot["target"])
        )):
            raise WorkflowError("original self-created session target binding required")
        self.assert_process(slot["web_pid"], slot["web_identity"])
        current = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
        if current.get("busy") or not current.get("unlocked") or current.get("session_id") != binding["session_id"]:
            raise WorkflowError("original exact Web session must be unlocked and idle; do not replace it")
        return cleanup_recovery.inspect_candidate(
            self.root / "execution-gate.sqlite3", slot=str(slot["id"]), scope=f"{job_id}:{dimension}",
            session=binding["session_id"], account=next(iter(packet["scenes"].values()))["account"],
            grants=packet["execution_grants"][dimension],
            authority=creation.execution_authority(self.root, packet, dimension),
        )

    def inspect_native_identity_recovery(self, output: str) -> dict[str, Any]:
        """Create a read-only OPS proposal for the exact retained R0036 session."""
        from . import native_identity_recovery

        return native_identity_recovery.write_proposal(self, output)

    def execute_native_identity_recovery(self, evidence: str) -> dict[str, Any]:
        """Submit only the reviewed one-shot STS identity continuation to the original Web session."""
        from . import native_identity_recovery

        return native_identity_recovery.execute(self, evidence)

    def authorize_creation(self, proposal: Path, evidence: str, max_budget_cents: int) -> dict[str, Any]:
        """OPS only, after reviewing user scope, official cost and exact creation/deletion calls."""
        from .creation import (
            authorization_path,
            execution_digest,
            reviewed_authorities,
            validate_execution,
            validate_stack_timeouts,
        )

        packet = read_json(contained(self.root, str(proposal)))
        proof = contained(self.root, evidence)
        if not proof.is_file():
            raise WorkflowError("save original authorization and OPS exact-operation review before authorization")
        review = read_json(proof)
        with self.store.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (packet["task_id"],)).fetchone()
            hourly_budget.operation_review(self.root, connection, task, packet, review)
            readonly = self.store.readonly_dimensions(connection, task, packet, review=review) if task else set()
            sample_authorities = temporary_samples.admitted(
                self.root, connection, task, packet, review=review,
            ) if task else {}
        if not task or type(max_budget_cents) is not int or not 0 <= packet["budget_cents"] <= max_budget_cents:
            raise WorkflowError("unknown task or unauthorized cost cap")
        from .selection import selected_scenes, validate_review

        resource = json.loads(task["spec"])
        needed = {scene["dimension"] for scene in selected_scenes(self.root, packet, resource)}
        validate_execution(
            packet, needed, require_read_policy=resource.get("execution_policy_version", 1) == 2,
            readonly_dimensions=readonly, native_nas_query_sample="QUERY" in sample_authorities,
        )
        validate_stack_timeouts(packet)
        validate_review(self.root, packet, review)
        if "execution_selection" in packet:
            additional_source.validate_paid_operation_review(self.root, packet, review)
        if "execution_selection" in packet and review["proposal_sha256"] != digest(
            contained(self.root, str(proposal))
        ):
            raise WorkflowError("selection review must bind the full final proposal")
        additional_review = any(key in packet for key in ("additional_source_request", "additional_source_requests"))
        identity_review = any(source.get(key) is not None
                              for source in review.get("carry_forward_dimensions", {}).values()
                              for key in ("identity_continuation", "read_continuation"))
        if (additional_review or identity_review) and review.get("proposal_sha256") != digest(
            contained(self.root, str(proposal))
        ):
            raise WorkflowError("additional source review must bind the final exact proposal SHA")
        if "hourly_cost" in packet and review.get("proposal_sha256") != digest(contained(self.root, str(proposal))):
            raise WorkflowError("hourly operation review must bind the final full proposal SHA")
        account = next(iter(packet["scenes"].values()))["account"]
        value = {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": [task["resource_id"]],
            "task_id": task["id"],
            "account": account,
            "max_budget_cents": max_budget_cents,
            "execution_sha256": execution_digest(packet),
            "authorization_evidence": evidence,
            "authorization_sha256": digest(proof),
            "lifetime": "task",
            "expires_at": None,
            "dimension_authorities": {**reviewed_authorities(self.root, packet, review), **sample_authorities},
        }
        if "execution_selection" in packet or identity_review or "hourly_cost" in packet:
            value["proposal_evidence"] = contained(self.root, str(proposal)).relative_to(self.root).as_posix()
            value["proposal_sha256"] = digest(contained(self.root, str(proposal)))
        if "additional_source_request" in packet:
            value["additional_source_request"] = packet["additional_source_request"]
        if "additional_source_requests" in packet:
            value["additional_source_requests"] = packet["additional_source_requests"]
        json_write(authorization_path(self.root, packet), value, private=True)
        return {
            "resource_id": task["resource_id"],
            "identity_mode": "existing_account",
            "execution_reviewed": True,
            "cloud_write_performed": False,
        }

    def authorize_cleanup_identity(self, job_id: str, dimension: str, evidence: str) -> dict[str, Any]:
        status = self.store.status()
        if not status["paused"] or status["jobs"]:
            raise WorkflowError("OPS cleanup identity review requires maintenance pause and drained models")
        candidate = self.inspect_cleanup(job_id, dimension)
        return cleanup_recovery.authorize_identity(self.root, candidate, evidence)

    def queue_cleanup(self, job_id: str, dimension: str, evidence: str, retry_job: str | None = None,
                      read_only: bool = False) -> dict[str, Any]:
        candidate = self.inspect_cleanup(job_id, dimension)
        proof_path = contained(self.root, evidence)
        proof = read_json(proof_path)
        delete_calls = [x for x in candidate["remaining_calls"] if x["kind"] == "delete"]
        if not read_only and (len(delete_calls) != 1 or delete_calls[0]["remaining"] != 1):
            raise WorkflowError("original unused deletion allowance required; never repeat a used or unknown delete")
        required = {"original_job_id": job_id, "dimension": dimension, "scope": candidate["scope"],
                    "authority": candidate["authority"], "account": candidate["account"],
                    "owned_from_wire": candidate["owned_from_wire"]}
        if read_only:
            required.update(cleanup_recovery.readonly_review_fields(candidate))
            required["session_id"] = candidate["session_id"]
        else:
            required["delete_call"] = delete_calls[0]["call"]
        kind = "ops_cleanup_readonly_review" if read_only else "ops_cleanup_only_review"
        if proof.get("kind") != kind or proof.get("approved") is not True or any(
            proof.get(k) != v for k, v in required.items()
        ):
            raise WorkflowError("OPS exact original ownership and deletion review required")
        with self.store.transaction() as connection:
            task_id = connection.execute("SELECT task_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        recovery = {"original_job_id": job_id, "dimension": dimension, "evidence": evidence,
                    "evidence_sha256": digest(proof_path), "authority": candidate["authority"]}
        if read_only:
            recovery["read_only"] = True
        if retry_job is not None:
            if proof.get("retry_job_id") != retry_job or not re.fullmatch(r"[a-f0-9]{32}", retry_job):
                raise WorkflowError("fresh OPS review must bind the exact terminal cleanup job")
            raw_path = contained(self.root, proof.get("retry_terminal_evidence", ""))
            if not raw_path.is_file() or digest(raw_path) != proof.get("retry_terminal_sha256"):
                raise WorkflowError("original cleanup terminal evidence changed")
            raw = read_json(raw_path)
            with self.store.transaction() as connection:
                retry = connection.execute("SELECT * FROM jobs WHERE id=?", (retry_job,)).fetchone()
            if not retry or retry["task_id"] != task_id or retry["purpose"] != "cleanup":
                raise WorkflowError("retry must reference this task original cleanup job")
            exact = [t for t in raw.get("thread", {}).get("turns", []) if t.get("id") == retry["turn_id"]]
            if (raw.get("thread", {}).get("id") != retry["thread_id"] or len(exact) != 1
                    or exact[0].get("status") not in {"completed", "failed", "interrupted"}):
                raise WorkflowError("exact owning-backend cleanup terminal response required")
            recovery["retry_job_id"] = retry_job
        self.store.queue_cleanup(task_id, recovery)
        return {"cleanup_queued": task_id, "only_luna_dispatches": True, "cloud_call_performed": False,
                "pass_count_unchanged": True, "original_counters_preserved": True}

    def assert_previous_cleanup(self, job_id: str, dimension: str, *, output_dir: Path | None = None) -> None:
        """A successful DeleteStack response is not a terminal disappearance proof."""
        import sqlite3

        from .phases import (
            assert_cleanup_reads_complete,
            known_original_rejected_oss_detail_read,
            known_original_resolved_read,
            original_stack_read_body,
        )

        path = self.root / "execution-gate.sqlite3"
        if not path.exists():
            return
        output = output_dir if output_dir is not None else self.root / "jobs" / job_id / "output"
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            scopes = connection.execute(
                "SELECT scope FROM grants WHERE scope LIKE ? UNION SELECT scope FROM attempts WHERE scope LIKE ?",
                (job_id + ":%", job_id + ":%"),
            )
            for row in scopes.fetchall():
                scope = row["scope"]
                if scope == f"{job_id}:{dimension}":
                    continue
                attempts = connection.execute(
                    "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param "
                    "FROM attempts a LEFT JOIN grants g "
                    "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? ORDER BY a.created", (scope,)
                ).fetchall()
                if any(x["kind"] is None for x in attempts):
                    raise WorkflowError("previous dimension has an attempt without its original reviewed grant")
                created = [x for x in attempts if x["kind"] == "create"]
                if not created:
                    continue
                if any(
                    x["state"] != "succeeded"
                    and not known_original_resolved_read(connection, x)
                    and not known_original_rejected_oss_detail_read(connection, x)
                    for x in attempts
                ) or not any(
                    x["kind"] == "delete" and x["state"] == "succeeded" for x in attempts
                ):
                    raise WorkflowError(
                        "previous dimension has unresolved creation/deletion; cleanup before new request"
                    )
                previous = scope.rsplit(":", 1)[-1]
                if not (output / f"{previous}-cleanup-source.json").is_file():
                    raise WorkflowError("save actual previous cleanup source before activating another dimension")
                for create in created:
                    if json.loads(create["call"])["action"] != "CreateStack":
                        continue
                    statuses = [
                        original_stack_read_body(connection, x, create, require_active_identity=False)
                        for x in attempts
                        if x["kind"] == "read" and json.loads(x["call"])["action"] == "GetStack"
                        and x["created"] > max(a["created"] for a in attempts if a["kind"] == "delete")
                    ]
                    if (not statuses or not statuses[-1] or statuses[-1].get("Status") != "DELETE_COMPLETE"
                            or statuses[-1].get("StackId") != json.loads(create["response"]).get("StackId")):
                        raise WorkflowError("previous ROS/managed Terraform stack is not proven DELETE_COMPLETE")
                assert_cleanup_reads_complete(path, scope)

    def dimension_transition_admission(self, job_id: str, packet: dict[str, Any]) -> dict[str, Any]:
        """Expose the existing request gate, without claiming cleanup acceptance.

        The actual request revalidates the journal. This read-only observation
        cannot install capabilities, unfreeze sources, or accept PendingDelete.
        """
        output = self.root / "jobs" / job_id / "output"
        candidates = {}
        for dimension in packet["requests"]:
            if (output / f"{dimension}-request-intent.json").exists():
                continue
            try:
                self.assert_previous_cleanup(job_id, dimension)
            except WorkflowError as error:
                candidates[dimension] = {"previous_cleanup_execution_complete": False, "blocker": str(error)}
            else:
                candidates[dimension] = {"previous_cleanup_execution_complete": True, "blocker": None}
        return {
            "authority": "same read-only journal checks as request; revalidated at actual request",
            "remaining_dimensions": candidates,
            "physical_absence_verified": False,
            "cleanup_accepted": False,
            "independent_ops_sol_review_required": True,
            "no_call_or_quota_added": True,
            "immutable_cleanup_source_must_not_be_reopened": True,
        }

    @staticmethod
    def require_free_port(port: int) -> None:
        with socket.socket() as probe:
            # Match the Web listener's restart semantics: TIME_WAIT is not a
            # live conflicting server. A live listener still rejects this bind.
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                probe.listen(1)
            except OSError as error:
                raise WorkflowError(
                    "reserved port is occupied; adopt exact owned process or resolve under OPS"
                ) from error

    def preflight(self, slot: dict[str, Any]) -> None:
        marker = Path(self.config["repo"]) / "evidence" / "resource-link-workflow-owner.json"
        if not marker.exists() or read_json(marker)["state"] != str(self.root):
            raise WorkflowError("claim repository dispatch ownership before GLM launch")
        self.assert_process(slot["web_pid"], slot["web_identity"])
        marker = self.root / "slots" / str(slot["id"]) / "execution-gate-process.json"
        if not marker.is_file() or read_json(marker).get("pid") != slot["web_pid"]:
            raise WorkflowError("restart this same Web slot with the program execution gate before dispatch")
        # Reject a stale product interpreter before reserving a GLM turn, not
        # only when the worker asks for its first context after launch.
        self.attest_web(slot["id"])
        remote = worker_hosts.host(self.config) == "macmini"
        chrome = read_json(self.root / ("chrome-macmini.json" if remote else "chrome.json"))
        if remote:
            worker_hosts.require_tunnel(self)
            worker_hosts.remote_chrome(self.config, {**chrome, "operation": "status"})
        else:
            self.assert_process(chrome["pid"], chrome["identity"])
        state = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
        if not state.get("app_ready") or not state["unlocked"]:
            raise WorkflowError("slot Web remains locked")
        result = subprocess.run(
            [self.config.get("macmini_cli", "codex-alilang-macmini") if remote else
             self.config.get("bailian_cli", "codex-bailian-control"), "--json", "status"],
            capture_output=True,
            encoding="utf-8",
            timeout=15,
            check=True,
        )
        controller = json.loads(result.stdout)
        if not controller.get("ready") or not controller.get("connected"):
            raise WorkflowError("selected GLM controller is not ready/connected")

    def provision(self, source: Path) -> dict[str, Any]:
        if any(slot["web_pid"] for slot in self.store.status()["slots"]):
            raise WorkflowError("stop owned Web processes before provisioning private configs")
        source = source.resolve()
        copied = []
        for slot in self.store.status()["slots"]:
            target = self.root / "slots" / str(slot["id"]) / "config"
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, 0o700)
            for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
                file = source / name
                if file.is_file():
                    atomic_write(target / name, file.read_bytes(), private=True)
            copied.append(slot["id"])
        return {"provisioned_slots": copied, "sessions_and_history_not_copied": True}

    def start_chrome(self) -> dict[str, Any]:
        worker_hosts.assert_selection(self)
        if worker_hosts.host(self.config) == "macmini":
            worker_hosts.require_tunnel(self)
            with exclusive(self.root / "chrome-ops.lock"):
                marker = self.root / "chrome-macmini.json"
                old = read_json(marker) if marker.exists() else {}
                value = worker_hosts.mini(self.config)
                result = worker_hosts.remote_chrome(self.config, {
                    **old, "operation": "start", "profile": value["chrome_profile"],
                    "executable": value["chrome_executable"],
                })
                json_write(marker, result, private=True)
                return {"pid": result["pid"], "worker_host": "macmini", "ready": False,
                        "cdp_endpoint": "http://127.0.0.1:9226", "preflight_required": True}
        marker = self.root / "chrome.json"
        with exclusive(self.root / "chrome-ops.lock"):
            if marker.exists():
                old = read_json(marker)
                try:
                    self.assert_process(old["pid"], old["identity"])
                    return {"already_running": True, "pid": old["pid"]}
                except subprocess.CalledProcessError:
                    pass
            profile = self.root / "chrome-profile"
            if marker.exists():
                profile = Path(read_json(marker)["profile"])
            profile.mkdir(parents=True, exist_ok=True)
            self.require_free_port(9225)
            argv = [
                self.config["chrome_executable"],
                "--remote-debugging-port=9225",
                "--remote-debugging-address=127.0.0.1",
                "--user-data-dir=" + str(profile),
                "--no-first-run",
                "about:blank",
            ]
            with (self.root / "chrome.log").open("ab") as log:
                os.chmod(self.root / "chrome.log", 0o600)
                process = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=os.name != "nt")
            identity = self.process_identity(process.pid)
            json_write(marker, {"pid": process.pid, "identity": identity, "profile": str(profile)}, private=True)
            return {"pid": process.pid, "profile": str(profile), "cdp_endpoint": "http://127.0.0.1:9225"}

    def start_sol_backend(self) -> dict[str, Any]:
        endpoint = self.config.get("sol_endpoint", "ws://127.0.0.1:4502")
        if endpoint != "ws://127.0.0.1:4502":
            raise WorkflowError(
                "managed Sol backend only uses reserved local4502; external endpoints need explicit OPS"
            )
        marker = self.root / "sol-backend.json"
        with exclusive(self.root / "sol-backend.lock"):
            if marker.exists():
                old = read_json(marker)
                try:
                    self.assert_process(old["pid"], old["identity"])
                    return {"already_running": True, "pid": old["pid"]}
                except subprocess.CalledProcessError:
                    pass
            cwd = self.root / "sol-backend"
            cwd.mkdir(exist_ok=True)
            self.require_free_port(4502)
            with (self.root / "sol-backend.log").open("ab") as log:
                os.chmod(self.root / "sol-backend.log", 0o600)
                process = subprocess.Popen(
                    [self.config.get("codex_cli", "codex"), "app-server", "--listen", endpoint],
                    cwd=cwd,
                    stdout=log,
                    stderr=log,
                    start_new_session=os.name != "nt",
                )
            json_write(marker, {"pid": process.pid, "identity": self.process_identity(process.pid)}, private=True)
            return {"pid": process.pid, "endpoint": endpoint, "no_model_turn_started": True}

    def adopt_chrome(self, pid: int, profile: Path) -> dict[str, Any]:
        if any(slot["task_id"] for slot in self.store.status()["slots"]):
            raise WorkflowError("cannot change Chrome owner during an execution lease")
        if worker_hosts.host(self.config) == "macmini":
            worker_hosts.require_tunnel(self)
            if str(profile) != worker_hosts.mini(self.config)["chrome_profile"]:
                raise WorkflowError("remote Chrome adoption must use the configured exact profile")
            result = worker_hosts.remote_chrome(self.config, {
                "operation": "adopt", "pid": pid, "profile": str(profile),
            })
            json_write(self.root / "chrome-macmini.json", result, private=True)
            return {"adopted": pid, "worker_host": "macmini", "login_profile_preserved": True}
        identity = self.process_identity(pid)
        profile = profile.resolve()
        if "--remote-debugging-port=9225" not in identity or "--user-data-dir=" + str(profile) not in identity:
            raise WorkflowError("Chrome PID must prove dedicated9225 and the exact user-provided profile")
        json_write(self.root / "chrome.json", {"pid": pid, "identity": identity, "profile": str(profile)}, private=True)
        return {"adopted": pid, "login_profile_preserved": True}

    def browser(self, request: dict[str, Any]) -> dict[str, Any]:
        worker_hosts.assert_selection(self)
        remote = worker_hosts.host(self.config) == "macmini"
        endpoint = self.config.get("cdp_endpoint", "http://127.0.0.1:9225")
        if endpoint != "http://127.0.0.1:9225":
            raise WorkflowError("dedicated local Chrome9225 required")
        if remote:
            worker_hosts.require_tunnel(self)
            endpoint = "http://127.0.0.1:9226"
        environment = dict(os.environ)
        if self.config.get("playwright_root"):
            environment["RESOURCE_LINK_PLAYWRIGHT_ROOT"] = self.config["playwright_root"]
        # Popup events and UI actions are scoped to the leased page. A global
        # nonblocking lock rejected unrelated slots while one console loaded.
        # Keep same-target operations exclusive without blocking the other three.
        page_key = hashlib.sha256(
            json.dumps([request.get("origin"), request.get("target")], sort_keys=True).encode()
        ).hexdigest()
        with exclusive(self.root / "browser-locks" / f"{page_key}.lock"):
            result = subprocess.run(
                [self.config.get("node", "node"), str(Path(__file__).with_name("browser.mjs"))],
                input=json.dumps(
                    {
                        **request,
                        "cdp_endpoint": endpoint,
                        "remote_cdp": remote,
                        "foreground_lock": str(self.root / "browser-locks" / "foreground.lock"),
                    }
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                timeout=65,
                check=False,
            )
        if result.returncode:
            # Browser exceptions can embed private unlock URLs. Never expose subprocess stderr.
            try:
                safe_error = json.loads(result.stdout).get("workflow_error")
            except (ValueError, AttributeError):
                safe_error = None
            if safe_error in {
                "source session mismatch", "answer still running", "session mismatch",
                "complete active session key mismatch", "precise reference destination ambiguous or pending",
                "bound product occurrence changed",
            }:
                raise WorkflowError(
                    safe_error + "; copy the exact current snapshot session_id; never guess or retry creation"
                )
            raise WorkflowError("scoped browser operation failed; OPS inspect redacted state, not token URLs")
        return json.loads(result.stdout)

    def start_web(self, slot_id: int) -> dict[str, Any]:
        """OPS only; restart does not replace config/session storage or target ownership."""
        repo = Path(self.config["repo"]).resolve()
        slot = next((slot for slot in self.store.status()["slots"] if slot["id"] == slot_id), None)
        if not slot:
            raise WorkflowError("unknown slot")
        base = self.root / "slots" / str(slot_id)
        token = base / "access-token"
        config = base / "config"
        if not config.is_dir():
            raise WorkflowError("provision private isolated slot config before starting Web")
        if not token.exists():
            atomic_write(token, secrets.token_urlsafe(36), private=True)
        if token.stat().st_mode & 0o777 != 0o600:
            raise WorkflowError("token must be 0600")
        with exclusive(base / "ops.lock"):
            if slot["web_pid"]:
                self.assert_process(slot["web_pid"], slot["web_identity"])
                marker = base / "execution-gate-process.json"
                if not marker.is_file() or read_json(marker).get("pid") != slot["web_pid"]:
                    raise WorkflowError("existing Web lacks execution gate; OPS stop/start same slot, preserving state")
                return {"already_running": True, "slot": slot_id}
            from scripts.web.e2e.resource_link_workflow.execution_gate import ENV, SLOT_ENV, initialize

            gate = self.root / "execution-gate.sqlite3"
            initialize(gate)
            environment = dict(os.environ, IAC_CODE_CONFIG_DIR=str(config),
                               PYTHONPATH=os.pathsep.join((str(repo), str(repo / "src"))))
            environment.update({ENV: str(gate), SLOT_ENV: str(slot_id)})
            port = slot["origin"].rsplit(":", 1)[1]
            self.require_free_port(int(port))
            argv = [
                self.config.get("uv", "uv"),
                "run",
                "--project",
                str(repo),
                "python",
                "-m",
                "scripts.web.e2e.resource_link_workflow.web_entry",
                "web",
                "--host",
                "127.0.0.1",
                "--port",
                port,
                "--access-token-file",
                str(token),
                "--no-open",
            ]
            with (base / "web.log").open("ab") as log:
                os.chmod(base / "web.log", 0o600)
                process = subprocess.Popen(
                    argv, cwd=slot["cwd"], env=environment, stdout=log, stderr=log, start_new_session=os.name != "nt"
                )
            identity = self.process_identity(process.pid)
            json_write(base / "execution-gate-process.json", {"pid": process.pid, "version": 1}, private=True)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE slots SET web_pid=?,web_identity=? WHERE id=?", (process.pid, identity, slot_id)
                )
                self.store.event(connection, "web_started", {"slot": slot_id, "pid": process.pid})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    self.attest_web(slot_id)
                    with urllib.request.urlopen(slot["origin"], timeout=1) as response:
                        if response.status == 200:
                            return {"slot": slot_id, "pid": process.pid, "origin": slot["origin"]}
                except (OSError, ValueError, subprocess.SubprocessError):
                    time.sleep(0.2)
            raise WorkflowError("Web startup not attested/ready; preserve process and state for OPS")

    def attest_web(self, slot_id: int) -> dict[str, Any]:
        from scripts.web.e2e.resource_link_workflow.execution_gate import ATTEST_ASSETS, ATTEST_MODULES

        slot = next(item for item in self.store.status()["slots"] if item["id"] == slot_id)
        self.assert_process(slot["web_pid"], slot["web_identity"])
        value = read_json(self.root / "runtime-attestations" / f"slot-{slot_id}.json")
        if value["parent_pid"] != slot["web_pid"] or value["slot"] != str(slot_id):
            raise WorkflowError("runtime attestation does not belong to current Web process")
        child = self.process_identity(value["pid"])
        port = slot["origin"].rsplit(":", 1)[1]
        parent = subprocess.check_output(["ps", "-p", str(value["pid"]), "-o", "ppid="], encoding="utf-8")
        if (int(parent.strip()) != slot["web_pid"]
                or "scripts.web.e2e.resource_link_workflow.web_entry" not in child
                or f"--port {port}" not in child):
            raise WorkflowError("attested Web interpreter no longer belongs to this slot")
        repo = Path(self.config["repo"]).resolve()
        if {item["module"] for item in value["modules"]} != set(ATTEST_MODULES):
            raise WorkflowError("required loaded modules missing")
        for module in value["modules"]:
            path = Path(module["path"])
            module_path = module["module"].replace(".", "/") + ".py"
            expected = repo / module_path if module["module"].startswith("scripts.") else repo / "src" / module_path
            if path != expected or digest(path) != module["sha256"]:
                raise WorkflowError("loaded module changed; restart same Web before execution")
        if {item["relative_path"] for item in value.get("assets", [])} != set(ATTEST_ASSETS):
            raise WorkflowError("required startup resource mapping/static manifest missing")
        for asset in value["assets"]:
            path = Path(asset["path"])
            expected = repo / "src" / "iac_code" / asset["relative_path"]
            if path != expected or digest(path) != asset["sha256"]:
                raise WorkflowError("startup resource mapping/static asset changed; restart same Web before execution")
        return {**value, "leader_pid": slot["web_pid"], "leader_identity": slot["web_identity"],
                "child_identity": child, "origin": slot["origin"], "same_slot_state_preserved": True}

    @staticmethod
    def process_identity(pid: int) -> str:
        if os.name == "nt":
            raise WorkflowError("native Windows process ownership adapter required; fail closed")
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout.strip()

    def assert_process(self, pid: int, identity: str) -> None:
        if not identity or self.process_identity(pid) != identity:
            raise WorkflowError("PID ownership mismatch or process gone; reconcile, never kill an unrelated PID")

    def restart_web(self, slot_id: int) -> dict[str, Any]:
        """OPS reloads a drained retained lease without detaching ownership."""
        status = self.store.status()
        if not status["paused"] or status["jobs"]:
            raise WorkflowError("same-Web maintenance restart requires pause and all models terminal")
        with self.store.transaction() as connection:
            if connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone():
                raise WorkflowError("drain issued actions before same-Web maintenance restart")
        slot = next(item for item in status["slots"] if item["id"] == slot_id)
        before = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
        if not before.get("unlocked") or before.get("busy"):
            raise WorkflowError("same-Web maintenance restart requires original unlocked idle target")
        self.stop_web(slot_id, _retained_maintenance=True)
        result = self.start_web(slot_id)
        after = self.browser({"operation": "refresh-session", "origin": slot["origin"], "target": slot["target"],
                              "session_id": before.get("session_id")})
        current = next(item for item in self.store.status()["slots"] if item["id"] == slot_id)
        if (not after.get("unlocked") or after.get("busy")
                or after.get("session_id") != before.get("session_id")
                or any(current[key] != slot[key] for key in ("task_id", "target", "origin", "cwd"))):
            raise WorkflowError("same-Web recovery binding changed; preserve paused state and investigate")
        return {**result, "same_session_preserved": True, "same_target_preserved": True,
                "original_page_reloaded": after.get("original_page_reloaded") is True,
                "ownership_lease_retained": True, "no_cloud_calls": True, "no_new_model_turn": True}

    def stop_web(self, slot_id: int, *, _retained_maintenance: bool = False) -> None:
        import signal
        import time

        slot = next(slot for slot in self.store.status()["slots"] if slot["id"] == slot_id)
        if slot["task_id"] and not _retained_maintenance:
            raise WorkflowError("drain the worker lease before stopping its Web")
        with exclusive(self.root / "slots" / str(slot_id) / "ops.lock"):
            if slot["web_pid"]:
                try:
                    self.assert_process(slot["web_pid"], slot["web_identity"])
                except subprocess.CalledProcessError:
                    pass  # Exact recorded PID is absent; clear only the stale record.
                else:
                    pid = slot["web_pid"]
                    if os.name == "nt" or os.getpgid(pid) != pid:
                        raise WorkflowError("Web process group ownership mismatch; OPS reconcile before stop")
                    # uv owns a dedicated session. Stopping only uv leaves the
                    # Python child and SSE connections alive on the reserved port.
                    os.killpg(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            self.assert_process(pid, slot["web_identity"])
                        except subprocess.CalledProcessError:
                            break
                        time.sleep(0.1)
                    else:
                        self.assert_process(pid, slot["web_identity"])
                        os.killpg(pid, signal.SIGKILL)
                with self.store.transaction() as connection:
                    connection.execute("UPDATE slots SET web_pid=NULL,web_identity=NULL WHERE id=?", (slot_id,))
                    self.store.event(connection, "web_stopped", {"slot": slot_id})

    def bootstrap(self, slot_id: int) -> dict[str, Any]:
        slot = next(slot for slot in self.store.status()["slots"] if slot["id"] == slot_id)
        if slot["task_id"]:
            raise WorkflowError("bootstrap cannot navigate a leased worker page")
        result = self.browser(
            {
                "operation": "bootstrap",
                "origin": slot["origin"],
                "token_file": str(self.root / "slots" / str(slot_id) / "access-token"),
            }
        )
        self.store.set_target(slot_id, result["target"])
        return result

    def tool(self, job_id: str, token: str, operation: str, arguments: dict[str, Any]) -> Any:
        context = self.store.context(job_id, token)
        if (context["role"] == "sol" and context["purpose"] in {"prepare", "repair"}
                and not context["resource"].get("query_candidates")):
            catalog_path = Path(__file__).resolve().parents[4] / (
                "src/iac_code/tools/cloud/aliyun/data/direct_resource_api_catalog.json"
            )
            candidates = refreshed_query_candidates(context["resource"], read_json(catalog_path))
            if candidates:
                context["resource"]["query_candidates"] = candidates
                context["query_candidate_refresh"] = {
                    "path": str(catalog_path), "sha256": digest(catalog_path),
                    "basis": "current reviewed production response contracts for own QUERY type only",
                    "execution_authorized": False, "frozen_plan_changed": False,
                }
        if operation == "context":
            with self.store.transaction() as connection:
                owner = connection.execute("SELECT value FROM meta WHERE key=?", ("job_backend:" + job_id,)).fetchone()
            if owner:
                context["worker_backend"] = json.loads(owner[0])
            from .credentials import source_readiness

            current = self.store.status()
            context["dispatch_state"] = {
                "revision": current["revision"],
                "paused": current["paused"],
                "review_only": current["review_only"],
                "pause_scope": "new_dispatch_only; existing jobs retain response-bound cleanup obligations",
                "worker_host": worker_hosts.host(self.config),
                "web_host": "local",
            }
            context["cloud_source_readiness"] = source_readiness(self.root)
            if context["role"] == "sol":
                context["control_runtime"] = self.attest_control()
                from scripts.web.e2e.resource_link_workflow.execution_gate import creations

                from .store import dumps

                with self.store.transaction() as connection:
                    workers = [
                        row[0]
                        for row in connection.execute(
                            "SELECT id FROM jobs WHERE task_id=? AND role='glm'", (context["task_id"],)
                        )
                    ]
                    context["ops_reconciled_creation_attempts"] = sorted(
                        self.store.reconciled_creations(connection, context["task_id"])
                    )
                records = creations(self.root / "execution-gate.sqlite3", workers)
                context["creation_attempts"] = records
                for record in records:
                    version = hashlib.sha256(dumps(record).encode()).hexdigest()[:12]
                    evidence = f"jobs/{record['job_id']}/output/{record['attempt_id']}-{version}-wire-source.json"
                    target = contained(self.root, evidence)
                    if not target.exists():
                        json_write(target, record, private=True)
                    if record["state"] != "succeeded" or not record["resource"]["id"]:
                        continue
                    with self.store.transaction() as connection:
                        ownership.register_generation(
                            connection, self.root, context["task_id"], record["dimension"], record["resource"],
                            record["job_id"], {"path": evidence, "sha256": digest(target)},
                        )
                context["cleanup_wire_reads"] = []
                for record in wire_evidence.cleanup_reads(self.root / "execution-gate.sqlite3", workers):
                    version = hashlib.sha256(dumps(record).encode()).hexdigest()[:12]
                    evidence = (
                        f"jobs/{record['job_id']}/output/"
                        f"{record['attempt_id']}-{version}-cleanup-wire-source.json"
                    )
                    target = contained(self.root, evidence)
                    if target.exists():
                        if read_json(target) != record:
                            raise WorkflowError("original cleanup wire export changed; preserve and investigate")
                    else:
                        json_write(target, record, private=True)
                    context["cleanup_wire_reads"].append({
                        "path": evidence, "sha256": digest(target), "dimension": record["dimension"],
                        "attempt_id": record["attempt_id"], "tool_use_id": record["tool_use_id"],
                        "state": record["state"], "absence_verified": False,
                    })
                context["artifact_index"] = [
                    {"path": path.relative_to(self.root).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
                    for worker in workers
                    for path in sorted((self.root / "jobs" / worker / "output").glob("*.json"))
                ]
                resource_inputs = contained(self.root / "inputs", context["resource"]["id"])
                context["preparation_inputs"] = [
                    path.relative_to(self.root / "inputs").as_posix()
                    for path in sorted(resource_inputs.rglob("*"))
                    if path.is_file() and path.suffix in {".json", ".md", ".txt"} and path.stat().st_size <= 2_000_000
                ]
                context["creation_approval_present"] = any(
                    read_json(path).get("task_id") == context["task_id"]
                    for path in (self.root / "creation-authorizations").glob("*.json")
                )
                context["web_runtimes"] = []
                for web_slot in self.store.status()["slots"]:
                    if web_slot["web_pid"]:
                        try:
                            context["web_runtimes"].append(self.attest_web(web_slot["id"]))
                        except (OSError, ValueError, subprocess.SubprocessError):
                            context["web_runtimes"].append({"slot": str(web_slot["id"]), "attested": False})
            if context["packet_path"]:
                packet_path = contained(self.root, context["packet_path"])
                packet = read_json(packet_path)
                context["packet"] = packet
                if context["role"] == "glm":
                    from .creation import requires_execution_review, validate_authorization

                    creation_authorized = True
                    if requires_execution_review(packet):
                        try:
                            validate_authorization(self.root, context["resource"]["id"], packet)
                        except WorkflowError:
                            creation_authorized = False
                    inputs_intact = all(
                        digest(contained(self.root, entry["path"])) == entry["sha256"]
                        for entry in packet["inputs"]
                    )
                    leased = bool(context["slot"] and context["slot"]["task_id"] == context["task_id"])
                    allowed = leased and creation_authorized and inputs_intact and (
                        current["review_only"] is None or context["purpose"] == "cleanup"
                        or (context["purpose"] == "continuation" and bool(
                            context.get("native_continuation") or context.get("existing_query_recovery")))
                    )
                    context["execution_admission"] = {
                        "authority": "current_program_state; historical proposal flags are not admission",
                        "packet_approved": True,
                        "packet_sha256": digest(packet_path),
                        "job_already_dispatched": leased,
                        "creation_authorized": creation_authorized,
                        "inputs_intact": inputs_intact,
                        "dispatch_allowed": allowed,
                        "signed_identity_verified": False,
                        "execution_policy": "one dimension at a time; prove owned cleanup before next creation",
                    }
                    # Immutable proposals contain historical hold/approval narratives.
                    # Execution receives only the approved operational contract; keep
                    # the complete original packet and its SHA for independent review.
                    context["packet"] = {
                        key: packet[key]
                        for key in (
                            "task_id", "profile", "terraform", "budget_cents", "requests", "cleanup_requests",
                            "execution_grants", "scenes", "inputs", "ui", "preserve_pass", "execution_selection",
                        )
                        if key in packet
                    }
                    context["packet"].update(
                        proposal_status="program_approved_operational_contract",
                        executable=allowed,
                        dispatch_allowed=allowed,
                    )
                    if context["purpose"] not in {"cleanup", "navigation", "continuation"}:
                        context["dimension_transition"] = self.dimension_transition_admission(job_id, packet)
                    if context.get("navigation_recovery"):
                        context["execution_admission"].update(
                            execution_policy="navigation only; original signed source/session; zero cloud calls",
                            cloud_calls_allowed=False,
                        )
                    if context.get("native_continuation"):
                        from scripts.web.e2e.resource_link_workflow import execution_gate as gate

                        with gate.database(self.root / "execution-gate.sqlite3") as wire:
                            active = wire.execute("SELECT * FROM active WHERE scope=?",
                                                  (context["native_continuation"]["scope"],)).fetchone()
                            managed = context["native_continuation"].get("kind") == "managed_read_continuation"
                            retained = context["native_continuation"].get("kind") == "retained_ros_capture"
                            if retained and not active:
                                active = wire.execute("SELECT * FROM active WHERE scope=? AND session=?", (
                                    context["native_continuation"]["original_job_id"] + ":QUERY",
                                    context["native_continuation"]["session_id"],
                                )).fetchone()
                            chain = (gate.nas_native_continuation(wire, active)
                                     if active and not (managed or retained) else None)
                            managed_identity = wire.execute("SELECT used FROM grants WHERE scope=? AND id=?", (
                                context["native_continuation"]["scope"],
                                context["native_continuation"].get("identity_grant_id"),
                            )).fetchone() if managed or retained else None
                        if not active or (not (managed or retained) and not chain):
                            raise WorkflowError("native continuation lineage unavailable")
                        context["native_identity_pending"] = (bool(managed_identity and managed_identity[0] == 0)
                                                              if managed or retained else chain[0]["used"] == 0)
                        context["native_phase"] = active["phase"]
                        if retained:
                            context["retained_ros_capture"] = {
                                "new_cloud_grants": 0, "new_creation_quota": 0,
                                "instruction": "Reuse frozen original ROS source and full session. First click "
                                "only missing ROS open; native notice inspector preserves landing and closes only "
                                "the exact announcement. If native_identity_pending, continue-execution identity "
                                "ONCE and wait for actual same-account result. Then cleanup ROS ONCE "
                                "(DeleteStack FIRST), wait, "
                                "continue-read ROS cleanup if needed, cleanup-source ROS. Only after successful "
                                "original cleanup request QUERY for its FIRST original STS/list, wait, source QUERY, "
                                "click QUERY open and submit. No new-session, ROS request/source, unreviewed renewal, "
                                "recreation or business-resource mutation. All old source/PNG/counters remain.",
                            }
                        if managed:
                            context["managed_read_continuation"] = {
                                "dimension": "TF", "new_cloud_grants": 1 if managed_identity else 0,
                                "new_creation_quota": 0,
                                "instruction": "If native_identity_pending=true, FIRST continue-execution "
                                "stage=identity ONCE and wait for actual same-account success. Then use "
                                "continue-read dimension=TF stage=source in the SAME session. "
                                "Do NOT request/new-session or repeat STS/CreateStack. Read ListStackResources, "
                                "then GetStackResource for sample/sample_vpc/sample_vswitch, then the original "
                                "bound service reads. Repeat continue-read source for each next dependency group; "
                                "only after all groups complete capture NEW source and the missing real Shift click. "
                                "After the click, use original cleanup once and verify all five absence reads. "
                                "Original incomplete source and cleanup intent remain immutable history.",
                            }
                            if context["native_continuation"].get("capture_source"):
                                context["managed_read_continuation"].update(
                                    new_cloud_grants=0,
                                    instruction="All original member/detail reads and renewed identity already "
                                    "succeeded. Use the exact frozen capture_source for the missing real Shift "
                                    "click now. Do not new-session/request/source/continue-read source or STS. "
                                    "Then original cleanup once, wait, remaining cleanup reads, cleanup-source "
                                    "and submit. Original source and all old outputs remain immutable.",
                                )
            if context["role"] == "sol" and context["purpose"] == "review" and context["observations"]:
                with self.store.transaction() as connection:
                    source_job = connection.execute(
                        "SELECT * FROM jobs WHERE task_id=? AND role='glm' ORDER BY created DESC,rowid DESC",
                        (context["task_id"],),
                    ).fetchone()
                managed_recovery = False
                if source_job and source_job["purpose"] == "continuation":
                    from .native_continuation import load

                    with self.store.transaction() as connection:
                        if not context.get("existing_query_recovery"):
                            managed_recovery = load(self.root, connection, context["task_id"]).get("kind") in {
                                "managed_read_continuation", "retained_ros_capture",
                            }
                if source_job and source_job["purpose"] == "continuation" and not managed_recovery:
                    context["native_cleanup_order"] = {
                        "instruction": "Inspect every original missing-click PNG with view_image and verify source, "
                        "account, full session, region, resource and real landing. Before cleanup do NOT accept PASS. "
                        "If navigation is verified, write the below JSON to inputs, then block with category cleanup "
                        "and its path/SHA. OPS/Luna will queue original cleanup; "
                        "final acceptance follows actual absence.",
                        "required_review": {"kind": "native_screenshot_review", "task_id": context["task_id"],
                            "job_id": job_id, "source_job_id": source_job["id"], "navigation_verified": True,
                            "verified_scenes": "sorted by scene: [{scene, screenshot:state-relative PNG, sha256}]"},
                    }
            with self.store.transaction() as connection:
                context["owned"] = [
                    dict(row)
                    for row in connection.execute("SELECT * FROM owned WHERE task_id=?", (context["task_id"],))
                ]
                if context["role"] == "glm":
                    from .pending_delete import prior_user_decisions

                    context["resource_group_cleanup_user_decisions"] = prior_user_decisions(
                        self.root, connection, context["task_id"],
                    )
            return context
        if context["role"] == "sol":
            return self.sol_tool(context, token, operation, arguments)
        if operation == "submit":
            self.store.submit(job_id, token, arguments["report"])
            return {"submitted": True, "not_accepted": True}
        if operation not in {
            "snapshot",
            "close-preview",
            "new-session",
            "request",
            "click",
            "evidence",
            "source",
            "cleanup-source",
            "cleanup",
            "continue-read",
            "continue-execution",
            "wait",
        }:
            raise WorkflowError("GLM operation is not allowed")
        slot = context["slot"]
        if not slot or not slot["target"]:
            raise WorkflowError("slot target not bootstrapped")
        packet = read_json(contained(self.root, context["packet_path"]))
        for entry in packet["inputs"]:
            if digest(contained(self.root, entry["path"])) != entry["sha256"]:
                raise WorkflowError("approved packet evidence changed")
        recovery = context.get("cleanup_recovery")
        navigation = context.get("navigation_recovery")
        native = context.get("native_continuation")
        managed = bool(native and native.get("kind") == "managed_read_continuation")
        retained = bool(native and native.get("kind") == "retained_ros_capture")
        existing = context.get("existing_query_recovery")
        if existing and operation not in {
            "snapshot", "wait", "close-preview", "new-session", "request", "source", "click", "evidence",
        }:
            raise WorkflowError("existing sample query forbids creation, cleanup and original graph continuation")
        if native and not (managed or retained) and context["purpose"] != "cleanup" and operation == "cleanup":
            raise WorkflowError("original Sol must inspect the missing click before cleanup; submit the capture first")
        if native and operation not in {
            "snapshot", "wait", "close-preview", "continue-execution", "source", "cleanup-source",
            "click", "evidence", "cleanup",
        } | ({"continue-read"} if managed or retained else set()) | ({"request"} if retained else set()):
            raise WorkflowError("native continuation forbids new session, packet install and initial request")
        if operation == "continue-execution" and not native:
            raise WorkflowError("continue-execution requires independently reviewed native recovery")
        if managed and ((operation == "continue-execution" and (
            arguments.get("stage") != "identity" or not native.get("identity_grant_id")
        )) or arguments.get("dimension", "TF") != "TF"):
            raise WorkflowError("managed continuation only permits original TF reads, click and cleanup")
        if retained and (
            (operation == "continue-execution" and (
                arguments.get("stage") != "identity" or not native.get("identity_grant_id")))
            or (operation in {"request", "source"} and arguments.get("dimension") != "QUERY")
            or (operation in {"cleanup", "cleanup-source"} and arguments.get("dimension") != "ROS")
            or (operation == "continue-read" and (arguments.get("dimension"), arguments.get("stage"))
                != ("ROS", "cleanup"))
        ):
            raise WorkflowError("retained capture only permits original ROS click/cleanup and first unused QUERY")
        if navigation and operation not in {"snapshot", "wait", "click", "evidence", "close-preview"}:
            raise WorkflowError("navigation-only recovery forbids new sessions, requests, cloud calls and cleanup")
        if "execution_selection" in packet and not recovery and operation in {
            "request", "cleanup", "source", "cleanup-source", "continue-read"
        } and (
            arguments.get("dimension") not in packet["requests"]
        ):
            raise WorkflowError("dimension is outside this packet's approved active execution scope")
        if "execution_selection" in packet and not recovery and operation in {"click", "evidence"} and (
            arguments.get("scene") not in {scene["id"] for scene in context["expected_scenes"]}
        ):
            raise WorkflowError("scene is outside this packet's approved active execution scope")
        original_job = (recovery or navigation or native or {}).get("original_job_id", job_id)
        source_output = self.root / "jobs" / original_job / "output"
        artifact_output = Path(context["output_dir"]) if native else source_output
        if native and digest(contained(self.root, native["evidence"])) != native["evidence_sha256"]:
            raise WorkflowError("independent native continuation review changed")
        if (managed or retained) and native.get("capture_source"):
            capture_source = contained(self.root, native["capture_source"]["path"])
            if digest(capture_source) != native["capture_source"]["sha256"]:
                raise WorkflowError("original managed capture source changed")
            if managed and (operation in {"source", "continue-execution"} or (operation == "continue-read"
                                                               and arguments.get("stage") != "cleanup")):
                raise WorkflowError("managed capture-only reuses completed source; no additional execution reads")
        if recovery:
            proof = contained(self.root, recovery["evidence"])
            if digest(proof) != recovery["evidence_sha256"]:
                raise WorkflowError("original OPS cleanup review changed")
            cleanup_operations = {"snapshot", "wait", "cleanup", "cleanup-source", "continue-read", "submit"}
            if recovery.get("native"):
                cleanup_operations.update({"continue-execution", "close-preview"})
            if operation not in cleanup_operations:
                raise WorkflowError("cleanup-only recovery forbids creation, new sessions, clicks and new tests")
            if (operation in {"cleanup", "cleanup-source", "continue-read"}
                    and arguments.get("dimension") != recovery["dimension"]):
                raise WorkflowError("cleanup-only recovery is limited to the original dimension")
            if operation == "continue-read" and arguments.get("stage") != "cleanup":
                raise WorkflowError("cleanup-only continuation is limited to original post-delete reads")
        if operation == "wait":
            # No browser/UI lock, cloud request, capability installation or
            # deadline. The already-authorized exact worker simply yields.
            return delays.worker_wait(arguments.get("seconds", 30))
        session_file = self.root / "jobs" / original_job / "output/self-created-session.json"
        if operation in {
            "source", "cleanup-source", "click", "evidence", "close-preview", "continue-read", "continue-execution",
        }:
            if not session_file.is_file():
                raise WorkflowError("this job has no program-bound self-created session")
            bound_session = read_json(session_file)
            if any(bound_session.get(k) != v for k, v in (
                ("job_id", original_job), ("target", slot["target"]), ("origin", slot["origin"])
            )):
                raise WorkflowError("self-created session lease mismatch")
            if arguments.get("session_id", bound_session["session_id"]) != bound_session["session_id"]:
                raise WorkflowError("explicit session differs from this job bound key; do not guess")
            arguments = {**arguments, "session_id": bound_session["session_id"]}
        request = {
            "operation": operation,
            "origin": slot["origin"],
            "target": slot["target"],
            "output_dir": context["output_dir"],
            **packet.get("ui", {}),
        }
        if operation == "continue-execution":
            from .native_continuation import delivery

            stage = arguments.get("stage", "source")
            if stage == "cleanup" and context["purpose"] != "cleanup":
                raise WorkflowError("Sol screenshot review and OPS cleanup queue required before native cleanup")
            if recovery and recovery.get("native") and stage not in {"identity", "cleanup"}:
                raise WorkflowError("native cleanup recovery cannot execute source or creation operations")
            output = Path(context["output_dir"])
            native_dimension = native.get("dimension", "QUERY")
            frozen = output / f"{native_dimension}-{'cleanup' if stage == 'cleanup' else 'product'}-source.json"
            if stage != "identity" and frozen.exists():
                raise WorkflowError("native phase source already frozen; never overwrite")
            if stage == "source":
                assert_navigation_before_cleanup(output, native_dimension)
            if stage == "cleanup" and not (output / f"{native_dimension}-cleanup-intent.json").is_file():
                raise WorkflowError("explicit cleanup required before cleanup continuation")
            current = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
            if (current.get("session_id") != arguments["session_id"] or current.get("busy")
                    or not current.get("unlocked")):
                raise WorkflowError("native continuation requires original unlocked idle full session")
            request["operation"] = "request"
            request["session_id"] = arguments["session_id"]
            if retained:
                from .retained_capture import identity_delivery

                click = output / "R0083-ROS-1-open.json"
                if not click.is_file() or read_json(click).get("actual_ui_click") is not True:
                    raise WorkflowError("retained identity follows the actual missing ROS click only")
                request["text"] = identity_delivery(self.root, native)
            else:
                request["text"] = delivery(self.root, native, stage)
            prior = sorted(output.glob(f"{native_dimension}-{stage}-native-*.json"))
            if any(read_json(path).get("state") != "submitted" for path in prior):
                raise WorkflowError("unknown native continuation submission; do not repeat")
            continuation = output / f"{native_dimension}-{stage}-native-{len(prior) + 1:04d}.json"
            json_write(continuation, {"state": "may_have_submitted", "scope": native["scope"],
                                      "session_id": arguments["session_id"], "stage": stage}, private=True)
        elif operation == "new-session":
            if (Path(context["output_dir"]) / "new-session-intent.json").exists():
                raise WorkflowError("one self-created Web session per attempt; do not repeat an unknown creation")
            before = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
            json_write(
                Path(context["output_dir"]) / "new-session-intent.json", {"previous_session": before.get("session_id")}
            )
            request["selector"] = packet["ui"]["new_session_selector"]
        elif operation == "close-preview":
            request["session_id"] = arguments["session_id"]
        elif operation == "request":
            dimension = arguments["dimension"]
            if existing:
                if dimension != "QUERY":
                    raise WorkflowError("existing sample admission is QUERY only")
            else:
                self.assert_previous_cleanup(original_job if retained else job_id, dimension,
                                             output_dir=Path(context["output_dir"]) if retained else None)
            request["text"] = execution_request(packet["requests"][dimension])
            marker = Path(context["output_dir"]) / f"{dimension}-request-intent.json"
            if marker.exists():
                raise WorkflowError(
                    "approved request already submitted or outcome unknown; never repeat cloud creation"
                )
            from scripts.web.e2e.resource_link_workflow.execution_gate import install

            from .creation import (
                authorization_path,
                execution_authority,
                requires_execution_review,
                validate_authorization,
            )

            if requires_execution_review(packet):
                validate_authorization(self.root, context["resource"]["id"], packet)
            current = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
            intent = source_output / "new-session-intent.json"
            session = current.get("session_id")
            if current.get("busy"):
                raise WorkflowError("wait for the current answer to finish before activating another dimension")
            if not session or not intent.is_file() or session == read_json(intent)["previous_session"]:
                raise WorkflowError("exact self-created session required before activating cloud capabilities")
            if not session_file.is_file() or read_json(session_file)["session_id"] != session:
                raise WorkflowError("current session differs from this job self-created session")
            account = next(iter(packet["scenes"].values()))["account"]
            expires = None
            if requires_execution_review(packet):
                expires = read_json(authorization_path(self.root, packet))["expires_at"]
            install(
                self.root / "execution-gate.sqlite3",
                str(slot["id"]),
                f"{original_job if retained else job_id}:{dimension}",
                session,
                account,
                expires,
                packet["execution_grants"][dimension],
                authority=("existing-query:" + existing["evidence_sha256"] if existing
                           else execution_authority(self.root, packet, dimension)),
                **({"existing_review": (str(contained(self.root, existing["evidence"])),
                                        existing["evidence_sha256"])} if existing else {}),
            )
            json_write(marker, {"dimension": dimension, "state": "may_have_submitted"})
        elif operation == "cleanup":
            native_rocketmq = (context["resource"].get("key") == "ALIYUN::ROCKETMQ::Instance"
                              and arguments.get("dimension") == "ROS")
            if retained or (native_rocketmq and not recovery):
                ros_click = Path(context["output_dir"]) / "R0083-ROS-1-open.json"
                inspected = read_json(ros_click).get("console_inspection", {}) if ros_click.is_file() else {}
                if inspected.get("readonly_completed") is not True or inspected.get("identity_visible") is not True:
                    raise WorkflowError(
                        "retained ROS announcement capture failed; submit pixels before cleanup recovery"
                    )
            if (managed or retained) and not any(
                p.is_file() and read_json(p).get("actual_ui_click") is True
                for p in (Path(context["output_dir"]) / (s["id"] + ".json") for s in context["expected_scenes"])
            ):
                raise WorkflowError("capture the missing actual MSE click before original cleanup")
            if arguments["dimension"] in context["readonly_retest_dimensions"]:
                raise WorkflowError("existing-source navigation retest cannot delete existing resources")
            if retained and native.get("identity_grant_id"):
                from scripts.web.e2e.resource_link_workflow import execution_gate as gate

                with gate.database(self.root / "execution-gate.sqlite3") as wire:
                    renewed = wire.execute("SELECT used FROM grants WHERE scope=? AND id=?",
                                           (native["scope"], native["identity_grant_id"])).fetchone()
                if not renewed or renewed[0] != 1:
                    raise WorkflowError("complete the separately approved same-account identity before cleanup")
            request["operation"] = "request"
            request["text"] = packet["cleanup_requests"][arguments["dimension"]]
            if native_rocketmq and not retained:
                request["text"] += (
                    "\n先且仅执行原创建响应绑定的唯一DeleteStack一次，逐字使用原机器契约参数，"
                    "包括RetainAllResources=false；不再次STS，不先GetStack或ONS。"
                    "删除成功后才能执行after_delete的原GetStack/OnsInstanceInServiceList，"
                    "保存根栈DELETE_COMPLETE和自身精确实例ID消失原证。不改业务资源；未知删除不重复。"
                )
            if managed:
                request["text"] += (
                    "\n原会话身份续核已成功，不重发STS，不创建、不改名。当前是独立清理阶段，"
                    "先且仅执行原机器契约中尚未使用的响应绑定DeleteStack一次。"
                    "标有after_delete的GetStack及服务/网络消失读取只能在该删除实际成功后执行，"
                    "不得在删除前先读取这些项。保留真实结果；未知删除不重复。"
                )
            if retained:
                from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

                request["text"] += (
                    "\n本次沿用原代次、原会话及已核验签名，原身份额度已用尽，不再次STS。"
                    "先且仅执行以下原创建响应绑定DeleteStack一次，逐字使用全部参数，不加默认参数。"
                    "删除实际成功后再执行原after_delete的GetStack和OnsInstanceInServiceList，"
                    "保存根栈DELETE_COMPLETE和自身实例消失原证；不删除业务实例，不再次创建：\n"
                    + canonical(native["delete_call"])
                )
            if recovery and not recovery.get("read_only"):
                request["text"] += (
                    "\n这是原创建物的清理恢复，不是新会话或新创建。每次实际调用仍由门禁核验原账号、"
                    "完整会话和实际签名凭据；同凭据身份绑定已存在且 identity 剩余为零时，禁止重复身份查询。"
                    "复用原成功创建、详情及已完成的精确成员为空原证；不能重调额度为零的前置读取。"
                    "先执行机器契约唯一仍未调用的响应绑定删除；标有 after_delete 的必查读取只在删除成功后执行。"
                    "若实际签名轮换、绑定缺失、未知调用或剩余额度不足则如实停止，不能绕过门禁。"
                )
            marker = Path(context["output_dir"]) / f"{arguments['dimension']}-cleanup-intent.json"
            if marker.exists():
                raise WorkflowError("cleanup already submitted or unknown; verify absence before retry")
            from scripts.web.e2e.resource_link_workflow.execution_gate import cleanup

            current = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
            if not session_file.is_file():
                raise WorkflowError("cleanup requires this job exact self-created session")
            binding = read_json(session_file)
            if any(binding.get(key) != value for key, value in (
                ("job_id", original_job), ("origin", slot["origin"]), ("target", slot["target"])
            )):
                raise WorkflowError("cleanup self-created session lease mismatch")
            def ensure_cleanup_ready(snapshot: dict[str, Any]) -> None:
                if snapshot.get("session_id") != binding["session_id"]:
                    raise WorkflowError("cleanup requires this job exact self-created session")
                if snapshot.get("busy") or not snapshot.get("unlocked"):
                    raise WorkflowError("cleanup requires an unlocked idle original session")

            ensure_cleanup_ready(current)
            if current.get("preview_open"):
                # Close only this leased session's fixed product preview before
                # activating cleanup or persisting a may-have-submitted marker.
                self.browser({"operation": "close-preview", "origin": slot["origin"],
                              "target": slot["target"], "session_id": binding["session_id"]})
                current = self.browser({"operation": "snapshot", "origin": slot["origin"],
                                        "target": slot["target"]})
                ensure_cleanup_ready(current)
                if current.get("preview_open"):
                    raise WorkflowError("cleanup preview still blocks the original composer")
            if not recovery or not recovery.get("read_only"):
                cleanup(
                    self.root / "execution-gate.sqlite3",
                    str(slot["id"]),
                    f"{original_job}:{arguments['dimension']}",
                    current.get("session_id", ""),
                )
            if recovery and recovery.get("native"):
                from .native_continuation import delivery

                request["text"] = delivery(self.root, native, "cleanup")
            elif recovery:
                if recovery.get("read_only"):
                    request["operation"] = "original-read-continuation"
                    request["text"] = cleanup_recovery.exact_readonly_request(
                        self.root, recovery, str(slot["id"]), binding["session_id"],
                        packet["execution_grants"][arguments["dimension"]],
                    )
                else:
                    request["text"] += cleanup_recovery.exact_delete_request(
                        self.root, recovery, str(slot["id"]), binding["session_id"]
                    )
            json_write(marker, {"state": "may_have_submitted"})
        elif operation == "continue-read":
            dimension, stage = arguments["dimension"], arguments["stage"]
            if dimension not in packet["requests"]:
                raise WorkflowError("continuation dimension outside the approved packet")
            output = Path(context["output_dir"])
            frozen = output / f"{dimension}-{'cleanup' if stage == 'cleanup' else 'product'}-source.json"
            if frozen.exists():
                raise WorkflowError("original source already frozen; do not overwrite or replay it")
            intent_output = source_output if managed and stage == "source" else output
            required = intent_output / f"{dimension}-{'cleanup' if stage == 'cleanup' else 'request'}-intent.json"
            if not required.is_file():
                raise WorkflowError("continue-read requires an original submitted phase request")
            if stage == "source":
                assert_navigation_before_cleanup(output, dimension)
            current = self.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
            if (current.get("session_id") != arguments["session_id"] or current.get("busy")
                    or not current.get("unlocked")):
                raise WorkflowError("continue-read requires the original unlocked idle full session")
            request["operation"] = "original-read-continuation"
            request["text"] = readonly_continuation(
                self.root / "execution-gate.sqlite3", str(slot["id"]), f"{original_job}:{dimension}",
                arguments["session_id"], stage,
            )
            prior = sorted(output.glob(f"{dimension}-{stage}-continue-*.json"))
            if any(read_json(path).get("state") != "submitted" for path in prior):
                raise WorkflowError("unknown prior continuation; preserve evidence, do not resubmit")
            continuation = output / f"{dimension}-{stage}-continue-{len(prior) + 1:04d}.json"
            json_write(continuation, {"state": "may_have_submitted", "scope": f"{original_job}:{dimension}",
                                      "session_id": arguments["session_id"], "stage": stage}, private=True)
        elif operation in {"source", "cleanup-source"}:
            dimension = arguments["dimension"]
            if operation == "cleanup-source" and not (
                Path(context["output_dir"]) / f"{dimension}-cleanup-intent.json"
            ).is_file():
                raise WorkflowError("cleanup-source requires an actual explicit cleanup phase request first")
            if dimension not in packet["requests"]:
                raise WorkflowError("source dimension outside packet")
            if managed and operation == "source":
                phases.assert_managed_source_reads_complete(
                    self.root / "execution-gate.sqlite3", f"{original_job}:TF",
                )
            assert_stack_source_ready(
                self.root / "execution-gate.sqlite3", f"{original_job}:{dimension}",
                cleanup=operation == "cleanup-source",
            )
            if operation == "cleanup-source":
                assert_cleanup_reads_complete(self.root / "execution-gate.sqlite3", f"{original_job}:{dimension}")
            request["operation"] = "source"
            artifact_id = f"{dimension}-{'cleanup' if operation == 'cleanup-source' else 'product'}-source"
            if (Path(context["output_dir"]) / f"{artifact_id}.json").exists():
                raise WorkflowError("raw product source is immutable; do not overwrite it")
            request.update(session_id=arguments["session_id"], artifact_id=artifact_id)
        elif operation in {"click", "evidence"}:
            scene = next((scene for scene in context["expected_scenes"] if scene["id"] == arguments["scene"]), None)
            if not scene:
                raise WorkflowError("unknown scene or immutable PASS replay")
            request.update(session_id=arguments["session_id"], mode=scene["mode"], artifact_id=scene["id"])
            intent = source_output / "new-session-intent.json"
            if not intent.is_file() or read_json(intent)["previous_session"] == arguments["session_id"]:
                raise WorkflowError("worker must self-create a new Web session in this attempt")
            if (Path(context["output_dir"]) / f"{scene['id']}.png").exists():
                raise WorkflowError("do not replay an existing scene; submit it for independent review")
            binding = packet["scenes"][scene["id"]]
            if navigation and navigation.get("capture_recovery"):
                binding = {**binding, "console_inspection": navigation["capture_recovery"]["console_inspection"]}
                if existing and binding["console_inspection"] == "nas_dataflow_metadata":
                    binding.update(file_system_id=existing["binding"]["file_system_id"],
                                   fileset_id=existing["binding"]["fileset_id"])
                old_click = read_json(source_output / f"{scene['id']}.json")
                request["recovery_occurrence"] = old_click["clicked_occurrence"]
            request["selector"] = binding["selector"]
            source = contained(self.root, binding["source_evidence"])
            if digest(source) != binding["source_sha256"]:
                raise WorkflowError("source provenance changed")
            if operation == "evidence" and scene["mode"] == "creation":
                # A creation screenshot is not a click, but its provenance must
                # describe this generation, not the proposal's preparation input.
                # Missing/empty product sources remain useful failure evidence;
                # they do not prove successful creation or earn a verdict.
                generated = Path(context["output_dir"]) / f"{scene['dimension']}-product-source.json"
                request["creation_source_verified"] = False
                if generated.is_file():
                    value = read_json(generated)
                    if value.get("session_id") != arguments["session_id"]:
                        raise WorkflowError("creation source/session mismatch")
                    binding = {**binding, "source_evidence": generated.relative_to(self.root).as_posix(),
                               "source_sha256": digest(generated)}
                    references = self.proven_references(
                        job_id, scene["dimension"], arguments["session_id"], binding["account"],
                        value.get("references", {}).get("items", []),
                    )
                    if references:
                        request["creation_source_verified"] = bool(self.created_references(
                            job_id, scene["dimension"], arguments["session_id"], references,
                        ))
            if operation == "click":
                assert_navigation_before_cleanup(artifact_output, scene["dimension"])
                generated = (capture_source if (managed or (retained and scene["dimension"] == "ROS"))
                             and native.get("capture_source") else
                             artifact_output / f"{scene['dimension']}-product-source.json")
                if not generated.is_file():
                    raise WorkflowError("click requires this new session original product source")
                binding = {
                    **binding,
                    "source_evidence": generated.relative_to(self.root).as_posix(),
                    "source_sha256": digest(generated),
                }
                generated_value = read_json(generated)
                if generated_value["session_id"] != arguments["session_id"]:
                    raise WorkflowError("creation source/session mismatch")
                references = self.proven_references(
                    original_job, scene["dimension"], arguments["session_id"], binding["account"],
                    generated_value["references"]["items"],
                )
                signed_references = references
                if existing:
                    from .existing_query import references as existing_references

                    references = existing_references(
                        self.root, existing, original_job, arguments["session_id"], references,
                    )
                if scene["dimension"] == "TF" and "TF" in context["readonly_retest_dimensions"]:
                    from .remaining_review import managed_references

                    references = managed_references(
                        self.root / "execution-gate.sqlite3", f"{original_job}:TF", arguments["session_id"],
                        binding["account"], references,
                    )
                if scene["dimension"] == "QUERY" and context.get("native_nas_query_sample"):
                    references = self.native_nas_query_references(
                        original_job, arguments["session_id"], binding["account"], references,
                    )
                elif scene["dimension"] != "QUERY" and scene["dimension"] not in context["readonly_retest_dimensions"]:
                    references = self.created_references(
                        original_job, scene["dimension"], arguments["session_id"], references
                    )
                if "{identifier}" in request["selector"]:
                    reference_types = binding.get("reference_types", [scene.get("source_type")])
                    if not isinstance(reference_types, list) or any(
                        not isinstance(value, str) for value in reference_types
                    ):
                        raise WorkflowError("precise reference types must be an explicit string list")
                    matches = [
                        reference
                        for reference in references
                        if (
                            reference.get("resourceType") in reference_types
                            and (
                                binding["region"] == "global" or reference_region_matches(reference, binding["region"])
                            )
                            and (
                                not binding.get("name_prefix")
                                or reference.get("identifier", "").startswith(binding["name_prefix"])
                            )
                        )
                    ]
                    identifiers = {reference.get("identifier") for reference in matches}
                    # Repeated API results may prove the same resource; the browser
                    # separately freezes the real occurrence and href for both modes.
                    if len(identifiers) != 1 or not next(iter(identifiers)):
                        raise WorkflowError(
                            "newly generated precise reference must be unique; Sol repair input, do not guess"
                        )
                    identifier = next(iter(identifiers))
                    projected = {navigation_identifier(reference, generated_value.get("messages", {}).get("tools", {}))
                                 for reference in matches}
                    if len(projected) != 1:
                        raise WorkflowError("precise navigation identity must be unique")
                    locator = next(iter(projected))
                    request["source_identifier"] = identifier
                    request["render_identifier"] = locator
                    # The original ROS member card labels its NAS mount link
                    # with the composite PhysicalResourceId. The bare domain
                    # identifies the console landing, not that DOM occurrence.
                    # Keep href matching and other resource projections intact;
                    # navigation_identifier above has already verified this
                    # exact managed member against its own original response.
                    if (":" in identifier and all(reference.get("resourceType") == "alicloud_nas_mount_target"
                                                  for reference in matches)):
                        label = identifier.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ")
                        request["selector"] = request["selector"].replace(
                            '[aria-label="{identifier}"]', f'[aria-label="{label}"]',
                        )
                    escaped = locator.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ")
                    request["selector"] = request["selector"].replace("{identifier}", escaped)
                elif not binding.get("identifier") or not any(
                    reference.get("identifier") == binding["identifier"]
                    and reference.get("resourceType") == binding.get("expected_type", scene.get("source_type"))
                    for reference in references
                ):
                    raise WorkflowError("query needs its exact resource in fresh signed product source")
                else:
                    exact_type = binding.get("expected_type", scene.get("source_type"))
                    exact = [reference for reference in references
                             if reference.get("identifier") == binding["identifier"]
                             and reference.get("resourceType") == exact_type]
                    projected = {navigation_identifier(reference, generated_value.get("messages", {}).get("tools", {}))
                                 for reference in exact}
                    if len(projected) != 1:
                        raise WorkflowError("precise navigation identity must be unique")
                    request["source_identifier"] = binding["identifier"]
                    request["render_identifier"] = next(iter(projected))
                if navigation and not navigation.get("capture_recovery"):
                    exact = [reference for reference in references
                             if reference.get("identifier") == binding["identifier"]
                             and reference.get("resourceType") == binding.get("expected_type", scene["source_type"])]
                    # Select the resource's own successful source card, not an
                    # identical identifier emitted by another source dimension.
                    owners = {source["toolUseId"] for reference in exact for source in reference.get("sources", [])
                              if source.get("evidence") == "api_response" and source.get("product") == "ros"
                              and source.get("action") in {"GetStackResource", "ListStackResources"}}
                    if len(owners) != 1:
                        raise WorkflowError("navigation recovery requires one exact original ROS source card")
                    owner = next(iter(owners))
                    if not owner.replace("_", "").replace("-", "").isalnum():
                        raise WorkflowError("invalid original source card identity")
                    request["selector"] = f'details[data-tool-use-id="{owner}"] ' + request["selector"]
                    request["source_card_tool_use_id"] = owner
                    turns = {source.get("turnId") for reference in exact for source in reference.get("sources", [])
                             if source.get("toolUseId") == owner and source.get("evidence") == "api_response"}
                    if (len(turns) != 1 or not all(isinstance(turn, str) and turn
                                                 and turn.replace("-", "").isalnum() for turn in turns)):
                        raise WorkflowError("exact original source card turn required")
                    request["source_card_turn_id"] = next(iter(turns))
                elif scene["dimension"] in {"QUERY", "TF"}:
                    bind_card = (managed_source_card_binding if scene["dimension"] == "TF"
                                 else query_source_card_binding)
                    source_card = bind_card(
                        references, request.get("source_identifier", binding.get("identifier", "")),
                        binding.get("expected_type", scene["source_type"]),
                    )
                    if source_card:
                        request.update(source_card)
                        owner = source_card["source_card_tool_use_id"]
                        request["selector"] = f'details[data-tool-use-id="{owner}"] ' + request["selector"]
                elif scene["dimension"] == "ROS" and (
                    retained or scene["source_type"] == "ALIYUN::ROCKETMQ::Instance"
                ):
                    own = [{**ref, "sources": [s for s in ref.get("sources", [])
                             if s.get("product") == "ros" and s.get("action") == "GetStackResource"]}
                           for ref in references]
                    card = managed_source_card_binding(own, request["source_identifier"], scene["source_type"])
                    if not card:
                        raise WorkflowError("native capture requires its unique original ROS resource card")
                    request.update(card)
                    request["selector"] = (f'details[data-tool-use-id="{card["source_card_tool_use_id"]}"] '
                                           + request["selector"])
                if binding.get("dynamic_render_binding") == "nas_managed_mount_target":
                    dynamic_markers = binding.get("render_text")
                    if (scene["dimension"] != "TF" or scene["source_type"] != "alicloud_nas_mount_target"
                            or binding.get("console_inspection") != "nas_mount_target_address"
                            or binding.get("file_system_id") != "{file_system_id}"
                            or not isinstance(dynamic_markers, list) or len(dynamic_markers) != 2
                            or dynamic_markers[0] != "{mount_target_domain}"):
                        raise WorkflowError("dynamic NAS render fields require their exact managed mount contract")
                    members = [reference for reference in references
                               if reference.get("resourceType") == scene["source_type"]
                               and reference.get("identifier") == request["source_identifier"]]
                    identities = {console_sources.managed_nas_mount_identity(
                        member, generated_value.get("messages", {}).get("tools", {})) for member in members}
                    if len(identities) != 1:
                        raise WorkflowError("managed NAS render requires its unique current creation member")
                    parent, domain = next(iter(identities))
                    binding = {**binding, "file_system_id": parent,
                               "render_text": [domain, *binding["render_text"][1:]]}
                markers = binding.get("render_text", [])
                if not isinstance(markers, list) or any(not isinstance(x, str) or not x.strip() for x in markers):
                    raise WorkflowError("approved render markers must be nonempty strings")
                request["render_text"] = markers
                if scene["source_type"] == "ALIYUN::ROCKETMQ::Instance" and scene["mode"] == "open":
                    from scripts.web.e2e.resource_link_workflow.execution_gate import reference_observations

                    proof_ids = {p["tool_use_id"] for p in reference_observations(
                        self.root / "execution-gate.sqlite3", f"{original_job}:{scene['dimension']}",
                        arguments["session_id"],
                    ) if p["account"] == binding["account"]}
                    # A retained full session contains prior cleanup inventory
                    # results too. Only the current dimension's actual wire
                    # tool can supply this click's readonly notice identity.
                    notice_tools = {k: v for k, v in generated_value.get("messages", {}).get("tools", {}).items()
                                    if k in proof_ids}
                    notice = console_sources.bind_rocketmq_notice_source(
                        {"kind": "rocketmq_instance_notice", "resource_type": scene["source_type"],
                         "identifier": request["source_identifier"], "region": binding["region"],
                         "account": binding["account"], "name": markers[-1], "dimension": scene["dimension"]},
                        references, notice_tools,
                    )
                    request["console_inspection"] = notice
                inspection = console_inspection(binding, scene["source_type"], request["render_identifier"])
                if inspection:
                    if inspection["kind"] == "sae_application_metadata":
                        inspection = console_sources.bind_sae_application_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "nas_mount_target_address":
                        inspection = console_sources.bind_nas_mount_target_source(
                            inspection, signed_references, generated_value.get("messages", {}).get("tools", {}),
                        )
                        if (binding.get("dynamic_render_binding") == "nas_managed_mount_target"
                                and inspection["vswitch_id"] != binding.get("vswitch_id")):
                            raise WorkflowError("managed NAS own mount row differs from approved existing switch")
                    if inspection["kind"] == "sls_project_metadata":
                        inspection = console_sources.bind_sls_project_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "ots_vpc_binding_row":
                        inspection = console_sources.bind_ots_vpc_binding_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "nas_protocol_service_identity":
                        inspection = console_sources.bind_nas_protocol_service_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "nas_fileset_description":
                        inspection = console_sources.bind_nas_fileset_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "nas_dataflow_metadata":
                        inspection = console_sources.bind_nas_dataflow_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "ots_instance_endpoint":
                        inspection = console_sources.bind_ots_instance_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "nas_protocol_mount_target_child":
                        inspection = bind_nas_export_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "oos_state_configuration_list":
                        inspection = console_sources.bind_oos_state_configuration_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "oos_template_metadata":
                        inspection = bind_oos_template_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "ess_scheduled_task_metadata":
                        if scene["mode"] != "open":
                            raise WorkflowError("ESS task metadata inspection is only reviewed for direct open")
                        inspection = bind_ess_scheduled_task_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "router_interface_vbr_child":
                        inspection = bind_router_interface_source(
                            inspection, references, generated_value.get("messages", {}).get("tools", {}),
                        )
                    if inspection["kind"] == "resource_group_metadata" and not any(
                        reference.get("identifier") == inspection["identifier"]
                        and reference.get("resourceType") == scene["source_type"]
                        and (reference.get("attributes") or {}).get("Id") == inspection["identifier"]
                        and (reference.get("attributes") or {}).get("Name") == inspection["name"]
                        and (reference.get("attributes") or {}).get("DisplayName") == inspection["display_name"]
                        for reference in references
                    ):
                        raise WorkflowError("ResourceGroup metadata fields differ from own signed product source")
                    request["console_inspection"] = inspection
        result = self.browser(request)
        if operation in {"continue-read", "continue-execution"}:
            json_write(continuation, {**read_json(continuation), "state": "submitted"}, private=True)
        if operation == "new-session":
            session = result.get("session_id")
            previous = read_json(Path(context["output_dir"]) / "new-session-intent.json")["previous_session"]
            if not session or session == previous:
                raise WorkflowError("UI did not materialize a new complete active session")
            json_write(session_file, {"job_id": job_id, "target": slot["target"], "origin": slot["origin"],
                                      "session_id": session})
        if operation in {"source", "cleanup-source"}:
            result["source_evidence"] = Path(result["source_evidence"]).relative_to(self.root).as_posix()
            result["sha256"] = digest(contained(self.root, result["source_evidence"]))
            if operation == "cleanup-source":
                result["original_cleanup_responses"] = wire_evidence.cleanup_execution(
                    self.root / "execution-gate.sqlite3", original_job, arguments["dimension"],
                    arguments["session_id"],
                )
                result["absence_verified"] = False
                result["eligible_for_automatic_verdict"] = False
                if not recovery and not native:
                    result["dimension_transition"] = self.dimension_transition_admission(job_id, packet)
        if operation in {"click", "evidence"}:
            result.update(
                scene=scene["id"],
                source_evidence=binding["source_evidence"],
                source_sha256=binding["source_sha256"],
                account=binding["account"],
                region=binding["region"],
            )
            if navigation:
                result["navigation_recovery"] = {
                    "original_job_id": original_job, "evidence": navigation["evidence"],
                    "evidence_sha256": navigation["evidence_sha256"], "cloud_calls": 0,
                }
            if operation == "click":
                result["source_identifier"] = request.get("source_identifier", request["render_identifier"])
                result["navigation_identifier"] = request["render_identifier"]
            elif scene["mode"] == "creation":
                result["creation_source_verified"] = request["creation_source_verified"]
                result["eligible_for_automatic_verdict"] = False
            result["screenshot"] = Path(result["screenshot"]).resolve().relative_to(self.root).as_posix()
            supplemental = result.get("console_inspection", {}).get("screenshot")
            if supplemental:
                result["console_inspection"]["screenshot"] = Path(supplemental).resolve().relative_to(self.root).as_posix()
            for capture in result.get("console_inspection", {}).get("supplemental_captures", []):
                capture["screenshot"] = Path(capture["screenshot"]).resolve().relative_to(self.root).as_posix()
                capture["sha256"] = digest(contained(self.root, capture["screenshot"]))
            raw = Path(context["output_dir"]) / f"{scene['id']}.json"
            json_write(raw, result)
            self.store.record_observation(job_id, token, result)
        return result

    def queue_preserved_repair(self, task_id: str, evidence: str, reason: str) -> dict[str, Any]:
        """OPS-only product repair admission; never installs or renews cloud capability."""
        proof = read_json(contained(self.root / "inputs", evidence))
        if (proof.get("schema_version") != 1 or proof.get("kind") != "ops_preserved_product_gap"
                or proof.get("task_id") != task_id or not proof.get("diagnosis")
                or proof.get("formal_test") is not False or proof.get("pass_added") is not False
                or not proof.get("sources") or not proof.get("code_evidence")):
            raise WorkflowError("preserved repair needs exact task, diagnosis and immutable original/code evidence")
        for source in proof["sources"]:
            source_path = contained(self.root, source["path"])
            if source_path.relative_to(self.root).parts[0] not in {"inputs", "jobs"}:
                raise WorkflowError("preserved repair source must be original input or job artifact")
            if digest(source_path) != source.get("sha256"):
                raise WorkflowError("preserved repair original source changed")
        repo = Path(self.config["repo"]).resolve()
        for source in proof["code_evidence"]:
            source_path = contained(repo, source["path"])
            if (source_path.relative_to(repo).parts[0] not in {"src", "tests"}
                    or not source.get("fact") or digest(source_path) != source.get("sha256")):
                raise WorkflowError("preserved repair requires current product source/tests evidence")
        self.store.queue_preserved_repair(task_id, evidence, reason)
        return {"preserved_repair_queued": task_id, "no_glm_dispatched": True,
                "ownership_and_wire_unchanged": True, "pass_count_unchanged": True}

    def queue_preparation_repair(self, task_id: str, evidence: str, reason: str) -> dict[str, Any]:
        """Validate immutable OPS defect input, then use the existing single-Sol repair queue."""
        path = contained(self.root / "inputs", evidence)
        proof = read_json(path)
        if (
            proof.get("schema_version") != 1 or proof.get("kind") != "preparation_product_gap"
            or proof.get("task_id") != task_id or not proof.get("diagnosis")
            or not proof.get("sources") or not proof.get("code_evidence")
        ):
            raise WorkflowError("preparation repair needs exact task, diagnosis, original sources and code evidence")
        for source in proof["sources"]:
            source_path = contained(self.root / "inputs", source["path"])
            if digest(source_path) != source.get("sha256"):
                raise WorkflowError("preparation source changed; independently re-review its original evidence")
        repo = Path(self.config["repo"]).resolve()
        for source in proof["code_evidence"]:
            source_path = contained(repo, source["path"])
            if source_path.relative_to(repo).parts[0] not in {"src", "tests"} or not source.get("fact"):
                raise WorkflowError("product gap must identify product source/tests, not controller or ledger")
            if digest(source_path) != source.get("sha256"):
                raise WorkflowError("product preimage changed; independently re-review the defect")
        self.store.queue_preparation_repair(task_id, evidence, reason)
        return {"preparation_repair_queued": task_id, "pass_count_unchanged": True, "no_glm_dispatched": True}

    def attest_control(self) -> dict[str, Any]:
        """Report this interpreter's import-time code, not a later disk-only hash."""
        from . import retained_capture

        retained_source = Path(retained_capture.__file__).resolve()
        if digest(retained_source) != retained_capture.LOADED_SHA:
            raise WorkflowError("retained capture review changed on disk; restart the same Server")
        source = Path(__file__).resolve()
        if digest(source) != RUNTIME_LOADED_SHA:
            raise WorkflowError("control runtime changed on disk; restart the same Server before model operations")
        creation_source = Path(creation.__file__).resolve()
        if digest(creation_source) != creation.CREATION_LOADED_SHA:
            raise WorkflowError("creation review code changed on disk; restart the same Server")
        additional_source_path = Path(additional_source.__file__).resolve()
        if digest(additional_source_path) != additional_source.LOADED_SHA:
            raise WorkflowError("additional source review code changed on disk; restart the same Server")
        console_sources_path = Path(console_sources.__file__).resolve()
        if digest(console_sources_path) != console_sources.LOADED_SHA:
            raise WorkflowError("console source binding code changed on disk; restart the same Server")
        recovery_source = Path(cleanup_recovery.__file__).resolve()
        if digest(recovery_source) != cleanup_recovery.LOADED_SHA:
            raise WorkflowError("cleanup review code changed on disk; restart the same Server")
        scheduler_source = Path(store_module.__file__).resolve()
        if digest(scheduler_source) != store_module.STORE_LOADED_SHA:
            raise WorkflowError("scheduler code changed on disk; restart the same Server")
        hourly_source = Path(hourly_budget.__file__).resolve()
        if digest(hourly_source) != hourly_budget.LOADED_SHA:
            raise WorkflowError("hourly fee review code changed on disk; restart the same Server")
        phases_source = Path(phases.__file__).resolve()
        if digest(phases_source) != phases.LOADED_SHA:
            raise WorkflowError("execution phase code changed on disk; restart the same Server")
        delay_source = Path(delays.__file__).resolve()
        if digest(delay_source) != delays.LOADED_SHA:
            raise WorkflowError("worker wait code changed on disk; restart the same Server")
        ownership_source = Path(ownership.__file__).resolve()
        if digest(ownership_source) != ownership.LOADED_SHA:
            raise WorkflowError("ownership generation code changed on disk; restart the same Server")
        wire_source = Path(wire_evidence.__file__).resolve()
        if digest(wire_source) != wire_evidence.LOADED_SHA:
            raise WorkflowError("cleanup wire evidence code changed on disk; restart the same Server")
        selection_source = Path(selection.__file__).resolve()
        if digest(selection_source) != selection.LOADED_SHA:
            raise WorkflowError("execution selection code changed on disk; restart the same Server")
        reports_source = Path(reports.__file__).resolve()
        if digest(reports_source) != reports.LOADED_SHA:
            raise WorkflowError("results renderer code changed on disk; restart the same Server")
        hosts_source = Path(worker_hosts.__file__).resolve()
        if digest(hosts_source) != worker_hosts.LOADED_SHA:
            raise WorkflowError("worker host code changed on disk; restart the same Server")
        plans_source = Path(plans.__file__).resolve()
        if digest(plans_source) != plans.LOADED_SHA:
            raise WorkflowError("frozen plan scope code changed on disk; restart the same Server")
        remaining_source = Path(remaining_review.__file__).resolve()
        if digest(remaining_source) != remaining_review.LOADED_SHA:
            raise WorkflowError("remaining review provenance code changed on disk; restart the same Server")
        handoff_source = Path(plan_handoff.__file__).resolve()
        if digest(handoff_source) != plan_handoff.LOADED_SHA:
            raise WorkflowError("plan handoff code changed on disk; restart the same Server")
        sample_source = Path(temporary_samples.__file__).resolve()
        if digest(sample_source) != temporary_samples.LOADED_SHA:
            raise WorkflowError("temporary sample admission code changed on disk; restart the same Server")
        additional_nas_source = Path(additional_nas.__file__).resolve()
        if digest(additional_nas_source) != additional_nas.LOADED_SHA:
            raise WorkflowError("additional NAS admission code changed on disk; restart the same Server")
        trial_source = Path(provider_trial.__file__).resolve()
        if digest(trial_source) != provider_trial.LOADED_SHA:
            raise WorkflowError("provider trial review code changed on disk; restart the same Server")
        mse_source = Path(mse_correction.__file__).resolve()
        if digest(mse_source) != mse_correction.LOADED_SHA:
            raise WorkflowError("MSE corrective-source review changed on disk; restart the same Server")
        nas_rejection_source = Path(nas_rejection.__file__).resolve()
        if digest(nas_rejection_source) != nas_rejection.LOADED_SHA:
            raise WorkflowError("NAS rejected-order review changed on disk; restart the same Server")
        nas_correction_source = Path(nas_correction.__file__).resolve()
        if digest(nas_correction_source) != nas_correction.LOADED_SHA:
            raise WorkflowError("NAS corrective-operation review changed on disk; restart the same Server")
        native_retry_source = Path(native_retry.__file__).resolve()
        existing_query_source = Path(existing_query.__file__).resolve()
        if digest(existing_query_source) != existing_query.LOADED_SHA:
            raise WorkflowError("existing query review changed on disk; restart the same Server")
        if digest(native_retry_source) != native_retry.LOADED_SHA:
            raise WorkflowError("native retry review changed on disk; restart the same Server")
        worker_hosts.assert_selection(self)
        return {"pid": os.getpid(), "identity": self.process_identity(os.getpid()),
                "path": str(source), "loaded_sha256": RUNTIME_LOADED_SHA, "disk_matches_loaded": True,
                "retained_capture": {"path": str(retained_source), "loaded_sha256": retained_capture.LOADED_SHA,
                                     "disk_matches_loaded": True},
                "creation_review": {"path": str(creation_source), "loaded_sha256": creation.CREATION_LOADED_SHA,
                                    "disk_matches_loaded": True},
                "additional_source_review": {"path": str(additional_source_path),
                                             "loaded_sha256": additional_source.LOADED_SHA,
                                             "disk_matches_loaded": True},
                "console_source_binding": {"path": str(console_sources_path),
                                           "loaded_sha256": console_sources.LOADED_SHA,
                                           "disk_matches_loaded": True},
                "cleanup_review": {"path": str(recovery_source), "loaded_sha256": cleanup_recovery.LOADED_SHA,
                                   "disk_matches_loaded": True},
                "scheduler": {"path": str(scheduler_source), "loaded_sha256": store_module.STORE_LOADED_SHA,
                              "disk_matches_loaded": True},
                "hourly_fee_review": {"path": str(hourly_source), "loaded_sha256": hourly_budget.LOADED_SHA,
                                      "disk_matches_loaded": True},
                "execution_phases": {"path": str(phases_source), "loaded_sha256": phases.LOADED_SHA,
                                     "disk_matches_loaded": True},
                "worker_wait": {"path": str(delay_source), "loaded_sha256": delays.LOADED_SHA,
                                "disk_matches_loaded": True},
                "ownership_generation": {"path": str(ownership_source), "loaded_sha256": ownership.LOADED_SHA,
                                         "disk_matches_loaded": True},
                "cleanup_wire_evidence": {"path": str(wire_source), "loaded_sha256": wire_evidence.LOADED_SHA,
                                          "disk_matches_loaded": True},
                "execution_selection": {"path": str(selection_source), "loaded_sha256": selection.LOADED_SHA,
                                        "disk_matches_loaded": True},
                "results_renderer": {"path": str(reports_source), "loaded_sha256": reports.LOADED_SHA,
                                     "disk_matches_loaded": True},
                "worker_hosts": {"path": str(hosts_source), "loaded_sha256": worker_hosts.LOADED_SHA,
                                 "disk_matches_loaded": True},
                "frozen_plan_scope": {"path": str(plans_source), "loaded_sha256": plans.LOADED_SHA,
                                      "disk_matches_loaded": True},
                "remaining_review": {"path": str(remaining_source), "loaded_sha256": remaining_review.LOADED_SHA,
                                     "disk_matches_loaded": True},
                "plan_handoff": {"path": str(handoff_source), "loaded_sha256": plan_handoff.LOADED_SHA,
                                 "disk_matches_loaded": True},
                "temporary_samples": {"path": str(sample_source), "loaded_sha256": temporary_samples.LOADED_SHA,
                                      "disk_matches_loaded": True},
                "additional_nas": {"path": str(additional_nas_source), "loaded_sha256": additional_nas.LOADED_SHA,
                                   "disk_matches_loaded": True},
                "provider_trial": {"path": str(trial_source), "loaded_sha256": provider_trial.LOADED_SHA,
                                   "disk_matches_loaded": True},
                "mse_correction": {"path": str(mse_source), "loaded_sha256": mse_correction.LOADED_SHA,
                                   "disk_matches_loaded": True},
                "nas_rejection": {"path": str(nas_rejection_source), "loaded_sha256": nas_rejection.LOADED_SHA,
                                  "disk_matches_loaded": True},
                "nas_correction": {"path": str(nas_correction_source), "loaded_sha256": nas_correction.LOADED_SHA,
                                  "disk_matches_loaded": True},
                "existing_query": {"path": str(existing_query_source), "loaded_sha256": existing_query.LOADED_SHA,
                                   "disk_matches_loaded": True},
                "native_retry": {"path": str(native_retry_source), "loaded_sha256": native_retry.LOADED_SHA,
                                 "disk_matches_loaded": True}}

    def proven_references(self, job_id, dimension, session, account, references):
        from scripts.web.e2e.resource_link_workflow.execution_gate import reference_observations

        proofs = reference_observations(self.root / "execution-gate.sqlite3", f"{job_id}:{dimension}", session)
        return [reference for reference in references if any(
            proof["account"] == account and proof["reference_scope"] == reference.get("accountScope")
            and reference_region_matches(reference, proof["region"], proof["tool_use_id"])
            and any(source.get("toolUseId") == proof["tool_use_id"] and source.get("evidence") == "api_response"
                    for source in reference.get("sources", []))
            for proof in proofs
        )]

    def native_nas_query_references(self, job_id, session, account, references):
        from scripts.web.e2e.resource_link_workflow.execution_gate import GateDeniedError, nas_native_navigation_source

        try:
            own = nas_native_navigation_source(
                self.root / "execution-gate.sqlite3", f"{job_id}:QUERY", session, account,
            )
        except GateDeniedError as error:
            raise WorkflowError(
                "NAS QUERY click needs its own healthy signed DataFlow and complete dependencies"
            ) from error
        return [reference for reference in references
                if reference.get("resourceType") == "ALIYUN::NAS::DataFlow"
                and reference.get("identifier") == own["identifier"]
                and reference.get("accountScope") == own["reference_scope"]
                and reference_region_matches(reference, own["region"], own["tool_use_id"])
                and any(source.get("toolUseId") == own["tool_use_id"]
                        and source.get("evidence") == "api_response" and source.get("product") == "nas"
                        and source.get("action") == "DescribeDataFlows" for source in reference.get("sources", []))]

    def created_references(
        self, job_id: str, dimension: str, session: str, references: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Bind selection to this dimension's actual wire creation, not an earlier chat reference."""
        from scripts.web.e2e.resource_link_workflow.execution_gate import creations

        records = [
            record
            for record in creations(self.root / "execution-gate.sqlite3", [job_id])
            if record["dimension"] == dimension and record["session_id"] == session and record["state"] == "succeeded"
            and record.get("managed_child") is not True
        ]
        if len(records) != 1 or not records[0]["resource"]["id"]:
            raise WorkflowError("one successful original wire creation is required for this dimension")
        created = records[0]["resource"]
        def original_signer_or_reviewed_successor(reference):
            if reference.get("accountScope") == (records[0].get("reference_scope") or created["account"]):
                return True
            if dimension != "TF" or created["product"] != "ros":
                return False
            # Source calls still belong to the original creation scope. A
            # separately reviewed STS successor must not erase that creation,
            # nor may an arbitrary same-account reference inherit ownership.
            from scripts.web.e2e.resource_link_workflow import execution_gate as gate

            scope = f"{job_id}:{dimension}"
            if not (self.root / "execution-gate.sqlite3").is_file() or "attempt_id" not in records[0]:
                return False
            with gate.database(self.root / "execution-gate.sqlite3") as wire:
                active = wire.execute("SELECT * FROM active WHERE scope=? AND session=?", (scope, session)).fetchone()
                parent = wire.execute("SELECT * FROM attempts WHERE id=?", (records[0]["attempt_id"],)).fetchone()
                if not active or not parent:
                    return False
                for source in reference.get("sources", []):
                    if source.get("evidence") != "api_response":
                        continue
                    for attempt in wire.execute("SELECT * FROM attempts WHERE scope=? AND session=? "
                                                "AND tool_use_id=? AND state='succeeded'", (
                            scope, session, source.get("toolUseId"))):
                        grant = wire.execute("SELECT * FROM grants WHERE scope=? AND id=?",
                                             (scope, attempt["grant_id"])).fetchone()
                        if (attempt["reference_scope"] == reference.get("accountScope")
                                and gate.mse_retained_identity(wire, active, grant, parent,
                                    attempt["credential"], attempt["reference_scope"], before=attempt["created"])):
                            return True
            return False

        return [
            reference
            for reference in references
            if original_signer_or_reviewed_successor(reference)
            and reference_region_matches(reference, created["region"])
            and (
                reference.get("stackId") == created["id"]
                if created["product"] == "ros"
                else reference.get("identifier") == created["id"]
            )
        ]

    def sol_tool(self, context: dict[str, Any], token: str, operation: str, arguments: dict[str, Any]) -> Any:
        job_id = context["job_id"]
        if operation in {"read_artifact", "write_verification"}:
            # Review needs original worker JSON, not only screenshots or a worker summary.
            with self.store.transaction() as connection:
                outputs = [
                    self.root / "jobs" / row[0] / "output"
                    for row in connection.execute("SELECT id FROM jobs WHERE task_id=?", (context["task_id"],))
                ]
            if operation == "read_artifact":
                path = contained(self.root, arguments["path"])
                if not any(path.is_relative_to(folder) for folder in outputs) or path.suffix != ".json":
                    raise WorkflowError("only this resource's original output JSON may be read")
                if path.stat().st_size > 2_000_000:
                    raise WorkflowError("artifact too large; use bounded original evidence")
                value = read_json(path)
                pointer = arguments.get("pointer", "")
                if pointer:
                    if not isinstance(pointer, str) or not pointer.startswith("/"):
                        raise WorkflowError("JSON pointer must start with slash")
                    for part in pointer[1:].split("/"):
                        key = part.replace("~1", "/").replace("~0", "~")
                        value = value[int(key)] if isinstance(value, list) else value[key]
                return {"text": json.dumps(value, ensure_ascii=False), "sha256": digest(path), "pointer": pointer}
            if context["purpose"] != "review":
                raise WorkflowError("verification evidence is a Sol review operation")
            value = arguments["value"]
            source = contained(self.root, value["source_evidence"])
            if not source.is_relative_to(self.root / "inputs") and not any(
                source.is_relative_to(folder) for folder in outputs
            ):
                raise WorkflowError("verification must cite this resource's original evidence")
            if digest(source) != value["source_sha256"]:
                raise WorkflowError("verification original source SHA mismatch")
            name = arguments["id"]
            if not isinstance(name, str) or not name.replace("-", "").isalnum():
                raise WorkflowError("invalid verification identity")
            path = self.root / "jobs" / job_id / "output" / f"verification-{name}.json"
            if path.exists():
                raise WorkflowError("verification evidence is immutable")
            json_write(path, value)
            return {"path": path.relative_to(self.root).as_posix(), "sha256": digest(path)}
        if operation == "transport_evidence":
            # Read-only, original bounded events for this resource. Do not expose
            # capability files, arbitrary jobs or streaming reasoning/config.
            target_job = arguments["job_id"]
            with self.store.transaction() as connection:
                owner = connection.execute("SELECT task_id FROM jobs WHERE id=?", (target_job,)).fetchone()
            if not owner or owner["task_id"] != context["task_id"]:
                raise WorkflowError("transport evidence must belong to this resource")
            path = contained(self.root, f"jobs/{target_job}/raw-rollout.jsonl")
            events = []
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                event = json.loads(line)
                method = event.get("method", "")
                item = event.get("params", {}).get("item", {})
                rejected = method == "item/completed" and item.get("tool") == "resource_workflow" and any(
                    content.get("text") == "Unsupported dynamic tool namespace: null"
                    for content in item.get("contentItems", []) or []
                )
                if method.startswith(("transport/", "script/")) or method == "turn/completed" or rejected:
                    events.append({"line": line_number, "event": event})
            return {"source": path.relative_to(self.root).as_posix(), "sha256": digest(path), "events": events}
        if operation in {"read_code", "search_code"}:
            if context["purpose"] not in {"prepare", "repair"}:
                raise WorkflowError("source inspection is not an acceptance substitute")
            repo = Path(self.config["repo"]).resolve()
            if operation == "read_code":
                path = contained(repo, arguments["path"])
                framework = repo / "scripts/web/e2e/resource_link_workflow"
                allowed = path.relative_to(repo).parts[0] in {"src", "tests"} or (
                    path.is_relative_to(framework) and path.suffix in {".py", ".mjs"}
                )
                if not allowed or path.stat().st_size > 2_000_000:
                    raise WorkflowError("read_code only bounded product/test or read-only workflow implementation")
                return {"text": path.read_text(encoding="utf-8"), "sha256": digest(path)}
            result = subprocess.run(
                ["rg", "-n", "--max-count", "100", "--", arguments["pattern"], "src", "tests"],
                cwd=repo,
                capture_output=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
            return {"output": result.stdout[:32000], "exit_code": result.returncode}
        if operation in {"read", "write"}:
            relative = arguments["path"]
            path = contained(self.root / "inputs", relative)
            if operation == "read":
                if path.stat().st_size > 2_000_000:
                    raise WorkflowError("input too large; use bounded files")
                return {"text": path.read_text(encoding="utf-8"), "sha256": digest(path)}
            if context["purpose"] not in {"prepare", "repair"}:
                from .screenshot_review import validate

                validate(self.root, self.store, context, relative, arguments["text"])
            if path.exists():
                raise WorkflowError("raw inputs are immutable; choose a new path")
            if context["purpose"] == "review":
                json_write(path, json.loads(arguments["text"]), private=True)
            else:
                atomic_write(path, arguments["text"])
            return {"path": path.relative_to(self.root).as_posix(), "sha256": digest(path)}
        if operation == "approve":
            self.store.approve_packet(job_id, token, arguments["packet"])
            return {"staged": True}
        if operation == "resolve":
            self.store.resolve_dimensions(job_id, token, arguments["dimensions"])
            return {"resolved": True}
        if operation == "register_owned":
            if context["purpose"] != "review":
                raise WorkflowError("ownership extraction is verified by Sol against raw product response")
            evidence = arguments.get("evidence")
            expected_sha = None
            if isinstance(evidence, dict):
                if (
                    set(evidence) != {"path", "sha256"}
                    or not isinstance(evidence.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"])
                ):
                    raise WorkflowError("register_owned evidence object requires exactly path and SHA256")
                expected_sha = evidence["sha256"]
                evidence = evidence.get("path")
            if not isinstance(evidence, str) or not evidence.strip():
                raise WorkflowError("register_owned evidence requires a path string or a path/SHA256 object")
            resource = arguments.get("resource")
            dimension = arguments.get("dimension")
            if (
                not isinstance(resource, dict)
                or not all(isinstance(resource.get(key), str) and resource[key].strip() for key in ("id", "region"))
                or not isinstance(dimension, str) or dimension not in {"ROS", "TF", "CREATE"}
            ):
                raise WorkflowError("register_owned requires ROS/TF/CREATE and a resource object with ID and region")
            # Register before creation acceptance; it does not prove successful cleanup.
            with self.store.transaction() as connection:
                source = contained(self.root, evidence)
                if not source.is_file():
                    raise WorkflowError("original creation response required")
                source_sha = digest(source)
                if expected_sha is not None and source_sha != expected_sha:
                    raise WorkflowError("register_owned original evidence SHA256 changed")
                binding = {"path": evidence, "sha256": source_sha}
                generation = ownership.evidence_generation(connection, self.root, context["task_id"], binding)
                if generation is None:
                    raise WorkflowError("original creation evidence must bind an exact GLM generation of this task")
                identity = ownership.register_generation(
                    connection, self.root, context["task_id"], dimension, resource, generation, binding,
                )
                self.store.event(connection, "owned_resource_verified", {"ownership_id": identity})
            return {"ownership_id": identity}
        if operation == "cloud_read":
            # Preparation queries must be frozen in the resource plan, not improvised scans.
            query = next(
                (item for item in context["resource"].get("queries", []) if item["id"] == arguments["id"]), None
            )
            if not query or not preparation_read_api(query["product"], query["action"]):
                raise WorkflowError("query must be an approved named read-only API in the plan")
            if not isinstance(query["id"], str) or not query["id"].replace("-", "").isalnum():
                raise WorkflowError("invalid bounded query identity")
            evidence = self.root / "inputs" / job_id / f"query-{query['id']}.json"
            intent = evidence.with_suffix(".intent.json")
            with exclusive(self.root / "cloud-read.lock"):
                if intent.exists() or evidence.exists():
                    raise WorkflowError("bounded query already executed or unknown; reuse original evidence")
                json_write(intent, {"query": query, "state": "may_have_executed"})
            argv = ["aliyun", query["product"], query["action"], "--profile", "resource-link-test"]
            for key, value in query.get("parameters", {}).items():
                if not key.isalnum() or not key[0].isupper() or not isinstance(value, (str, int)):
                    raise WorkflowError("invalid API parameter")
                argv.extend(["--" + key, str(value)])
            try:
                from .credentials import cli_snapshot

                with cli_snapshot(self.root) as private_config:
                    if private_config is not None:
                        argv.extend(["--config-path", str(private_config)])
                    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=40,
                                            check=False)
                exit_code = result.returncode
                response = json.loads(result.stdout) if exit_code == 0 else None
                error_kind = "api_error" if exit_code else None
            except (subprocess.TimeoutExpired, OSError, ValueError) as error:
                exit_code, response, error_kind = -1, None, type(error).__name__
            json_write(
                evidence,
                {
                    "query": query,
                    "exit_code": exit_code,
                    "response": response,
                    "failure_blind_spot": exit_code != 0,
                    "error_kind": error_kind,
                },
            )
            return {
                "path": evidence.relative_to(self.root).as_posix(),
                "sha256": digest(evidence),
                "exit_code": exit_code,
            }
        if operation == "register_query":
            if context["purpose"] not in {"prepare", "repair"}:
                raise WorkflowError("query preparation is Sol only")
            query = arguments["query"]
            basis = contained(self.root, query["official_evidence"])
            candidates = context["resource"].get("query_candidates", [])
            existing_ros_read = (
                bool({"ROS", "TF"} & plans.readonly_retest_dimensions(context["resource"]))
                and query.get("product") == "ros"
                and query.get("action") in {"GetStack", "GetStackResource", "ListStackResources"}
                and isinstance(query.get("parameters", {}).get("StackId"), str)
                and bool(query["parameters"]["StackId"].strip())
                and not query["parameters"]["StackId"].startswith("$")
            )
            if (
                not basis.is_file()
                or not (existing_ros_read or any(
                    item["product"] == query["product"] and item["action"] == query["action"] for item in candidates
                ))
                or not preparation_read_api(query["product"], query["action"])
            ):
                raise WorkflowError("query must be a resource-specific reviewed candidate with official evidence")
            if not query["id"].replace("-", "").isalnum():
                raise WorkflowError("invalid query identity")
            with self.store.transaction() as connection:
                spec = context["resource"]
                old = next((item for item in spec.get("queries", []) if item["id"] == query["id"]), None)
                if old and old != query:
                    raise WorkflowError("query inputs are immutable; choose a new preparation ID")
                if not old:
                    spec.setdefault("queries", []).append(query)
                from .store import dumps

                connection.execute("UPDATE tasks SET spec=? WHERE id=?", (dumps(spec), context["task_id"]))
                self.store.event(
                    connection, "bounded_query_registered", {"task_id": context["task_id"], "id": query["id"]}
                )
            return {"registered": query["id"]}
        if operation == "inspect":
            result = self.store.inspected(job_id, token, arguments["path"])
            pixels = contained(self.root, result["path"]).read_bytes()
            if hashlib.sha256(pixels).hexdigest() != result["sha256"]:
                raise WorkflowError("inspected pixels changed before delivery; independently recheck original evidence")
            response = {
                "image_url": "data:image/png;base64,"
                + base64.b64encode(pixels).decode(),
                "sha256": result["sha256"],
            }
            if result.get("evidence_role") == "diagnostic_only":
                response.update(evidence_role="diagnostic_only", eligible_for_verdict=False)
            return response
        if operation == "accept":
            self.store.accept(job_id, token, arguments["receipt"])
            return {"staged": True}
        if operation == "block":
            self.store.block(job_id, token, arguments["reason"], arguments.get("handoff"))
            return {"staged": True}
        if operation == "escalate_repair":
            self.store.escalate_repair(job_id, token, arguments["reason"])
            return {"staged": True, "product_repair_queued": True, "cloud_calls_added": False}
        if operation == "classify_preparation":
            self.store.classify_preparation(job_id, token, arguments.get("coverage"))
            return {"staged": True, "no_glm_dispatched": True}
        if operation == "repair_file":
            if context["purpose"] != "repair":
                raise WorkflowError("only the single Sol repair job can modify product files")
            repo = Path(self.config["repo"]).resolve()
            path = contained(repo, arguments["path"])
            if path.relative_to(repo).parts[0] not in {"src", "tests"} or path.suffix not in {
                ".py",
                ".js",
                ".css",
                ".html",
                ".json",
            }:
                raise WorkflowError("repair is limited to product source/tests; no controller or ledger writes")
            with self.store.transaction() as connection:
                job = self.store.authorized(connection, job_id, token)
                task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
                safety = self.store.repair_safety(connection, job, task)
                if not safety["product_write_allowed"]:
                    raise WorkflowError("bound single Sol repair barrier and drained exact GLM turns required")
                if digest(path) != arguments["before_sha256"]:
                    raise WorkflowError("repair preimage changed; preserve concurrent user edits")
                atomic_write(path, arguments["text"])
            return {"sha256": digest(path), "restart_required": True}
        if operation == "offline_test":
            argv = arguments["argv"]
            node_test = argv == ["node", "--test", "tests/web/resource_link_workflow.test.mjs"]
            if (
                not isinstance(argv, list)
                or (not node_test and argv[:3] not in [["uv", "run", "pytest"], ["uv", "run", "ruff"]])
                or any(not isinstance(item, str) for item in argv)
            ):
                raise WorkflowError("only offline pytest/ruff or the exact workflow Node test is permitted; no shell")
            if context["purpose"] != "repair" and not (node_test and context["purpose"] == "prepare"):
                raise WorkflowError("offline test is a repair operation")
            pytest_test = argv[:3] == ["uv", "run", "pytest"]
            if pytest_test:
                # Pytest executes code and can write reports: it is not an input-repair read.
                # Keep output/config/plugin switches out of the model-controlled argv.
                flags = {"-q", "-v", "-vv", "-x", "--exitfirst", "--disable-warnings", "--no-header", "--collect-only"}
                values = {"-k", "-m", "-n", "--maxfail", "--tb"}
                repo = Path(self.config["repo"]).resolve()
                has_test_path = False
                arguments_left = iter(argv[3:])
                for item in arguments_left:
                    option, separator, _ = item.partition("=")
                    if option in values:
                        if not separator and next(arguments_left, None) is None:
                            raise WorkflowError("offline pytest option needs a value")
                    elif item not in flags:
                        path = contained(repo, item.split("::", 1)[0])
                        if item.startswith("-") or path.relative_to(repo).parts[:1] != ("tests",):
                            raise WorkflowError("offline pytest permits only test paths and read-only test options")
                        has_test_path = True
                argv = [*argv[:3], "-o", "addopts=", *argv[3:], *([] if has_test_path else ["tests/"])]
            if argv[:3] == ["uv", "run", "ruff"]:
                if argv[3:4] != ["check"] or any(
                    item.split("=", 1)[0] in {"--fix", "--fix-only", "--unsafe-fixes", "--add-noqa", "--output-file"}
                    or item.startswith("-o")
                    for item in argv[4:]
                ):
                    raise WorkflowError(
                        "offline ruff must be a read-only check; use the product repair barrier to edit"
                    )
                # Override fix=true in project/user configuration as well.
                argv = [*argv[:4], "--no-fix", "--no-unsafe-fixes", *argv[4:]]
            if node_test:
                argv = [self.config["node"], *argv[1:]]
            with self.store.transaction() if pytest_test else nullcontext() as connection:
                if pytest_test:
                    job = self.store.authorized(connection, job_id, token)
                    task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
                    if not self.store.repair_safety(connection, job, task)["product_write_allowed"]:
                        raise WorkflowError("offline pytest requires the bound product repair barrier and drained GLM")
                result = subprocess.run(
                    argv,
                    cwd=self.config["repo"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=180,
                    check=False,
                    **({"env": {**os.environ, "PYTEST_ADDOPTS": ""}} if pytest_test else {}),
                )
            return {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-24000:]}
        raise WorkflowError("Sol operation not allowed")
