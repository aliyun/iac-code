"""ShowArchitectureDiagramTool — reads a ROS YAML template and emits a Mermaid architecture diagram."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import DiagramEvent

LAYER_TYPES = frozenset({"VPC", "VSwitch", "SecurityGroup"})

HIDDEN_TYPES = frozenset(
    {
        "SecurityGroupIngress",
        "SecurityGroupEgress",
        "EIPAssociation",
        "BackendServerAttachment",
        "Listener",
        "VServerGroup",
        "Account",
    }
)


def _short_type(full_type: str) -> str:
    parts = full_type.split("::")
    return parts[-1] if parts else full_type


def _service_name(full_type: str) -> str:
    parts = full_type.split("::")
    return parts[1] if len(parts) >= 2 else ""


def _resource_label(short_type: str, service: str) -> str:
    labels = {
        "Instance": _("ECS instance"),
        "InstanceGroup": _("ECS instance group"),
        "EIP": _("Elastic IP address"),
        "LoadBalancer": _("SLB load balancer"),
        "NatGateway": _("NAT gateway"),
        "CommonBandwidthPackage": _("Shared bandwidth package"),
        "DBInstance": _("Database instance"),
        "PrepayDBInstance": _("Database instance"),
    }
    label = labels.get(short_type)
    if label is not None:
        return label
    return f"{service}::{short_type}" if service else short_type


def _layer_base_label(short_type: str, fallback: str) -> str:
    labels = {
        "VPC": _("VPC"),
        "VSwitch": _("VSwitch"),
        "SecurityGroup": _("Security group"),
    }
    return labels.get(short_type, fallback)


def _resolve_ref(value: Any) -> str | None:
    """Extract Ref target from ``{'Ref': 'xxx'}`` or a plain string."""
    if isinstance(value, dict) and "Ref" in value:
        ref = value["Ref"]
        return ref if isinstance(ref, str) and not ref.startswith("ALIYUN::") else None
    if isinstance(value, str):
        return value
    return None


def _resolve_cidr(value: Any, params: dict) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Ref" in value:
        ref = value["Ref"]
        if isinstance(ref, str) and ref in params:
            default = params[ref].get("Default")
            if isinstance(default, str):
                return default
    return None


def _node_sg_ref(props: dict) -> str | None:
    """Resolve SecurityGroupId or first element of SecurityGroupIds."""
    ref = _resolve_ref(props.get("SecurityGroupId"))
    if ref:
        return ref
    sg_ids = props.get("SecurityGroupIds", [])
    if isinstance(sg_ids, list) and sg_ids:
        return _resolve_ref(sg_ids[0])
    return None


def _extract_edges(short_type: str, props: dict, resources: dict) -> list[tuple[str, str]]:
    """Extract connection edges from hidden (auxiliary) resource types."""
    edges: list[tuple[str, str]] = []
    if short_type == "EIPAssociation":
        eip_ref = _resolve_ref(props.get("AllocationId"))
        inst_ref = _resolve_ref(props.get("InstanceId"))
        if eip_ref and inst_ref and eip_ref in resources and inst_ref in resources:
            edges.append((eip_ref, inst_ref))
    elif short_type == "BackendServerAttachment":
        lb_ref = _resolve_ref(props.get("LoadBalancerId"))
        if lb_ref and lb_ref in resources:
            for backend in props.get("BackendServers", []):
                server_ref = None
                if isinstance(backend, dict):
                    if "ServerId" in backend:
                        server_ref = _resolve_ref(backend["ServerId"])
                    elif "Fn::GetAtt" in backend:
                        getatt = backend["Fn::GetAtt"]
                        if isinstance(getatt, list) and getatt:
                            server_ref = str(getatt[0])
                if server_ref and server_ref in resources:
                    edges.append((lb_ref, server_ref))
    return edges


def ros_template_to_mermaid(template_yaml: str) -> str:
    """Convert a ROS YAML template into a Mermaid graph with nested infrastructure layers.

    VPC / VSwitch / SecurityGroup are rendered as nested subgraphs (container layers).
    Compute, gateway, storage resources are rendered as nodes inside the appropriate layer.
    Auxiliary resources (SecurityGroupIngress, EIPAssociation, etc.) are hidden; their
    relationships are expressed as edges between the visible nodes.
    """
    from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load

    try:
        doc = ros_yaml_load(template_yaml)
    except yaml.YAMLError:
        return "graph TD\n  Error[{}]".format(_("YAML parse error"))

    if not isinstance(doc, dict):
        return "graph TD"

    resources: dict = doc.get("Resources") or {}
    if not resources:
        return "graph TD"

    params: dict = doc.get("Parameters") or {}

    # --- classify resources ---
    layers: dict[str, dict] = {}
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str]] = []

    for lid, rdef in resources.items():
        if not isinstance(rdef, dict):
            continue
        full_type = rdef.get("Type", "")
        short = _short_type(full_type)
        service = _service_name(full_type)
        props = rdef.get("Properties") or {}

        if short in LAYER_TYPES:
            layers[lid] = {"short": short, "service": service, "props": props}
        elif short in HIDDEN_TYPES:
            edges.extend(_extract_edges(short, props, resources))
        else:
            label = _resource_label(short, service)
            nodes[lid] = {"short": short, "service": service, "props": props, "label": label}

    # disambiguate when multiple nodes share the same display label
    type_counts: Counter = Counter(n["short"] for n in nodes.values())
    type_seq: Counter = Counter()
    for ninfo in nodes.values():
        s = ninfo["short"]
        if type_counts[s] > 1:
            type_seq[s] += 1
            ninfo["label"] = f"{ninfo['label']} {type_seq[s]}"

    # --- build containment tree ---
    layer_parent: dict[str, str] = {}

    for lid, linfo in layers.items():
        if linfo["short"] == "VSwitch":
            vpc_ref = _resolve_ref(linfo["props"].get("VpcId"))
            if vpc_ref and vpc_ref in layers:
                layer_parent[lid] = vpc_ref

    for lid, linfo in layers.items():
        if linfo["short"] != "SecurityGroup":
            continue
        vpc_ref = _resolve_ref(linfo["props"].get("VpcId"))
        member_vswitches: set[str] = set()
        for nid, ninfo in nodes.items():
            if _node_sg_ref(ninfo["props"]) == lid:
                vs = _resolve_ref(ninfo["props"].get("VSwitchId"))
                if vs and vs in layers:
                    member_vswitches.add(vs)
        if len(member_vswitches) == 1:
            layer_parent[lid] = member_vswitches.pop()
        elif vpc_ref and vpc_ref in layers:
            layer_parent[lid] = vpc_ref

    node_parent: dict[str, str] = {}
    for nid, ninfo in nodes.items():
        sg_ref = _node_sg_ref(ninfo["props"])
        vs_ref = _resolve_ref(ninfo["props"].get("VSwitchId"))
        vpc_ref = _resolve_ref(ninfo["props"].get("VpcId"))

        if sg_ref and sg_ref in layers:
            node_parent[nid] = sg_ref
        elif vs_ref and vs_ref in layers:
            node_parent[nid] = vs_ref
        elif vpc_ref and vpc_ref in layers:
            node_parent[nid] = vpc_ref

    # --- generate Mermaid ---
    lines: list[str] = ["graph TD"]
    sg_style_ids: list[str] = []

    def layer_label(lid: str) -> str:
        linfo = layers[lid]
        base = _layer_base_label(linfo["short"], lid)
        if linfo["short"] in ("VPC", "VSwitch"):
            cidr = _resolve_cidr(linfo["props"].get("CidrBlock"), params)
            if cidr:
                return f"{base} ({cidr})"
        return base

    def render_layer(lid: str, indent: str) -> None:
        sub_id = f"layer_{lid}"
        lines.append(f'{indent}subgraph {sub_id}["{layer_label(lid)}"]')
        if layers[lid]["short"] == "SecurityGroup":
            sg_style_ids.append(sub_id)

        for child_lid in layers:
            if layer_parent.get(child_lid) == lid:
                render_layer(child_lid, indent + "  ")

        for child_nid in nodes:
            if node_parent.get(child_nid) == lid:
                lines.append(f'{indent}  {child_nid}["{nodes[child_nid]["label"]}"]')

        lines.append(f"{indent}end")

    for lid in layers:
        if lid not in layer_parent:
            render_layer(lid, "  ")

    for nid, ninfo in nodes.items():
        if nid not in node_parent:
            lines.append(f'  {nid}["{ninfo["label"]}"]')

    for from_id, to_id in edges:
        if from_id in nodes and to_id in nodes:
            lines.append(f"  {from_id} --> {to_id}")

    for sid in sg_style_ids:
        lines.append(f"  style {sid} stroke-dasharray: 5 5")

    return "\n".join(lines)


class ShowArchitectureDiagramTool(Tool):
    """Pipeline-specific tool that reads a ROS template and emits a Mermaid architecture diagram."""

    @property
    def name(self) -> str:
        return "show_architecture_diagram"

    @property
    def description(self) -> str:
        return _(
            "Read a ROS template YAML file and generate an architecture diagram. "
            "Pass the template file path relative to the working directory and the candidate name."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": _(
                        "Relative path to the ROS template YAML file, such as templates/1-simple-nginx.yml"
                    ),
                },
                "candidate_name": {
                    "type": "string",
                    "description": _("Candidate name, such as Simple Nginx single-instance plan"),
                },
                "candidate_index": {
                    "type": "integer",
                    "description": _(
                        "Zero-based candidate index in evaluated_candidates; used to distinguish duplicate names"
                    ),
                },
            },
            "required": ["file_path", "candidate_name", "candidate_index"],
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def needs_event_queue(self) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = tool_input["file_path"]
        candidate_name = tool_input["candidate_name"]
        candidate_index = tool_input.get("candidate_index")

        try:
            abs_path = _resolve_cwd_relative_file(context.cwd, file_path)
        except ValueError as exc:
            return ToolResult.error(str(exc))

        if not abs_path.exists():
            return ToolResult.error(_("Template file does not exist: {file_path}").format(file_path=file_path))

        template_content = abs_path.read_text(encoding="utf-8")
        mermaid_source = ros_template_to_mermaid(template_content)

        if context.event_queue is not None:
            event = DiagramEvent(
                candidate_name=candidate_name,
                template_content=template_content,
                mermaid_source=mermaid_source,
                candidate_index=candidate_index,
            )
            await context.event_queue.put(event)
        else:
            from loguru import logger

            logger.debug(
                "{} invoked without event_queue; skipping event emit "
                "(typically means pipeline mode not active for this tool call)",
                type(self).__name__,
            )

        return ToolResult.success(
            _('Generated the architecture diagram for "{candidate_name}".').format(candidate_name=candidate_name)
        )


def _resolve_cwd_relative_file(cwd: str, file_path: str) -> Path:
    path = Path(file_path)
    if path.is_absolute():
        raise ValueError(_("Template file path must be relative to the working directory"))

    root = Path(cwd).resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(_("Template file path cannot escape the working directory"))
    return resolved
