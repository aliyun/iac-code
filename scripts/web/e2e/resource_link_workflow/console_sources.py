"""Narrow native read-only capture bindings; callers provide wire-proven references."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .files import WorkflowError, digest

LOADED_SHA = digest(Path(__file__))


def bind_rocketmq_notice_source(inspection, references, tools):
    """Closing a native announcement never supplies or changes resource identity."""
    if (inspection.get("kind") != "rocketmq_instance_notice"
            or inspection.get("resource_type") != "ALIYUN::ROCKETMQ::Instance"
            or inspection.get("dimension") not in {"ROS", "QUERY"}
            or not re.fullmatch(r"[0-9]{16}", str(inspection.get("account", "")))
            or not re.fullmatch(r"MQ_INST_" + inspection["account"] + r"_[A-Za-z0-9]+",
                                str(inspection.get("identifier", "")))
            or not re.fullmatch(r"cn-[a-z0-9-]+", str(inspection.get("region", "")))
            or not isinstance(inspection.get("name"), str) or not inspection["name"].strip()):
        raise WorkflowError("RocketMQ notice needs exact own signed instance/account/region/name")
    matches = set()
    for ref in references:
        if (ref.get("resourceType") != inspection["resource_type"]
                or ref.get("identifier") != inspection["identifier"] or ref.get("regionId") != inspection["region"]):
            continue
        for source in ref.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (source.get("evidence") != "api_response" or tool.get("toolName") != "aliyun_api"
                    or tool.get("status") != "completed" or request.get("region_id") != inspection["region"]):
                continue
            if inspection["dimension"] == "ROS":
                valid = (request.get("product") == "ros" and request.get("action") == "GetStackResource"
                         and request.get("version") == "2019-09-10" and ref.get("stackId")
                         and request.get("params") == {"RegionId": inspection["region"],
                             "StackId": ref["stackId"], "LogicalResourceId": ref.get("logicalResourceId"),
                             "ShowResourceAttributes": True})
            else:
                params = request.get("params", {})
                valid = (request.get("product") == "ons" and request.get("action") == "OnsInstanceInServiceList"
                         and request.get("version") == "2019-02-14"
                         and params.get("RegionId") == inspection["region"]
                         and set(params) <= {"RegionId", "NeedResourceInfo"})
            if not valid:
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    raw = json.loads(result["content"])
                    if not raw.get("RequestId") or raw.get("Code"):
                        continue
                    if inspection["dimension"] == "ROS":
                        attrs = {a["ResourceAttributeKey"]: a.get("ResourceAttributeValue")
                                 for a in raw["ResourceAttributes"]}
                        if (raw.get("StackId") != ref["stackId"]
                                or raw.get("PhysicalResourceId") != inspection["identifier"]
                                or raw.get("ResourceType") != inspection["resource_type"]
                                or attrs.get("InstanceId") != inspection["identifier"]
                                or attrs.get("InstanceName") != inspection["name"]):
                            continue
                    else:
                        rows = [row for row in raw["Data"]["InstanceVO"]
                                if row.get("InstanceId") == inspection["identifier"]]
                        if (len(rows) != 1 or rows[0].get("InstanceName") != inspection["name"]
                                or rows[0].get("InstanceType") != 1 or rows[0].get("InstanceStatus") != 5):
                            continue
                    matches.add(tool_id)
                except (KeyError, TypeError, ValueError):
                    continue
    if len(matches) != 1:
        raise WorkflowError("RocketMQ native fields need one original signed ROS member or current QUERY row")
    return {**inspection, "source_tool_use_id": matches.pop()}


def bind_nas_dataflow_source(inspection, references, tools):
    """Join native metadata to the exact successful signed QUERY row, not seed attributes."""
    matches = set()
    for ref in references:
        if (ref.get("resourceType") != inspection["resource_type"]
                or ref.get("identifier") != inspection["identifier"] or ref.get("regionId") != inspection["region"]):
            continue
        for source in ref.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (source.get("evidence") != "api_response" or source.get("product") != "nas"
                    or source.get("action") != "DescribeDataFlows"
                    or source.get("requestRegionId") != inspection["region"]
                    or source.get("sourcePath") != "/result/DataFlowInfo/DataFlow/0/DataFlowId"
                    or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                    or request != {"product": "nas", "version": "2017-06-26", "action": "DescribeDataFlows",
                                   "region_id": inspection["region"], "params": {
                                       "FileSystemId": inspection["file_system_id"], "MaxResults": 20,
                                       "Filters.1.Key": "DataFlowIds", "Filters.1.Value": inspection["identifier"]}}):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    raw = json.loads(result["content"])
                    rows = raw["DataFlowInfo"]["DataFlow"]
                except (KeyError, TypeError, ValueError):
                    continue
                if (raw.get("RequestId") and not raw.get("Code") and not raw.get("NextToken")
                        and isinstance(rows, list) and len(rows) == 1
                        and all(rows[0].get(k) == v for k, v in {
                            "DataFlowId": inspection["identifier"], "FileSystemId": inspection["file_system_id"],
                            "FsetId": inspection["fileset_id"], "FileSystemPath": inspection["path"],
                            "SourceStorage": inspection["source_storage"], "Status": "Running", "ErrorMessage": "",
                        }.items())):
                    matches.add(tool_id)
    if len(matches) != 1:
        raise WorkflowError("NAS DataFlow native fields differ from one own signed healthy QUERY row")
    return {**inspection, "source_tool_use_id": matches.pop()}


def bind_oos_state_configuration_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Bind full native ID and amended metadata to one fresh, wire-proven OOS row.

    The caller verifies signed account/session and wire provenance first. The
    preparation response is only a selection seed, never substituted here.
    """
    matches = []
    for reference in references:
        if (
            reference.get("resourceType") != inspection["resource_type"]
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("product") != "oos" or source.get("action") != "ListStateConfigurations"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "oos" or request.get("action") != "ListStateConfigurations"
                or request.get("version") != "2019-06-01" or request.get("region_id") != inspection["region"]
                or request.get("params") != {"RegionId": inspection["region"], "MaxResults": 10}
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    rows = json.loads(result["content"])["StateConfigurations"]
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(rows, list):
                    continue
                own_rows = [row for row in rows if isinstance(row, dict)
                            and row.get("StateConfigurationId") == inspection["identifier"]]
                if len(own_rows) == 1 and all(own_rows[0].get(key) == value for key, value in {
                    "Description": inspection["description"], "TemplateName": inspection["template_name"],
                }.items()):
                    matches.append(tool_id)
    if len(matches) != 1:
        raise WorkflowError("OOS state fields differ from one own signed product source row")
    return {**inspection, "source_tool_use_id": matches[0]}


def _complete_managed_nas_member(row: dict[str, Any], stack_id: str, region: str) -> bool:
    # The actual ListStackResources schema uses Status. Keep older captured
    # ResourceStatus bodies usable, but never choose a successful alias over a
    # conflicting failure, foreign stack or foreign region in the same original.
    statuses = [row[key] for key in ("Status", "ResourceStatus") if key in row]
    return bool(statuses) and all(value == "CREATE_COMPLETE" for value in statuses) and all(
        row.get(key, expected) == expected for key, expected in (("StackId", stack_id), ("RegionId", region))
    )


def managed_nas_mount_identity(reference: dict[str, Any], tools: dict[str, Any]) -> tuple[str, str]:
    """Project a provider import ID only after joining its own original ROS member.

    Caller additionally verifies signed session/account and owning creation wire.
    Keep the source identifier composite; only the native link locator is domain.
    """
    identifier = reference.get("identifier", "")
    parts = identifier.split(":") if isinstance(identifier, str) else []
    attributes = reference.get("attributes") or {}
    region = reference.get("regionId")
    if (
        reference.get("resourceType") != "alicloud_nas_mount_target" or len(parts) != 2
        or not re.fullmatch(r"[a-z0-9]+", parts[0]) or region != "cn-hangzhou"
        or not re.fullmatch(r"[a-z0-9-]+\.cn-hangzhou\.nas\.aliyuncs\.com", parts[1])
        or not parts[1].startswith(parts[0] + "-")
        # Production catalog metadata names MountTargetDomain even for the
        # Terraform import composite. Identity still comes from the exact own
        # PhysicalResourceId below, never from this field label alone.
        or reference.get("identifierField") not in {"PhysicalResourceId", "MountTargetDomain"}
        or reference.get("provider") != "aliyun" or reference.get("consolePartition") != "aliyun"
        or reference.get("evidence") != "api_response" or reference.get("scopeKind") != "regional"
        or not reference.get("accountScope") or not reference.get("stackId") or not reference.get("logicalResourceId")
        or reference.get("status", "CREATE_COMPLETE") != "CREATE_COMPLETE"
        or attributes.get("FileSystemId", parts[0]) != parts[0]
        or attributes.get("MountTargetDomain", parts[1]) != parts[1]
    ):
        raise WorkflowError("managed NAS mount requires its own exact composite original identity")
    owners = set()
    for source in reference.get("sources", []):
        tool_id = source.get("toolUseId")
        tool = tools.get(tool_id, {})
        request = tool.get("input", {})
        if (
            source.get("evidence") != "api_response" or source.get("sourceKind") != "tool_result"
            or source.get("product") != "ros" or source.get("action") != "ListStackResources"
            or source.get("stackId") != reference["stackId"]
            or source.get("logicalResourceId") != reference["logicalResourceId"]
            or source.get("requestRegionId") != region
            or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
            or request.get("product") != "ros" or request.get("action") != "ListStackResources"
            or request.get("version") != "2019-09-10" or request.get("region_id") != region
            or request.get("params", {}).get("StackId") != reference["stackId"]
        ):
            continue
        for result in tool.get("results", []):
            if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                continue
            try:
                raw = json.loads(result["content"])
                rows = raw["Resources"]
                if not isinstance(rows, list) or len(rows) != 2:
                    continue
                members = [row for row in rows if isinstance(row, dict)
                           and row.get("ResourceType") == "alicloud_nas_mount_target"
                           and row.get("LogicalResourceId") == reference["logicalResourceId"]
                           and row.get("PhysicalResourceId") == identifier
                           and _complete_managed_nas_member(row, reference["stackId"], region)]
                parents = [row for row in rows if isinstance(row, dict)
                           and row.get("ResourceType") == "alicloud_nas_file_system"
                           and row.get("PhysicalResourceId") == parts[0]
                           and _complete_managed_nas_member(row, reference["stackId"], region)]
            except (ValueError, KeyError, TypeError):
                continue
            if raw.get("RequestId") and not raw.get("Code") and len(members) == len(parents) == 1:
                owners.add(tool_id)
    if len(owners) != 1:
        raise WorkflowError("managed NAS mount must join its own unique live same-stack filesystem/member rows")
    return parts[0], parts[1]


def bind_nas_mount_target_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Join the native address icon to its own signed DescribeMountTargets row.

    The caller has already verified the current account/session against wire.
    VPC/VSwitch are read from that same row, not invented reference attributes.
    """
    matches = []
    for reference in references:
        attributes = reference.get("attributes") or {}
        if (
            reference.get("provider") != "aliyun" or reference.get("consolePartition") != "aliyun"
            or reference.get("resourceType") not in {"ALIYUN::NAS::MountTarget", "alicloud_nas_mount_target"}
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or reference.get("scopeKind") != "regional" or not reference.get("accountScope")
            or attributes.get("FileSystemId") != inspection["file_system_id"]
            or attributes.get("MountTargetDomain") != inspection["identifier"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            source_path = source.get("sourcePath", "")
            row_path = (
                re.fullmatch(r"/result/MountTargets/MountTarget/(0|[1-9][0-9]*)/MountTargetDomain", source_path)
                if isinstance(source_path, str) else None
            )
            if (
                not tool_id or not row_path or source.get("evidence") != "api_response"
                or source.get("sourceKind") != "tool_result"
                or source.get("product") != "nas" or source.get("action") != "DescribeMountTargets"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "nas" or request.get("action") != "DescribeMountTargets"
                or request.get("version") != "2017-06-26" or request.get("region_id") != inspection["region"]
                or request.get("params", {}).get("FileSystemId") != inspection["file_system_id"]
                or request.get("params", {}).get("MountTargetDomain") != inspection["identifier"]
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    raw = json.loads(result["content"])
                    rows = raw["MountTargets"]["MountTarget"]
                    row = rows[int(row_path.group(1))]
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                if (
                    not raw.get("Code") and raw.get("RequestId") and isinstance(rows, list)
                    and isinstance(row, dict) and row.get("MountTargetDomain") == inspection["identifier"]
                    and row.get("VpcId") == inspection["vpc_id"] and row.get("NetworkType") == "Vpc"
                    and re.fullmatch(r"vsw-[a-z0-9]+", str(row.get("VswId", "")))
                    and len([item for item in rows if isinstance(item, dict)
                             and item.get("MountTargetDomain") == inspection["identifier"]]) == 1
                ):
                    matches.append((tool_id, row["VswId"]))
    if len(matches) != 1:
        raise WorkflowError("NAS mount address fields differ from one own signed original source row")
    return {**inspection, "source_tool_use_id": matches[0][0], "vswitch_id": matches[0][1]}


def bind_sls_project_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Join the native title tooltip to its own exact signed ListProject row."""
    matches = []
    for reference in references:
        if (
            reference.get("provider") != "aliyun" or reference.get("consolePartition") != "aliyun"
            or reference.get("resourceType") not in {"ALIYUN::SLS::Project", "alicloud_log_project"}
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("scopeKind") != "account" or reference.get("regionId")
            or not reference.get("accountScope")
            or (reference.get("attributes") or {}).get("projectName") != inspection["identifier"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            source_path = source.get("sourcePath", "")
            row_path = (
                re.fullmatch(r"/result/projects/(0|[1-9][0-9]*)/projectName", source_path)
                if isinstance(source_path, str) else None
            )
            if (
                not tool_id or not row_path or source.get("evidence") != "api_response"
                or source.get("sourceKind") != "tool_result"
                or source.get("product") != "sls" or source.get("action") != "ListProject"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "sls" or request.get("action") != "ListProject"
                or request.get("version") != "2020-12-30" or request.get("region_id") != inspection["region"]
                or request.get("params", {}).get("projectName") != inspection["identifier"]
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    raw = json.loads(result["content"])
                    rows = raw["projects"]
                    row = rows[int(row_path.group(1))]
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                if (
                    not raw.get("Code") and isinstance(rows, list) and isinstance(row, dict)
                    and row.get("projectName") == inspection["identifier"]
                    and row.get("description") == inspection["description"]
                    and row.get("region") == inspection["region"] and row.get("owner") == inspection["account"]
                    and len([item for item in rows if isinstance(item, dict)
                             and item.get("projectName") == inspection["identifier"]]) == 1
                ):
                    matches.append(tool_id)
    if len(matches) != 1:
        raise WorkflowError("SLS project fields differ from one own signed original source row")
    return {**inspection, "source_tool_use_id": matches[0]}


def oos_secret_parameter_navigation_name(reference: dict[str, Any], tools: dict[str, Any]) -> str:
    """Use the console's own Name without discarding the signed primary p-* ID.

    Runtime callers first prove the reference's account/session against the wire
    journal. This additional join proves Name and Id in the *same* successful
    ListSecretParameters row. It is not a generic alias or render-field override.
    """
    identifier = reference.get("identifier")
    attributes = reference.get("attributes") or {}
    name = attributes.get("Name")
    region = reference.get("regionId")
    aliases = reference.get("aliases", [])
    if (
        reference.get("provider") != "aliyun"
        or reference.get("resourceType") != "ALIYUN::OOS::SecretParameter"
        or reference.get("identifierField") not in {"Id", "PhysicalResourceId"}
        or reference.get("identifierKind") != "id" or reference.get("evidence") != "api_response"
        or reference.get("scopeKind") != "regional" or not reference.get("accountScope")
        or reference.get("consolePartition") != "aliyun"
        or not isinstance(identifier, str) or not re.fullmatch(r"p-[A-Za-z0-9]+", identifier)
        or attributes.get("Id") != identifier
        or attributes.get("PhysicalResourceId", identifier) != identifier
        or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", name)
        or not isinstance(region, str) or not re.fullmatch(r"cn-[a-z0-9-]+", region)
        or not isinstance(aliases, list) or not all(isinstance(alias, dict) for alias in aliases)
        or [alias for alias in aliases if alias.get("identifierField") == "Name"] != [
            {"identifier": name, "identifierKind": "name", "identifierField": "Name"}
        ]
        or any(alias.get("identifier") != identifier for alias in aliases
               if alias.get("identifierField") in {"Id", "PhysicalResourceId"})
    ):
        raise WorkflowError("precise OOS SecretParameter Id/Name context is missing or conflicting")
    matches = []
    for source in reference.get("sources", []):
        tool_id = source.get("toolUseId")
        tool = tools.get(tool_id, {})
        request = tool.get("input", {})
        path = source.get("sourcePath", "")
        row_path = re.fullmatch(r"/result/Parameters/(0|[1-9][0-9]*)/Id", path) if isinstance(path, str) else None
        if (
            not tool_id or not row_path or source.get("evidence") != "api_response"
            or source.get("sourceKind") != "tool_result"
            or source.get("product") != "oos" or source.get("action") != "ListSecretParameters"
            or source.get("requestRegionId") != region
            or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
            or request.get("product") != "oos" or request.get("action") != "ListSecretParameters"
            or request.get("version") != "2019-06-01" or request.get("region_id") != region
            or request.get("params", {}).get("RegionId") != region
        ):
            continue
        for result in tool.get("results", []):
            if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                continue
            try:
                raw = json.loads(result["content"])
                rows = raw["Parameters"]
                row = rows[int(row_path.group(1))]
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if (
                isinstance(rows, list) and isinstance(row, dict)
                and not raw.get("Code") and row.get("Id") == identifier and row.get("Name") == name
                and row.get("Type") == "Secret"
                and len([item for item in rows if isinstance(item, dict)
                         and (item.get("Id") == identifier or item.get("Name") == name)]) == 1
            ):
                matches.append(tool_id)
    if len(matches) != 1:
        raise WorkflowError("OOS SecretParameter Name differs from one own signed original Id row")
    return name


def bind_sae_application_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Bind distinct native SAE fields to one exact signed application query row."""
    matches = []
    for reference in references:
        if (
            reference.get("resourceType") != "ALIYUN::SAE::Application"
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or (reference.get("attributes") or {}).get("AppId") != inspection["identifier"]
            or (reference.get("attributes") or {}).get("AppName") != inspection["name"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("sourceKind") != "tool_result"
                or source.get("product") != "sae" or source.get("action") != "ListApplications"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "sae" or request.get("action") != "ListApplications"
                or request.get("version") != "2019-05-06" or request.get("region_id") != inspection["region"]
                or request.get("params") != {"FieldType": "appIds", "FieldValue": inspection["identifier"],
                                             "CurrentPage": 1, "PageSize": 10}
                or source.get("sourcePath") != "/result/Data/Applications/0/AppId"
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    raw = json.loads(result["content"])
                    rows = raw["Data"]["Applications"]
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    raw.get("Success") is True and str(raw.get("Code")) == "200"
                    and raw.get("RequestId") and isinstance(rows, list) and len(rows) == 1
                    and raw["Data"].get("TotalSize") == 1 and raw["Data"].get("CurrentPage") == 1
                    and raw["Data"].get("PageSize") == 10
                    and isinstance(rows[0], dict) and rows[0].get("AppId") == inspection["identifier"]
                    and rows[0].get("AppName") == inspection["name"]
                    and rows[0].get("AppDescription") == inspection["description"]
                    and rows[0].get("RegionId") == inspection["region"]
                ):
                    matches.append(tool_id)
    if len(matches) != 1:
        raise WorkflowError("SAE exact fields differ from one own signed bounded application row")
    return {**inspection, "source_tool_use_id": matches[0]}


def bind_ots_vpc_binding_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Require the same signed parent/list response and unique exact binding row."""
    matches = []
    fields = {"InstanceName": "instance_name", "InstanceVpcName": "identifier",
              "VpcId": "vpc_id", "RegionNo": "region"}
    for reference in references:
        if (
            reference.get("resourceType") != inspection["resource_type"]
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or any((reference.get("attributes") or {}).get(key) != inspection[field] for key, field in fields.items())
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("sourceKind") != "tool_result"
                or source.get("product") != "tablestore" or source.get("action") != "ListVpcInfoByInstance"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "tablestore" or request.get("action") != "ListVpcInfoByInstance"
                or request.get("version") != "2020-12-09" or request.get("region_id") != inspection["region"]
                or request.get("params", {}).get("InstanceName") != inspection["instance_name"]
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    raw = json.loads(result["content"])
                    rows = raw["VpcList"]
                except (KeyError, TypeError, ValueError):
                    continue
                if raw.get("InstanceName") == inspection["instance_name"] and isinstance(rows, list):
                    matches.extend(tool_id for row in rows if isinstance(row, dict) and all(
                        row.get(key) == inspection[field] for key, field in fields.items() if key != "InstanceName"
                    ))
    if len(matches) != 1:
        raise WorkflowError("OTS VPC binding fields differ from one own signed original source row")
    return {**inspection, "source_tool_use_id": matches[0]}


def bind_nas_protocol_service_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Bind the native ID tooltip and description to one own successful API row."""
    matches = []
    for reference in references:
        if (
            reference.get("resourceType") != inspection["resource_type"]
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or (reference.get("attributes") or {}).get("FileSystemId") != inspection["file_system_id"]
            or (reference.get("attributes") or {}).get("ProtocolServiceId") != inspection["identifier"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("sourceKind") != "tool_result"
                or source.get("product") != "nas" or source.get("action") != "DescribeProtocolService"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "nas" or request.get("action") != "DescribeProtocolService"
                or request.get("version") != "2017-06-26" or request.get("region_id") != inspection["region"]
                or request.get("params", {}).get("FileSystemId") != inspection["file_system_id"]
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    rows = json.loads(result["content"])["ProtocolServices"]
                except (KeyError, TypeError, ValueError):
                    continue
                if isinstance(rows, list):
                    matches.extend(tool_id for row in rows if isinstance(row, dict) and all(
                        row.get(key) == inspection[field] for key, field in {
                            "ProtocolServiceId": "identifier", "FileSystemId": "file_system_id",
                            "Description": "description",
                        }.items()
                    ))
    if len(matches) != 1:
        raise WorkflowError("NAS ProtocolService fields differ from one own signed original source row")
    return {**inspection, "source_tool_use_id": matches[0]}


def bind_nas_fileset_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Join description and path from one own signed Fileset row, not reference guesses."""
    matches = []
    for reference in references:
        if (
            reference.get("resourceType") != inspection["resource_type"]
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or (reference.get("attributes") or {}).get("FileSystemId") != inspection["file_system_id"]
            or (reference.get("attributes") or {}).get("FsetId") != inspection["identifier"]
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("sourceKind") != "tool_result"
                or source.get("product") != "nas" or source.get("action") != "DescribeFilesets"
                or source.get("requestRegionId") != inspection["region"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "nas" or request.get("action") != "DescribeFilesets"
                or request.get("version") != "2017-06-26" or request.get("region_id") != inspection["region"]
                or request.get("params", {}).get("FileSystemId") != inspection["file_system_id"]
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    rows = json.loads(result["content"])["Entries"]["Entrie"]
                except (KeyError, TypeError, ValueError):
                    continue
                if isinstance(rows, list):
                    matches.extend(tool_id for row in rows if isinstance(row, dict) and all(
                        row.get(key) == inspection[field] for key, field in {
                            "FsetId": "identifier", "FileSystemId": "file_system_id",
                            "Description": "description", "FileSystemPath": "path",
                        }.items()
                    ))
    if len(matches) != 1:
        raise WorkflowError("NAS Fileset fields differ from one own signed original source row")
    return {**inspection, "source_tool_use_id": matches[0]}


def bind_ots_instance_source(
    inspection: dict[str, str], references: list[dict[str, Any]], tools: dict[str, Any],
) -> dict[str, str]:
    """Require one successful own ROS child with its original unshortened endpoint."""
    matches = []
    for reference in references:
        if (
            reference.get("resourceType") != inspection["resource_type"]
            or reference.get("identifier") != inspection["identifier"]
            or reference.get("regionId") != inspection["region"]
            or (reference.get("attributes") or {}).get("InstanceName") != inspection["identifier"]
            or not reference.get("stackId") or not reference.get("logicalResourceId")
        ):
            continue
        for source in reference.get("sources", []):
            tool_id = source.get("toolUseId")
            tool = tools.get(tool_id, {})
            request = tool.get("input", {})
            params = request.get("params", {})
            if (
                not tool_id or source.get("evidence") != "api_response"
                or source.get("sourceKind") != "tool_result"
                or source.get("product") != "ros" or source.get("action") != "GetStackResource"
                or source.get("requestRegionId") != inspection["region"]
                or source.get("stackId") != reference["stackId"]
                or source.get("logicalResourceId") != reference["logicalResourceId"]
                or tool.get("toolName") != "aliyun_api" or tool.get("status") != "completed"
                or request.get("product") != "ros" or request.get("action") != "GetStackResource"
                or request.get("version") != "2019-09-10" or request.get("region_id") != inspection["region"]
                or params.get("RegionId") != inspection["region"] or params.get("ShowResourceAttributes") is not True
                or params.get("StackId") != reference["stackId"]
                or params.get("LogicalResourceId") != reference["logicalResourceId"]
            ):
                continue
            for result in tool.get("results", []):
                if result.get("isError") is not False or result.get("toolUseId", tool_id) != tool_id:
                    continue
                try:
                    row = json.loads(result["content"])
                    attributes = row["ResourceAttributes"]
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(attributes, list) or not all(isinstance(a, dict) for a in attributes):
                    continue
                keys = [a.get("ResourceAttributeKey") for a in attributes]
                if not all(isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
                    continue
                values = {a["ResourceAttributeKey"]: a.get("ResourceAttributeValue") for a in attributes}
                if (
                    row.get("ResourceType") == inspection["resource_type"]
                    and row.get("PhysicalResourceId") == inspection["identifier"]
                    and row.get("StackId") == reference["stackId"]
                    and row.get("LogicalResourceId") == reference["logicalResourceId"]
                    and row.get("Status") in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
                    and values.get("InstanceName") == inspection["identifier"]
                    and values.get("PublicEndpoint") == inspection["public_endpoint"]
                ):
                    matches.append(tool_id)
    if len(matches) != 1:
        raise WorkflowError("OTS fields differ from one own signed ROS child source")
    return {**inspection, "source_tool_use_id": matches[0]}
