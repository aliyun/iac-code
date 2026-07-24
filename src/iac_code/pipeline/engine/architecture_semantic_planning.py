#!/usr/bin/env python3
"""Preview a ROS template architecture diagram with a real LLM semantic pass.

Usage:
    uv run python scripts/rendering/preview_template_architecture_llm.py TEMPLATE.yml

The script uses the current iac-code LLM configuration by default. Override it
with IAC_CODE_PROVIDER / IAC_CODE_MODEL / IAC_CODE_API_KEY when needed.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from importlib import import_module
from pathlib import Path
from typing import Any, NamedTuple

from rich.console import Console

from iac_code.agent.system_prompt import DYNAMIC_BOUNDARY, split_by_dynamic_boundary
from iac_code.config import DEFAULT_MODEL, get_active_provider_key, load_credentials, load_saved_model
from iac_code.i18n import setup_i18n
from iac_code.pipeline.engine.architecture_graph import (
    ArchitectureMultiViewRenderResult,
    render_ros_template_architecture,
    render_ros_template_architecture_views,
)
from iac_code.providers.base import Message
from iac_code.providers.manager import ProviderManager
from iac_code.providers.thinking import get_thinking_spec
from iac_code.ui.diagram_rendering import style_attachment_lines

MAX_SEMANTIC_PLAN_ATTEMPTS = 3
RAM_GOVERNANCE_TYPE_PREFIX = "ALIYUN::RAM::"
RAM_GOVERNANCE_CORE_TYPES = {
    "ALIYUN::RAM::AccessKey",
    "ALIYUN::RAM::Group",
    "ALIYUN::RAM::Role",
    "ALIYUN::RAM::User",
    "ALIYUN::RAM::UserToGroupAddition",
}
RAM_GOVERNANCE_PERMISSION_TYPES = {
    "ALIYUN::RAM::AttachPolicyToGroup",
    "ALIYUN::RAM::AttachPolicyToRole",
    "ALIYUN::RAM::AttachPolicyToUser",
    "ALIYUN::RAM::ManagedPolicy",
    "ALIYUN::RAM::Policy",
    "ALIYUN::RAM::PolicyAttachment",
}


class TerminalPreviewItem(NamedTuple):
    id: str
    title: str
    mermaid_source: str


SYSTEM_PROMPT = """\
You enrich ROS architecture diagrams with semantic node labels and edges.
Return ONLY valid JSON, no markdown.

The JSON schema is:
{"node_labels":[{"id":string,"label":string,"confidence":"high|medium|low"}],"edges":[{"from":string,"to":string,"kind":"traffic|dependency|management|inferred","label":string,"confidence":"high|medium|low"}],"views":[{"id":"overview|detail_<area>","title":string,"purpose":string,"layout":"flat|contained","anchors":[string],"groups":[{"id":string,"label":string,"members":[string],"parent":string}],"nodes":[string],"edges":[{"from":string,"to":string,"kind":"traffic|dependency|management|inferred","label":string}]}]}

Rules:
- Use only ids from visible_nodes.
- Do not invent resources.
- Use node_label_hints, logical ids, resource types, visible labels, property_references,
  all_property_references, route_intents, orchestration_actions, and outputs as naming evidence.
- node_labels replace only the main node title; do not include attachment lines such as "+ EIP" or "+ Security group".
- node label must be short, max 32 chars, preferably a role name such as "vCenter manager" or "ESXi host group".
- Include node_labels only when a clearer role label is useful.
- Omit labels that would be identical to the visible label.
- Omit reason fields and prose explanations. Keep JSON compact.
- Keep the plan compact: at most 24 node_labels and at most 16 edges.
- For complex architectures, produce diagrams as "overview + drill-down details", not parallel perspective views.
  The first view must be id="overview": a small end-to-end map that explains the whole architecture with the main
  domains/components and the critical relationships between them. It should not repeat every child resource.
  The overview should be connected whenever possible: do not put important nodes in overview without an edge that
  explains how they participate. Move unconnected components to a detail view or add a high-confidence relationship.
- In overview, use view-only summary groups for equivalent or repeated resources instead of drawing each child as
  a separate top-level node. Put groups in the view's groups array, include the group id in nodes, and use the group
  id in overview edges and detail anchors. Example: {"id":"ProdAlbGroup","label":"生产 ALB","members":["Alb1","Alb2"]}.
  Do not invent group members; every member must be from visible_nodes. Use parent with a container id when the
  group belongs inside a VPC/VSwitch/region container. Detail views should expand the real members.
- For networked templates, overview should preserve placement enough to answer "which VPC/VSwitch contains this?".
  Prefer layout="contained" for overview when VPC/VSwitch/region placement is important, but keep it small by using
  summary groups instead of listing every resource.
- If placement_summary.requires_contained_overview is true, the overview must use layout="contained"; preserve the
  VPC/VSwitch placement because it is part of the architecture meaning.
- Add detail views only for complex local areas that need expansion, using ids like detail_dmz, detail_app,
  detail_data, detail_network, detail_operations, or detail_permissions. A detail view explains one area from the
  overview in more concrete resources and relationships. Each detail view should also be connected whenever possible;
  do not list unrelated local resources without relationships.
- Do not create a detail view just to fan out a small summary group that is already clear in overview. For example,
  if overview already shows "SLB -> backend server group" and the group label lists two backend servers, skip a
  detail_app that only redraws "SLB -> backend server 1" and "SLB -> backend server 2" with the same meaning.
- If overview plus one small detail would still fit in <=8 nodes/groups and <=6 edges, prefer a single overview
  instead of creating a drill-down. Small CEN bandwidth/config details can stay in overview when they do not crowd
  the layout.
- Every detail view must include anchors: ids from the overview view that this detail expands. If a detail expands
  a relationship between two overview nodes, include both as anchors. The renderer marks these anchors in overview
  so the user can see which overview part each detail belongs to.
- Choose detail views by local complexity. For example, load balancer plus backend servers becomes detail_app;
  CEN/Transit Router/NAT/routes/cross-VPC routing becomes detail_network; database/cache/storage internals become
  detail_data; Cloud Assistant/OOS/ESS lifecycle/commands become detail_operations.
- For RAM-heavy governance templates, the primary story is identity and permission governance, not application
  runtime traffic. Prioritize users, AccessKeys, groups, roles, policies/permission scopes, and governed resources.
  Use detail_permissions, and prefer relationships such as "用户凭证", "加入用户组", "授予权限", "资源范围",
  and "角色授权". Keep ECS/RDS/OSS traffic secondary unless the template explicitly describes runtime traffic.
  If governance_summary is present, follow its primary_intent and resource_scope_nodes.
- For detail_app that crosses VPCs, make the cross-VPC nature explicit on the edge label, such as "经 CEN 后端转发"
  or "跨 VPC 后端转发". If CEN/Transit Router is the actual network path, prefer including it as an intermediate
  node or at least name it on the line. Avoid layouts where one ALB appears to fan out to another ALB's ECS backends;
  use backend summary groups when fan-out lines become ambiguous.
- Do not use CEN/Transit Router as the target of business traffic. CEN/Transit Router is network underlay for VPC
  connectivity, not an application backend. For example, draw NLB -> production ALB/application group as traffic
  with a label like "经 CEN 后端转发", and draw separate dependency/management edges from CEN/Transit Router to
  VPC/domain/route summary nodes.
- NAT Gateway/SNAT is outbound access, not the public ingress path to NLB/ALB/SLB. Do not draw NAT gateway -> load
  balancer as "公网访问" or "公网出口". Show NAT as a VPC egress capability, or connect private/application domains
  to NAT with labels such as "SNAT 出网".
- For detail_network, show what the network path is for. Prefer domain nodes or summary groups such as DMZ VPC,
  production VPC 1/2, CEN Transit Router, and NAT gateway. Avoid a diagram made mostly of generic "VPC config" or
  "route config" nodes; route/config nodes should support a path like "生产 VPC -> CEN -> DMZ NAT".
- For multi-VSwitch architectures, detail_network must actually show VPC/VSwitch/network domain nodes or groups.
  Do not create a network detail that only repeats an application or compute anchor.
- Use route_intents to connect routing paths. If a route has next_hop_resource as an ECS instance/group and another
  route or CEN route entry points to a CEN attachment, show the forwarding compute as part of the path, such as
  "安全 VPC 路由 -> 安全转发网关 -> 安全子网回程路由 -> CEN 转发路由器"; do not leave the
  forwarding compute and return route/CEN route on separate isolated branches.
- In detail_network, CEN/Transit Router VPC-connection edges should terminate at VPC/domain/route summary nodes, not
  load balancer nodes. Name repeated route/config domains by purpose, such as "DMZ VPC 路由配置",
  "生产 VPC 1 路由配置", and "生产 VPC 2 路由配置".
- In overview, do not make CEN/Transit Router the visual source of VPC connection/support edges. A top-down
  "CEN/Transit Router -> VPC route domain" edge makes CEN look like the public ingress. Prefer
  "VPC/domain/route summary -> CEN/Transit Router" with labels such as "CEN 接入" or "VPC 接入", while business
  traffic edges can still say "经 CEN 后端转发".
- In detail_network, also avoid CEN/Transit Router as a traffic source to VPCs or route domains. Reverse those edges
  to VPC/domain -> CEN/Transit Router and label them as CEN/VPC access.
- For ACK/Kubernetes templates, use kubernetes_applications and CONCEPT::ACK nodes to express application semantics
  instead of leaving Kubernetes manifests as ACK cluster attachment text. Prefer chains such as
  Ingress -> Service exposure -> application workload, HPA -> application workload, and metrics/SLS -> HPA when those
  nodes exist.
- Keep route/config domain nodes out of overview. Nodes such as "DMZ VPC 路由配置" or "生产 VPC 1 路由域" belong in
  detail_network. If a business traffic edge already says "经 CEN", do not also include CEN/Transit Router as an
  overview node; anchor detail_network from the business endpoints instead.
- In short: do not also include CEN/Transit Router as an overview node when the traffic label already says "经 CEN".
- In overview, if CEN/Transit Router connects VPCs, do not draw a direct DMZ VPC -> production VPC edge that implies
  the VPCs are directly connected. Route the overview relationship through the CEN/Transit Router node.
- Do not create separate peer views named traffic, network, placement, operations, or attachments. Those are
  perspectives, not drill-down areas. Network or operations details are allowed only as detail_network or
  detail_operations when they expand one local area from the overview.
- Avoid repeating the same central nodes in every view. Repeat a node in a detail view only when it anchors that
  local expansion; otherwise keep it in the overview.
- Overview should stay compact: at most 8 nodes and 6 edges. Detail views should stay local: at most 12 nodes and
  8 edges. Put the key relationships in either overview or the relevant detail; do not delete relationships merely
  to make diagrams cleaner.
- Use layout="flat" for overview and most detail views so containment does not make terminal diagrams too tall.
  Use layout="contained" only for a detail view whose purpose is physical placement or boundary expansion, such as
  a VPC/VSwitch, region, security boundary, or network segment.
- Do not encode VPC/VSwitch/SecurityGroup containment as edges.
- edge label must be short, max 18 chars, preferably 1-3 words.
- Terminal rendering does not support multiline edge labels. For orchestration_actions, include the source in
  parentheses instead of using a newline, for example "挂载 NAS（云助手）".
- Follow target_language from the fact bundle. Use short Chinese role labels when target_language.code is "zh".
- Do not copy raw identifier-like names such as "APP01" or "ALB_HZ_1" as final labels in Chinese output. Treat
  node_label_hints as evidence for role and sequence, not as display text, unless the name is already a human
  business name in the target language.
- Product acronyms such as ECS, ALB, NLB, CEN, VPC, NAT, Redis, and PolarDB may remain, but the label should still
  carry target-language role meaning, for example "生产 ALB 1" or "应用服务器 1".
- Use explicit_relations, property_references, and all_property_references as evidence for dependencies, including
  references embedded in UserData. explicit_relations may include hidden folded resources such as Route resources.
  Treat ALIYUN::ECS::Route.NextHopId and CEN route-entry next-hop relations as routing-path evidence, and infer
  edges only between visible nodes.
- Use network_attachments as evidence for CEN/cross-VPC underlay. For ALIYUN::CEN::CenInstanceAttachment,
  child_instance_type=VPC/VBR means the CEN instance connects a VPC or VBR; child_resource identifies a resource
  created in this template, while Ref:* child ids are existing external networks.
- Do not leave CEN/network access configuration isolated in overview. If it matters to the end-to-end story, connect
  it to the relevant VPC/domain/application path; otherwise keep it in detail_network.
- For detail_network with CEN/VPC/VBR relationships, prefer flat VPC/domain summary nodes over contained VPC
  containers when the VPC contains application servers. Contained VPC layout expands the business resources and makes
  a network interconnect diagram look like an application placement diagram.
- When network_attachments has a child_resource such as "Vpc", include that resource/container as a flat node in
  detail_network and connect it to the CEN/network access config with a label like "CEN 接入". Existing external
  Ref:* VPC/VBR ids may stay summarized inside the CEN config label when no visible node exists.
- When CEN attachments include external Ref:* VPC/VBR ids, make that explicit in detail_network. Do not draw only
  "current VPC -> CEN config"; label the CEN config or edge as current VPC plus external VPC/VBR interconnect, such
  as "CEN 互联（外部网络 x2）" or "与外部 VPC/VBR 互联".
- Use orchestration_actions as evidence for intent edges from target compute resources to referenced resources.
  Do not render the orchestration action itself as a node.
- If one orchestration action targets multiple compute resources and references the same databases, caches, message
  queues, registries, or storage endpoints, show those as shared application dependencies. Use a summary group or
  mirror the dependency edges; do not attach the backend dependencies to only one target.
- When an ESS scaling configuration is derived from a seed/template ECS instance, either directly or through a custom
  image made from that instance, render the configuration-source relationship from the ECS instance to the ESS scaling
  group, not from the scaling group back to the seed instance. Use a short label such as "伸缩配置" or
  "scaling config".
- If concept_nodes contains scaled compute instances, put inherited application traffic on that concept node,
  not on the scaling controller.
- Use inferred/low for plausible app dependencies not explicitly present in the template.
- Use management for configuration, orchestration, scaling, or attachment relationships.
- Prefer a small number of useful edges over a dense graph.
- Avoid repeated fan-in or fan-out edges between equivalent resources. If several equivalent resources share the same
  relationship, use one aggregate relationship to a concept/group node when available, or keep the relationship
  summarized in the role labels instead of drawing every parallel edge.
"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #fafafa; }}
  .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1.5rem; }}
  .mermaid {{ overflow-x: auto; }}
  pre {{ white-space: pre-wrap; word-break: break-word; background: #f5f5f5; padding: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <h1>{title}</h1>
  <pre class="mermaid">
{mermaid}
  </pre>
</div>
<script>mermaid.initialize({{ startOnLoad: true, theme: 'default' }});</script>
</body>
</html>
"""

PROMPT_DEBUG_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f7f7f8;
    --card: #ffffff;
    --text: #1f2937;
    --muted: #6b7280;
    --border: #d1d5db;
    --accent: #0f766e;
    --code: #111827;
    --code-bg: #f3f4f6;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #111827;
      --card: #1f2937;
      --text: #f9fafb;
      --muted: #9ca3af;
      --border: #374151;
      --accent: #2dd4bf;
      --code: #e5e7eb;
      --code-bg: #0f172a;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 24px;
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  h1 {{ margin: 0 0 8px; font-size: 24px; }}
  h2 {{ margin: 24px 0 12px; font-size: 18px; }}
  .meta, .muted {{ color: var(--muted); }}
  .card {{
    margin: 16px 0;
    padding: 16px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--card);
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 8px;
  }}
  .metric {{
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
  }}
  .metric strong {{ display: block; font-size: 13px; color: var(--muted); }}
  .metric span {{ font-size: 16px; }}
  .prompt-step {{ margin: 10px 12px; }}
  .prompt-step summary {{
    border: 1px solid var(--border);
    border-radius: 6px 6px 0 0;
    background: color-mix(in srgb, var(--accent) 8%, transparent);
    color: var(--muted);
    font-size: 13px;
  }}
  .prompt-step pre {{
    border: 1px solid var(--border);
    border-top: 0;
    border-radius: 0 0 6px 6px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 8px;
  }}
  th, td {{
    border-bottom: 1px solid var(--border);
    padding: 8px;
    text-align: left;
    vertical-align: top;
  }}
  th {{ color: var(--muted); font-weight: 600; }}
  details {{
    margin: 12px 0;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--card);
  }}
  summary {{
    cursor: pointer;
    padding: 10px 12px;
    font-weight: 600;
  }}
  pre {{
    margin: 0;
    padding: 12px;
    overflow: auto;
    max-height: 720px;
    border-top: 1px solid var(--border);
    background: var(--code-bg);
    color: var(--code);
    white-space: pre-wrap;
    word-break: break-word;
    font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }}
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    color: var(--accent);
    font-size: 12px;
    font-weight: 700;
  }}
  .issue {{
    margin: 4px 0;
    padding-left: 18px;
  }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">Model: {model}</div>
  <div class="meta">Generated at: {generated_at}</div>
  <div class="card">
    <h2>Summary</h2>
    <div class="grid">{metrics}</div>
    <table>
      <thead>
        <tr>
          <th>Attempt</th>
          <th>Selected</th>
          <th>LLM Time</th>
          <th>Prompt Chars</th>
          <th>Cache Prefix Chars</th>
          <th>Sent Issues</th>
          <th>Result Issues</th>
          <th>Cache Read Tokens</th>
          <th>Raw Output Chars</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  {attempts}
</body>
</html>
"""


def build_semantic_plan_user_prompt(
    architecture_context: dict[str, Any],
    *,
    attempt: int | None = None,
    previous_plan: dict[str, Any] | None = None,
    validation_issues: list[str] | tuple[str, ...] = (),
    include_fact_bundle: bool = True,
    include_previous_plan: bool = True,
) -> str:
    fact_bundle = json.dumps(architecture_context, ensure_ascii=False, separators=(",", ":"))
    attempt_prefix = f"Attempt {attempt} instruction" if attempt is not None else "Attempt instruction"
    if validation_issues:
        issues = "\n".join(f"- {issue}" for issue in validation_issues)
        previous = json.dumps(previous_plan or {}, ensure_ascii=False, indent=2)
        previous_section = ""
        if include_previous_plan:
            previous_section = f"\n\nPrevious semantic_plan:\n{previous}"
        dynamic_instruction = (
            f"{attempt_prefix}:\n"
            "Revise the previous semantic_plan for this architecture fact bundle.\n"
            "Fix every validation issue below. Return JSON only.\n\n"
            "Validation issues:\n"
            f"{issues}"
            f"{previous_section}\n\n"
            "Return JSON only."
        )
    else:
        dynamic_instruction = (
            f"{attempt_prefix}:\n"
            "Create a semantic_plan for this architecture fact bundle.\n"
            "Follow target_language exactly. Return JSON only."
        )
    if not include_fact_bundle:
        return dynamic_instruction
    return f"Architecture fact bundle:\n{fact_bundle}\n\n{DYNAMIC_BOUNDARY}\n\n{dynamic_instruction}"


def extract_semantic_plan_json(text: str) -> dict[str, Any]:
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if match is None:
            raise ValueError("LLM output does not contain a JSON object") from None
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM output JSON must be an object")
    return value


def try_extract_semantic_plan_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return extract_semantic_plan_json(text), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def build_llm_architecture_context(architecture_context: dict[str, Any]) -> dict[str, Any]:
    """Build the smaller fact bundle sent to the LLM.

    Rendering and validation still use the full architecture context. This is
    only the semantic prompt payload, so it keeps evidence that helps naming and
    relationships while dropping rendered-state/debug data.
    """
    list_limits = {
        "visible_edges": 80,
        "explicit_relations": 96,
        "property_references": 48,
        "all_property_references": 64,
        "route_intents": 64,
        "network_attachments": 64,
        "kubernetes_applications": 80,
        "orchestration_actions": 64,
        "outputs": 32,
        "node_label_hints": 80,
    }
    keep_keys = (
        "template_summary",
        "target_language",
        "visible_nodes",
        "concept_nodes",
        "containers",
        "containment",
        "visible_edges",
        "explicit_relations",
        "property_references",
        "all_property_references",
        "route_intents",
        "network_attachments",
        "kubernetes_applications",
        "node_label_hints",
        "orchestration_actions",
        "outputs",
        "llm_semantic_plan_schema",
    )
    slim: dict[str, Any] = {}
    truncation_summary: dict[str, dict[str, int | bool]] = {}
    for key in keep_keys:
        if key not in architecture_context:
            continue
        value = architecture_context[key]
        if isinstance(value, list) and key in list_limits:
            limit = list_limits[key]
            slim[key] = value[:limit]
            total_count = len(value)
            if total_count > limit:
                truncation_summary[key] = {
                    "included_count": limit,
                    "total_count": total_count,
                    "omitted_count": total_count - limit,
                    "truncated": True,
                }
        else:
            slim[key] = value
    if truncation_summary:
        slim["truncation_summary"] = truncation_summary
    governance_summary = _build_governance_summary(architecture_context)
    if governance_summary:
        slim["governance_summary"] = governance_summary
    placement_summary = _build_placement_summary(architecture_context)
    if placement_summary:
        slim["placement_summary"] = placement_summary
    slim["semantic_plan_scaffold"] = _build_semantic_plan_scaffold(architecture_context)
    return slim


def _build_semantic_plan_scaffold(architecture_context: dict[str, Any]) -> dict[str, Any]:
    raw_edges: list[dict[str, Any]] = []
    overview_layout = (
        "contained"
        if _needs_network_drilldown_view(architecture_context)
        or _needs_placement_preserving_overview(architecture_context)
        else "flat"
    )
    needs_ram_governance = _needs_ram_governance_drilldown_view(architecture_context)
    needs_ack_application = _needs_ack_application_drilldown_view(architecture_context)
    views = [
        {
            "id": "overview",
            "layout": overview_layout,
            "max_nodes": 8,
            "max_edges": 6,
            "intent": "end-to-end summary; use summary groups for repeated resources",
        }
    ]
    if needs_ram_governance:
        views.append(
            {
                "id": "detail_permissions",
                "layout": "flat",
                "max_nodes": 12,
                "max_edges": 8,
                "intent": "expand RAM users, AccessKeys, groups, roles, policies, and governed resource scopes",
            }
        )
    if _needs_network_drilldown_view(architecture_context):
        views.append(
            {
                "id": "detail_network",
                "layout": "flat",
                "max_nodes": 12,
                "max_edges": 8,
                "intent": "expand CEN/TransitRouter/NAT/routes/cross-VPC relationships from overview anchors",
            }
        )
    resource_types = {
        str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if item.get("type")
    }
    if not needs_ram_governance and (
        needs_ack_application
        or any("LoadBalancer" in resource_type or "::ECS::" in resource_type for resource_type in resource_types)
    ):
        views.append(
            {
                "id": "detail_app",
                "layout": "flat",
                "max_nodes": 12,
                "max_edges": 8,
                "intent": (
                    "expand ACK/Kubernetes ingress/service/HPA/workload relationships from overview anchors"
                    if needs_ack_application
                    else "expand ingress/load-balancer/application compute relationships from overview anchors"
                ),
            }
        )
    if not needs_ram_governance and any(
        marker in resource_type
        for resource_type in resource_types
        for marker in ("POLARDB", "RDS", "REDIS", "NAS", "OSS", "DBInstance", "DBCluster")
    ):
        views.append(
            {
                "id": "detail_data",
                "layout": "flat",
                "max_nodes": 12,
                "max_edges": 8,
                "intent": "expand database/cache/storage access relationships from overview anchors",
            }
        )
    if _list_of_dicts(architecture_context.get("orchestration_actions")):
        views.append(
            {
                "id": "detail_operations",
                "layout": "flat",
                "max_nodes": 12,
                "max_edges": 8,
                "intent": "expand Cloud Assistant/OOS/ESS lifecycle operations from overview anchors",
            }
        )
    return {"views": views[:4], "edge_budget": 16, "node_label_budget": 24, "raw_edges": raw_edges}


def _build_governance_summary(architecture_context: dict[str, Any]) -> dict[str, Any]:
    if not _needs_ram_governance_drilldown_view(architecture_context):
        return {}
    visible_ram_nodes = [
        _summary_node(item)
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if _semantic_type_is_ram_governance(str(item.get("type") or ""))
    ]
    resource_scope_nodes = [
        _summary_node(item)
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if not _semantic_type_is_ram_governance(str(item.get("type") or ""))
    ]
    ram_counts: dict[str, int] = {}
    for item in _ram_governance_items(architecture_context):
        resource_type = str(item.get("type") or "")
        short_type = resource_type.rsplit("::", 1)[-1] if resource_type else "Unknown"
        ram_counts[short_type] = ram_counts.get(short_type, 0) + 1
    return {
        "primary_intent": "identity_and_permission_governance",
        "ram_counts": dict(sorted(ram_counts.items())),
        "ram_visible_nodes": visible_ram_nodes[:12],
        "resource_scope_nodes": resource_scope_nodes[:12],
        "recommended_view": "detail_permissions",
        "relationship_intents": [
            "AccessKey -> RAM User",
            "RAM User -> RAM Group",
            "RAM Group/Role -> permission scope resources",
            "RAM Role -> compute/service assuming the role",
        ],
    }


def _build_placement_summary(architecture_context: dict[str, Any]) -> dict[str, Any]:
    if not _needs_placement_preserving_overview(architecture_context):
        return {}
    return {
        "primary_intent": "multi_vswitch_or_availability_zone_placement",
        "requires_contained_overview": True,
        "placement_domains": _placement_vswitch_domains(architecture_context)[:8],
        "relationship_intents": [
            "show which application/data resources are placed in each VSwitch",
            "preserve VPC/VSwitch boundaries in overview",
            "keep SLB/application/database traffic edges inside the contained overview",
        ],
    }


def _summary_node(item: dict[str, Any]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key in ("id", "type", "label"):
        value = item.get(key)
        if isinstance(value, str) and value:
            summary[key] = value
    return summary


def repair_semantic_plan_locally(architecture_context: dict[str, Any], semantic_plan: dict[str, Any]) -> dict[str, Any]:
    valid_node_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    valid_container_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("containers"))
        if isinstance(item.get("id"), str)
    }
    valid_real_ids = valid_node_ids | valid_container_ids
    repaired: dict[str, Any] = {}

    node_labels: list[dict[str, Any]] = []
    for label in _list_of_dicts(semantic_plan.get("node_labels")):
        node_id = label.get("id")
        text = label.get("label")
        if not isinstance(node_id, str) or node_id not in valid_node_ids:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        cleaned: dict[str, Any] = {"id": node_id, "label": _single_line_label(text, 32)}
        confidence = label.get("confidence")
        if isinstance(confidence, str) and confidence in {"high", "medium", "low"}:
            cleaned["confidence"] = confidence
        node_labels.append(cleaned)
        if len(node_labels) >= 24:
            break
    if node_labels:
        repaired["node_labels"] = node_labels

    edges = _repair_semantic_edges(
        _list_of_dicts(semantic_plan.get("edges")),
        valid_node_ids,
        max_edges=16,
        require_selected_ids=None,
    )
    if edges:
        repaired["edges"] = edges

    views = _repair_semantic_views(
        architecture_context,
        _list_of_dicts(semantic_plan.get("views")),
        valid_real_ids,
        valid_node_ids,
    )
    if views:
        views = _replace_noisy_container_overview_with_business_edges(architecture_context, views, edges)
        views = _complete_overview_with_top_level_edges(architecture_context, views, edges)
        views = _repair_external_cen_network_view_edges(architecture_context, views)
        views = _drop_nat_to_load_balancer_edges_from_views(architecture_context, views)
        views = _repair_transit_router_source_edges_from_views(architecture_context, views)
        views = _repair_split_route_forwarder_chain_edges(architecture_context, views)
        views = _repair_cross_network_detail_edge_labels(architecture_context, views)
        views = _drop_redundant_detail_views(views)
        views = _merge_small_detail_views_into_overview(architecture_context, views)
        views = _drop_redundant_attachment_marker_nodes_from_views(architecture_context, views)
        views = _drop_empty_overview_containers_from_views(architecture_context, views)
        views = _complete_detail_views_from_overview_edges(views)
        views = _drop_redundant_detail_views(views)
        views = _drop_isolated_non_anchor_detail_nodes_from_views(views)
        repaired["views"] = views
    return repaired


def _replace_noisy_container_overview_with_business_edges(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
    top_level_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not top_level_edges:
        return views
    visible_node_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    network_container_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("containers"))
        if isinstance(item.get("id"), str) and str(item.get("type") or "") == "ALIYUN::ECS::VSwitch"
    }
    if not visible_node_ids or not network_container_ids:
        return views

    replacement_overview: dict[str, Any] | None = None
    replacement_nodes: list[str] = []
    replacement_edges: list[dict[str, Any]] = []
    for view in views:
        if str(view.get("id") or "") != "overview":
            continue
        nodes = _dedupe_strings(view.get("nodes"))
        if len([node_id for node_id in nodes if node_id in network_container_ids]) < 3:
            continue
        groups = _list_of_dicts(view.get("groups"))
        group_ids = {str(group.get("id")) for group in groups if isinstance(group.get("id"), str)}
        if groups or any(node_id in visible_node_ids or node_id in group_ids for node_id in nodes):
            continue
        replacement_nodes = _top_level_edge_endpoint_nodes(top_level_edges, visible_node_ids)
        if len(replacement_nodes) < 2:
            continue
        replacement_edges = _top_level_edges_between_nodes(top_level_edges, set(replacement_nodes))
        if not replacement_edges:
            continue
        replacement_overview = {
            **view,
            "nodes": replacement_nodes[:8],
            "edges": replacement_edges[:8],
        }
        replacement_overview.pop("groups", None)
        break

    if replacement_overview is None:
        return views

    replacement_node_ids = set(replacement_nodes)
    container_replacements = _overview_container_anchor_replacements(
        architecture_context,
        replacement_nodes,
    )
    repaired_views: list[dict[str, Any]] = []
    for view in views:
        if str(view.get("id") or "") == "overview":
            repaired_views.append(replacement_overview)
            continue
        repaired_view = dict(view)
        anchors = _dedupe_strings(view.get("anchors"))
        if anchors:
            repaired_anchors: list[str] = []
            for anchor_id in anchors:
                if anchor_id in replacement_node_ids:
                    repaired_anchors.append(anchor_id)
                    continue
                repaired_anchors.extend(container_replacements.get(anchor_id, []))
            repaired_anchors = _dedupe_strings(repaired_anchors)
            if repaired_anchors:
                repaired_view["anchors"] = repaired_anchors
            else:
                repaired_view.pop("anchors", None)
        repaired_views.append(repaired_view)
    return repaired_views


def _top_level_edge_endpoint_nodes(top_level_edges: list[dict[str, Any]], visible_node_ids: set[str]) -> list[str]:
    endpoint_nodes: list[str] = []
    for edge in top_level_edges:
        for endpoint_key in ("from", "to"):
            endpoint_id = _semantic_edge_endpoint(edge, endpoint_key)
            if endpoint_id in visible_node_ids:
                endpoint_nodes.append(endpoint_id)
    return _dedupe_strings(endpoint_nodes)


def _top_level_edges_between_nodes(
    top_level_edges: list[dict[str, Any]],
    selected_node_ids: set[str],
) -> list[dict[str, Any]]:
    repaired_edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for edge in top_level_edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id not in selected_node_ids or to_id not in selected_node_ids:
            continue
        repaired_edge = {
            "from": from_id,
            "to": to_id,
            "kind": str(edge.get("kind") or "inferred"),
            "label": str(edge.get("label") or ""),
        }
        edge_key = (
            str(repaired_edge["from"]),
            str(repaired_edge["to"]),
            str(repaired_edge["kind"]),
            str(repaired_edge["label"]),
        )
        if edge_key in seen:
            continue
        repaired_edges.append(repaired_edge)
        seen.add(edge_key)
    return repaired_edges


def _overview_container_anchor_replacements(
    architecture_context: dict[str, Any],
    overview_node_ids: list[str],
) -> dict[str, list[str]]:
    containment_parent = {
        str(item.get("resource")): str(item.get("container"))
        for item in _list_of_dicts(architecture_context.get("containment"))
        if isinstance(item.get("resource"), str) and isinstance(item.get("container"), str)
    }
    container_parent = {
        str(item.get("id")): str(item.get("parent"))
        for item in _list_of_dicts(architecture_context.get("containers"))
        if isinstance(item.get("id"), str) and isinstance(item.get("parent"), str)
    }
    replacements: dict[str, list[str]] = {}
    for node_id in overview_node_ids:
        current = containment_parent.get(node_id)
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            replacements.setdefault(current, []).append(node_id)
            current = container_parent.get(current)
    return {container_id: _dedupe_strings(node_ids) for container_id, node_ids in replacements.items()}


def _complete_overview_with_top_level_edges(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
    top_level_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not top_level_edges:
        return views
    visible_node_types = _visible_node_types_by_id(architecture_context)
    has_detail_network = any(str(view.get("id") or "") == "detail_network" for view in views)
    repaired_views: list[dict[str, Any]] = []
    for view in views:
        if str(view.get("id") or "") != "overview":
            repaired_views.append(view)
            continue
        nodes = _dedupe_strings(view.get("nodes"))
        groups = _list_of_dicts(view.get("groups"))
        group_ids = {str(group.get("id")) for group in groups if isinstance(group.get("id"), str)}
        selected_ids = set(nodes) | group_ids
        selected_group_members: set[str] = set()
        for group in groups:
            group_id = group.get("id")
            if isinstance(group_id, str) and group_id in selected_ids:
                selected_group_members.update(_dedupe_strings(group.get("members")))
        view_edges = _list_of_dicts(view.get("edges"))
        edge_keys = {
            (
                _semantic_edge_endpoint(edge, "from"),
                _semantic_edge_endpoint(edge, "to"),
                str(edge.get("kind") or ""),
                str(edge.get("label") or ""),
            )
            for edge in view_edges
        }
        connected_ids = _edge_endpoint_ids(view_edges)
        repaired_edges = list(view_edges)
        for edge in top_level_edges:
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            if from_id is None or to_id is None:
                continue
            if has_detail_network and (
                _semantic_endpoint_is_network_route_config_detail(from_id, visible_node_types)
                or _semantic_endpoint_is_network_route_config_detail(to_id, visible_node_types)
            ):
                continue
            if from_id not in selected_ids and to_id not in selected_ids:
                continue
            missing_ids = [node_id for node_id in (from_id, to_id) if node_id not in selected_ids]
            if any(node_id in selected_group_members for node_id in missing_ids):
                continue
            connects_isolated_selected_node = not missing_ids and (
                from_id not in connected_ids or to_id not in connected_ids
            )
            edge_limit = 8 if connects_isolated_selected_node else 6
            if len(nodes) + len(missing_ids) > 8 or len(repaired_edges) >= edge_limit:
                continue
            for node_id in missing_ids:
                nodes.append(node_id)
                selected_ids.add(node_id)
            edge_key = (from_id, to_id, str(edge.get("kind") or ""), str(edge.get("label") or ""))
            if edge_key in edge_keys:
                continue
            repaired_edges.append(
                {
                    "from": from_id,
                    "to": to_id,
                    "kind": str(edge.get("kind") or "inferred"),
                    "label": str(edge.get("label") or ""),
                }
            )
            edge_keys.add(edge_key)
            connected_ids.add(from_id)
            connected_ids.add(to_id)
        repaired_views.append({**view, "nodes": nodes, "edges": repaired_edges})
    return repaired_views


def _drop_empty_overview_containers_from_views(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    container_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("containers"))
        if isinstance(item.get("id"), str)
    }
    if not container_ids:
        return views
    containment_parent = {
        str(item.get("resource")): str(item.get("container"))
        for item in _list_of_dicts(architecture_context.get("containment"))
        if isinstance(item.get("resource"), str) and isinstance(item.get("container"), str)
    }
    container_parent = {
        str(item.get("id")): str(item.get("parent"))
        for item in _list_of_dicts(architecture_context.get("containers"))
        if isinstance(item.get("id"), str) and isinstance(item.get("parent"), str)
    }

    repaired_views: list[dict[str, Any]] = []
    for view in views:
        if str(view.get("id") or "") != "overview":
            repaired_views.append(view)
            continue
        nodes = _dedupe_strings(view.get("nodes"))
        if not any(node_id in container_ids for node_id in nodes):
            repaired_views.append(view)
            continue
        selected_ids = set(nodes)
        connected_ids: set[str] = set()
        for edge in _list_of_dicts(view.get("edges")):
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            if from_id in selected_ids:
                connected_ids.add(from_id)
            if to_id in selected_ids:
                connected_ids.add(to_id)
        groups = _list_of_dicts(view.get("groups"))
        repaired_nodes = [
            node_id
            for node_id in nodes
            if node_id not in container_ids
            or node_id in connected_ids
            or _overview_container_has_selected_real_child(
                node_id,
                selected_ids - container_ids,
                containment_parent,
                container_parent,
            )
            or _overview_container_has_selected_group_parent(node_id, selected_ids, groups, container_parent)
        ]
        repaired_views.append({**view, "nodes": repaired_nodes})
    return repaired_views


def _overview_container_has_selected_real_child(
    container_id: str,
    selected_node_ids: set[str],
    containment_parent: dict[str, str],
    container_parent: dict[str, str],
) -> bool:
    for selected_node_id in selected_node_ids:
        parent_id = containment_parent.get(selected_node_id)
        if parent_id and _semantic_container_is_descendant_or_same(parent_id, container_id, container_parent):
            return True
    return False


def _overview_container_has_selected_group_parent(
    container_id: str,
    selected_ids: set[str],
    groups: list[dict[str, Any]],
    container_parent: dict[str, str],
) -> bool:
    for group in groups:
        group_id = group.get("id")
        parent_id = group.get("parent")
        if (
            isinstance(group_id, str)
            and group_id in selected_ids
            and isinstance(parent_id, str)
            and _semantic_container_is_descendant_or_same(parent_id, container_id, container_parent)
        ):
            return True
    return False


def _drop_isolated_non_anchor_detail_nodes_from_views(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repaired_views: list[dict[str, Any]] = []
    for view in views:
        view_id = str(view.get("id") or "")
        view_edges = _list_of_dicts(view.get("edges"))
        if not view_id.startswith("detail_") or not view_edges:
            repaired_views.append(view)
            continue
        edge_endpoints: set[str] = set()
        for edge in view_edges:
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            if from_id is not None:
                edge_endpoints.add(from_id)
            if to_id is not None:
                edge_endpoints.add(to_id)
        anchors = set(_dedupe_strings(view.get("anchors")))
        nodes = _dedupe_strings(view.get("nodes"))
        kept_nodes = [node_id for node_id in nodes if node_id in edge_endpoints or node_id in anchors]
        if kept_nodes == nodes:
            repaired_views.append(view)
            continue
        repaired_view = dict(view)
        repaired_view["nodes"] = kept_nodes
        repaired_views.append(repaired_view)
    return repaired_views


def _repair_external_cen_network_view_edges(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    external_attachments = _semantic_external_cen_network_attachments(architecture_context)
    if not external_attachments:
        return views
    attached_child_resources = {
        str(attachment.get("child_resource"))
        for attachment in _list_of_dicts(architecture_context.get("network_attachments"))
        if attachment.get("type") == "ALIYUN::CEN::CenInstanceAttachment"
        and isinstance(attachment.get("child_resource"), str)
        and str(attachment.get("child_resource")).strip()
    }
    if not attached_child_resources:
        return views
    cen_config_ids = _semantic_selected_cen_config_ids(
        architecture_context,
        {
            str(item.get("id"))
            for item in _list_of_dicts(architecture_context.get("visible_nodes"))
            if isinstance(item.get("id"), str)
        },
    )
    if not cen_config_ids:
        return views
    replacement_label = _external_cen_network_edge_label(external_attachments)
    repaired_views: list[dict[str, Any]] = []
    for view in views:
        repaired_edges: list[dict[str, Any]] = []
        for edge in _list_of_dicts(view.get("edges")):
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            label = str(edge.get("label") or "")
            connects_local_child_to_cen = (
                from_id in attached_child_resources
                and to_id in cen_config_ids
                or to_id in attached_child_resources
                and from_id in cen_config_ids
            )
            if connects_local_child_to_cen and not _semantic_text_mentions_external_cen_network(label):
                edge = {**edge, "label": replacement_label}
            repaired_edges.append(edge)
        repaired_views.append({**view, "edges": repaired_edges})
    return repaired_views


def _external_cen_network_edge_label(external_attachments: list[dict[str, Any]]) -> str:
    child_types = {
        str(attachment.get("child_instance_type") or "").upper()
        for attachment in external_attachments
        if attachment.get("child_instance_type")
    }
    if "VPC" in child_types and "VBR" in child_types:
        return "连接外部VPC/VBR"
    if "VBR" in child_types:
        return "连接外部VBR"
    if "VPC" in child_types:
        return "连接外部VPC"
    return "连接外部网络"


def _drop_nat_to_load_balancer_edges_from_views(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    visible_node_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    repaired_views: list[dict[str, Any]] = []
    for view in views:
        group_members = _semantic_view_group_members(view)
        group_parents = _semantic_view_group_parents(view)
        kept_edges: list[dict[str, Any]] = []
        dropped_nat_ids: set[str] = set()
        for edge in _list_of_dicts(view.get("edges")):
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            if from_id is None or to_id is None:
                continue
            if _semantic_edge_connects_nat_and_load_balancer(
                from_id,
                to_id,
                visible_node_types,
                group_members,
                group_parents,
            ):
                nat_endpoint = _semantic_nat_endpoint_id(
                    from_id,
                    to_id,
                    visible_node_types,
                    group_members,
                    group_parents,
                )
                if nat_endpoint is not None:
                    dropped_nat_ids.add(nat_endpoint)
                continue
            kept_edges.append(edge)
        if not dropped_nat_ids:
            repaired_views.append(view)
            continue
        repaired_view = {**view, "edges": kept_edges}
        if str(view.get("id") or "") == "overview":
            connected_ids = _edge_endpoint_ids(kept_edges)
            repaired_view["nodes"] = [
                node_id
                for node_id in _dedupe_strings(view.get("nodes"))
                if node_id not in dropped_nat_ids or node_id in connected_ids
            ]
        repaired_views.append(repaired_view)
    return repaired_views


def _repair_transit_router_source_edges_from_views(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    endpoint_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in [
            *_list_of_dicts(architecture_context.get("visible_nodes")),
            *_list_of_dicts(architecture_context.get("containers")),
        ]
        if isinstance(item.get("id"), str)
    }
    repaired_views: list[dict[str, Any]] = []
    for view in views:
        if str(view.get("id") or "") != "detail_network":
            repaired_views.append(view)
            continue
        repaired_edges: list[dict[str, Any]] = []
        for edge in _list_of_dicts(view.get("edges")):
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            if from_id is None or to_id is None:
                continue
            label = str(edge.get("label") or "")
            kind = str(edge.get("kind") or "")
            if (
                _semantic_endpoint_is_transit_router(from_id, endpoint_types)
                and not _semantic_endpoint_is_transit_router(to_id, endpoint_types)
                and (kind == "traffic" or _semantic_label_mentions_vpc_connection(label) or "路由" in label)
            ):
                if _find_existing_edge_index(repaired_edges, to_id, from_id) is None:
                    repaired_edges.append(
                        {
                            "from": to_id,
                            "to": from_id,
                            "kind": "dependency",
                            "label": "CEN 接入",
                        }
                    )
                continue
            repaired_edges.append(edge)
        repaired_views.append({**view, "edges": repaired_edges})
    return repaired_views


def _repair_split_route_forwarder_chain_edges(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not _list_of_dicts(architecture_context.get("network_attachments")):
        return views
    forwarder_ids = _semantic_route_next_hop_compute_ids(architecture_context)
    if not forwarder_ids:
        return views
    visible_node_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }

    repaired_views: list[dict[str, Any]] = []
    for view in views:
        if str(view.get("id") or "") != "detail_network":
            repaired_views.append(view)
            continue

        selected_ids = _semantic_view_selected_ids(view)
        selected_forwarders = sorted(forwarder_ids & selected_ids)
        transit_ids = {
            selected_id
            for selected_id in selected_ids
            if _semantic_endpoint_is_transit_router(selected_id, visible_node_types)
        }
        if not selected_forwarders or not transit_ids:
            repaired_views.append(view)
            continue

        repaired_edges = [dict(edge) for edge in _list_of_dicts(view.get("edges"))]
        cen_route_config_ids = _semantic_route_config_ids_adjacent_to_targets(
            selected_ids,
            transit_ids,
            repaired_edges,
            visible_node_types,
        )
        route_config_ids = _semantic_return_route_config_target_ids(
            selected_ids,
            cen_route_config_ids,
            repaired_edges,
            visible_node_types,
        )
        changed = False
        for forwarder_id in selected_forwarders:
            target_id = _first_disconnected_route_chain_target(
                forwarder_id,
                route_config_ids,
                set(),
                repaired_edges,
            )
            if target_id is None:
                if _semantic_view_has_path(forwarder_id, transit_ids, repaired_edges):
                    continue
                target_id = _first_disconnected_route_chain_target(
                    forwarder_id,
                    [],
                    transit_ids,
                    repaired_edges,
                )
            if target_id is None or _find_existing_edge_index(repaired_edges, forwarder_id, target_id) is not None:
                continue
            repaired_edges.append(
                {
                    "from": forwarder_id,
                    "to": target_id,
                    "kind": "dependency",
                    "label": "回程路由" if target_id in route_config_ids else "转发至 CEN",
                }
            )
            changed = True
            if len(repaired_edges) >= 8:
                break
        if changed:
            repaired_views.append({**view, "edges": repaired_edges[:8]})
        else:
            repaired_views.append(view)
    return repaired_views


def _repair_cross_network_detail_edge_labels(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    visible_node_types = _visible_node_types_by_id(architecture_context)
    containment_parent = _semantic_resource_container_map(architecture_context)
    container_parent = _semantic_container_parent_map(architecture_context)
    repaired_views: list[dict[str, Any]] = []
    for view in views:
        if str(view.get("id") or "") != "detail_network":
            repaired_views.append(view)
            continue
        selected_ids = _semantic_view_selected_ids(view)
        if not _semantic_view_has_transit_router(selected_ids, visible_node_types):
            repaired_views.append(view)
            continue
        group_members = _semantic_view_group_members(view)
        group_parents = _semantic_view_group_parents(view)
        repaired_edges: list[dict[str, Any]] = []
        changed = False
        for edge in _list_of_dicts(view.get("edges")):
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            label = str(edge.get("label") or "")
            if (
                from_id is not None
                and to_id is not None
                and label
                and not _semantic_label_mentions_cross_network(label)
                and _semantic_edge_crosses_network_domains(
                    from_id,
                    to_id,
                    visible_node_types,
                    containment_parent,
                    container_parent,
                    group_members,
                    group_parents,
                )
            ):
                edge = {**edge, "label": _prefix_cen_edge_label(label)}
                changed = True
            repaired_edges.append(edge)
        repaired_views.append({**view, "edges": repaired_edges} if changed else view)
    return repaired_views


def _prefix_cen_edge_label(label: str) -> str:
    cleaned = _single_line_label(label, 14).strip()
    if not cleaned:
        return "经 CEN"
    if _semantic_label_mentions_cross_network(cleaned):
        return cleaned
    return _single_line_label(f"经 CEN {cleaned}", 18)


def _semantic_route_config_ids_adjacent_to_targets(
    selected_ids: set[str],
    target_ids: set[str],
    view_edges: list[dict[str, Any]],
    visible_node_types: dict[str, str],
) -> list[str]:
    route_config_ids: list[str] = []
    seen: set[str] = set()
    for edge in view_edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id is None or to_id is None:
            continue
        other_id: str | None = None
        if from_id in target_ids:
            other_id = to_id
        elif to_id in target_ids:
            other_id = from_id
        if (
            other_id is not None
            and other_id in selected_ids
            and other_id not in seen
            and _semantic_endpoint_is_network_route_config_detail(other_id, visible_node_types)
        ):
            route_config_ids.append(other_id)
            seen.add(other_id)
    return route_config_ids


def _semantic_return_route_config_target_ids(
    selected_ids: set[str],
    cen_route_config_ids: list[str],
    view_edges: list[dict[str, Any]],
    visible_node_types: dict[str, str],
) -> list[str]:
    route_config_ids: list[str] = []
    seen: set[str] = set()
    cen_route_config_set = set(cen_route_config_ids)
    for edge in view_edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id is None or to_id is None:
            continue
        other_id: str | None = None
        if from_id in cen_route_config_set:
            other_id = to_id
        elif to_id in cen_route_config_set:
            other_id = from_id
        if (
            other_id is not None
            and other_id in selected_ids
            and other_id not in cen_route_config_set
            and other_id not in seen
            and _semantic_endpoint_is_network_route_config_detail(other_id, visible_node_types)
        ):
            route_config_ids.append(other_id)
            seen.add(other_id)
    for route_config_id in cen_route_config_ids:
        if route_config_id not in seen:
            route_config_ids.append(route_config_id)
            seen.add(route_config_id)
    return route_config_ids


def _first_disconnected_route_chain_target(
    forwarder_id: str,
    route_config_ids: list[str],
    transit_ids: set[str],
    view_edges: list[dict[str, Any]],
) -> str | None:
    for route_config_id in route_config_ids:
        if _find_existing_edge_index(view_edges, forwarder_id, route_config_id) is None:
            return route_config_id
    for transit_id in sorted(transit_ids):
        if not _semantic_view_has_path(forwarder_id, {transit_id}, view_edges):
            return transit_id
    return None


def _semantic_nat_endpoint_id(
    from_id: str,
    to_id: str,
    visible_node_types: dict[str, str],
    group_members: dict[str, list[str]],
    group_parents: dict[str, str],
) -> str | None:
    if _semantic_endpoint_is_nat_gateway(from_id, visible_node_types, group_members, group_parents):
        return from_id
    if _semantic_endpoint_is_nat_gateway(to_id, visible_node_types, group_members, group_parents):
        return to_id
    return None


def _edge_endpoint_ids(edges: list[dict[str, Any]]) -> set[str]:
    endpoint_ids: set[str] = set()
    for edge in edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id is not None:
            endpoint_ids.add(from_id)
        if to_id is not None:
            endpoint_ids.add(to_id)
    return endpoint_ids


def _drop_redundant_detail_views(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    overview = next((view for view in views if str(view.get("id") or "") == "overview"), None)
    if overview is None:
        return views
    return [
        view
        for view in views
        if str(view.get("id") or "") == "overview"
        or (
            not _is_redundant_small_group_detail_view(view, overview)
            and not _is_redundant_endpoint_repeat_detail_view(view, overview)
        )
    ]


def _complete_detail_views_from_overview_edges(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    overview = next((view for view in views if str(view.get("id") or "") == "overview"), None)
    if overview is None:
        return views
    overview_edges = _list_of_dicts(overview.get("edges"))
    if not overview_edges:
        return views
    overview_group_members = _semantic_view_group_members(overview)
    if not overview_group_members:
        overview_group_members = {}

    repaired_views: list[dict[str, Any]] = []
    for view in views:
        view_id = str(view.get("id") or "")
        if not view_id.startswith("detail_"):
            repaired_views.append(view)
            continue
        selected_ids = _semantic_view_selected_ids(view)
        if len(selected_ids) < 2:
            repaired_views.append(view)
            continue
        view_edges = [dict(edge) for edge in _list_of_dicts(view.get("edges"))]
        changed = False
        for overview_edge in overview_edges:
            for from_id, to_id in _project_overview_edge_endpoints_to_detail(
                overview_edge,
                selected_ids,
                overview_group_members,
            ):
                if _find_existing_edge_index(view_edges, from_id, to_id) is not None:
                    continue
                view_edges.append(
                    {
                        "from": from_id,
                        "to": to_id,
                        "kind": str(overview_edge.get("kind") or "inferred"),
                        "label": str(overview_edge.get("label") or ""),
                    }
                )
                changed = True
                if len(view_edges) >= 8:
                    break
            if len(view_edges) >= 8:
                break
        if not changed:
            repaired_views.append(view)
            continue
        repaired_view = dict(view)
        repaired_view["edges"] = view_edges
        repaired_views.append(repaired_view)
    return repaired_views


def _project_overview_edge_endpoints_to_detail(
    overview_edge: dict[str, Any],
    selected_ids: set[str],
    overview_group_members: dict[str, list[str]],
) -> list[tuple[str, str]]:
    from_id = _semantic_edge_endpoint(overview_edge, "from")
    to_id = _semantic_edge_endpoint(overview_edge, "to")
    if from_id is None or to_id is None:
        return []

    from_candidates = _project_overview_endpoint_to_detail(from_id, selected_ids, overview_group_members)
    to_candidates = _project_overview_endpoint_to_detail(to_id, selected_ids, overview_group_members)
    candidates: list[tuple[str, str]] = []
    for candidate_from in from_candidates:
        for candidate_to in to_candidates:
            if candidate_from == candidate_to:
                continue
            candidates.append((candidate_from, candidate_to))
            if len(candidates) >= 4:
                return candidates
    return candidates


def _project_overview_endpoint_to_detail(
    endpoint_id: str,
    selected_ids: set[str],
    overview_group_members: dict[str, list[str]],
) -> list[str]:
    if endpoint_id in selected_ids:
        return [endpoint_id]
    member_ids = overview_group_members.get(endpoint_id)
    if not member_ids:
        return []
    return [member_id for member_id in member_ids if member_id in selected_ids]


def _merge_small_detail_views_into_overview(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    overview = next((view for view in views if str(view.get("id") or "") == "overview"), None)
    if overview is None:
        return views

    merged_overview = overview
    remaining_views: list[dict[str, Any]] = []
    for view in views:
        if view is overview:
            continue
        if _can_merge_small_detail_view_into_overview(architecture_context, view, merged_overview):
            merged_overview = _merge_detail_view_into_overview(merged_overview, view)
        else:
            remaining_views.append(view)
    return [merged_overview, *remaining_views]


def _can_merge_small_detail_view_into_overview(
    architecture_context: dict[str, Any],
    view: dict[str, Any],
    overview: dict[str, Any],
) -> bool:
    view_id = str(view.get("id") or "")
    if view_id != "detail_network":
        return False
    if not _list_of_dicts(architecture_context.get("network_attachments")):
        return False
    if not _list_of_dicts(overview.get("edges")):
        return False
    if _list_of_dicts(view.get("groups")):
        return False

    detail_nodes = _dedupe_strings(view.get("nodes"))
    detail_edges = _list_of_dicts(view.get("edges"))
    if not detail_nodes or not detail_edges:
        return False
    if len(detail_nodes) > 3 or len(detail_edges) > 2:
        return False

    overview_selected_ids = _semantic_view_selected_ids(overview)
    anchor_ids = _semantic_view_anchor_ids(view)
    if anchor_ids and not anchor_ids.issubset(overview_selected_ids):
        return False

    merged = _merge_detail_view_into_overview(overview, view)
    if len(_semantic_view_selected_ids(merged)) > 8:
        return False
    if len(_list_of_dicts(merged.get("edges"))) > 6:
        return False
    return True


def _merge_detail_view_into_overview(overview: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    member_group = _overview_member_group_map(overview)
    merged_nodes = _dedupe_strings(overview.get("nodes"))
    for node_id in _dedupe_strings(view.get("nodes")):
        if node_id in member_group:
            continue
        if node_id not in merged_nodes:
            merged_nodes.append(node_id)

    merged_edges = [dict(edge) for edge in _list_of_dicts(overview.get("edges"))]
    for edge in _list_of_dicts(view.get("edges")):
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id is None or to_id is None:
            continue
        mapped_from = member_group.get(from_id, from_id)
        mapped_to = member_group.get(to_id, to_id)
        if mapped_from == mapped_to:
            continue
        candidate = {**edge, "from": mapped_from, "to": mapped_to}
        existing_index = _find_existing_edge_index(merged_edges, mapped_from, mapped_to)
        if existing_index is None:
            merged_edges.append(candidate)
            continue
        existing = merged_edges[existing_index]
        existing["label"] = _merged_detail_edge_label(
            str(existing.get("label") or ""),
            str(candidate.get("label") or ""),
        )
    return {**overview, "nodes": merged_nodes, "edges": merged_edges}


def _overview_member_group_map(overview: dict[str, Any]) -> dict[str, str]:
    selected_node_ids = set(_dedupe_strings(overview.get("nodes")))
    member_group: dict[str, str] = {}
    for group_id, members in _semantic_view_group_members(overview).items():
        if group_id not in selected_node_ids:
            continue
        for member_id in members:
            member_group.setdefault(member_id, group_id)
    return member_group


def _find_existing_edge_index(edges: list[dict[str, Any]], from_id: str, to_id: str) -> int | None:
    reverse_index: int | None = None
    for index, edge in enumerate(edges):
        edge_from = _semantic_edge_endpoint(edge, "from")
        edge_to = _semantic_edge_endpoint(edge, "to")
        if edge_from == from_id and edge_to == to_id:
            return index
        if edge_from == to_id and edge_to == from_id:
            reverse_index = index
    return reverse_index


def _merged_detail_edge_label(existing_label: str, candidate_label: str) -> str:
    candidate_mentions_external = _semantic_text_mentions_external_cen_network(candidate_label)
    existing_mentions_external = _semantic_text_mentions_external_cen_network(existing_label)
    if candidate_mentions_external and not existing_mentions_external:
        return candidate_label
    if not existing_label:
        return candidate_label
    return existing_label


def _drop_redundant_attachment_marker_nodes_from_views(
    architecture_context: dict[str, Any],
    views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    marker_to_summary = _attachment_marker_layer_summary_ids(architecture_context)
    if not marker_to_summary:
        return views

    repaired_views: list[dict[str, Any]] = []
    for view in views:
        selected_ids = set(_dedupe_strings(view.get("nodes")))
        redundant_markers = {
            marker_id
            for marker_id, summary_id in marker_to_summary.items()
            if marker_id in selected_ids and summary_id in selected_ids
        }
        if not redundant_markers:
            repaired_views.append(view)
            continue

        repaired_view = dict(view)
        repaired_view["nodes"] = [
            node_id for node_id in _dedupe_strings(view.get("nodes")) if node_id not in redundant_markers
        ]
        if "anchors" in repaired_view:
            repaired_view["anchors"] = [
                anchor_id for anchor_id in _dedupe_strings(view.get("anchors")) if anchor_id not in redundant_markers
            ]
        repaired_groups: list[dict[str, Any]] = []
        for group in _list_of_dicts(view.get("groups")):
            group_id = str(group.get("id") or group.get("group_id") or "")
            if group_id in redundant_markers:
                continue
            members = [
                member_id for member_id in _dedupe_strings(group.get("members")) if member_id not in redundant_markers
            ]
            repaired_group = dict(group)
            repaired_group["members"] = members
            repaired_groups.append(repaired_group)
        if "groups" in repaired_view:
            repaired_view["groups"] = repaired_groups
        repaired_edges: list[dict[str, Any]] = []
        for edge in _list_of_dicts(view.get("edges")):
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            if from_id in redundant_markers or to_id in redundant_markers:
                continue
            repaired_edges.append(edge)
        repaired_view["edges"] = repaired_edges
        repaired_views.append(repaired_view)
    return repaired_views


def _attachment_marker_layer_summary_ids(architecture_context: dict[str, Any]) -> dict[str, str]:
    summary_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if item.get("type") == "CONCEPT::Layer::AttachmentSummary" and isinstance(item.get("id"), str)
    }
    marker_to_summary: dict[str, str] = {}
    for attachment in _list_of_dicts(architecture_context.get("attachments")):
        marker_id = attachment.get("marker")
        target_id = attachment.get("target")
        if not isinstance(marker_id, str) or not isinstance(target_id, str):
            continue
        summary_id = _layer_attachment_summary_node_id(target_id)
        if summary_id in summary_ids:
            marker_to_summary[marker_id] = summary_id
    return marker_to_summary


def _layer_attachment_summary_node_id(layer_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", f"layer_{layer_id}_Config")


def _is_redundant_small_group_detail_view(view: dict[str, Any], overview: dict[str, Any]) -> bool:
    view_id = str(view.get("id") or "")
    if not view_id.startswith("detail_") or view_id == "detail_network":
        return False
    if _list_of_dicts(view.get("groups")):
        return False

    overview_group_members = _semantic_view_group_members(overview)
    anchor_ids = _semantic_view_anchor_ids(view)
    expanded_group_ids = sorted(group_id for group_id in anchor_ids if group_id in overview_group_members)
    if not expanded_group_ids:
        return False
    if any(len(overview_group_members[group_id]) > 2 for group_id in expanded_group_ids):
        return False

    detail_node_ids = set(_dedupe_strings(view.get("nodes")))
    expanded_members = {
        member_id for group_id in expanded_group_ids for member_id in overview_group_members.get(group_id, [])
    }
    if not expanded_members or not expanded_members.issubset(detail_node_ids):
        return False
    if len(detail_node_ids) > len(expanded_members) + 2:
        return False

    overview_selected_ids = _semantic_view_selected_ids(overview)
    non_member_ids = detail_node_ids - expanded_members
    if not non_member_ids or not non_member_ids.issubset(overview_selected_ids - set(expanded_group_ids)):
        return False

    detail_edges = _list_of_dicts(view.get("edges"))
    if not detail_edges:
        return False
    overview_edge_labels = _overview_edge_labels_by_endpoint(overview)
    for edge in detail_edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id is None or to_id is None:
            return False
        replacement = _small_group_detail_replacement_edge(
            from_id,
            to_id,
            expanded_group_ids,
            overview_group_members,
            non_member_ids,
        )
        if replacement is None:
            return False
        overview_label = overview_edge_labels.get(replacement)
        if overview_label is None:
            return False
        if not _semantic_labels_equivalent_for_small_group_detail(overview_label, str(edge.get("label") or "")):
            return False
    return True


def _is_redundant_endpoint_repeat_detail_view(view: dict[str, Any], overview: dict[str, Any]) -> bool:
    view_id = str(view.get("id") or "")
    if not view_id.startswith("detail_"):
        return False
    if _list_of_dicts(view.get("groups")):
        return False

    detail_node_ids = set(_dedupe_strings(view.get("nodes")))
    if not detail_node_ids or not detail_node_ids.issubset(_semantic_view_selected_ids(overview)):
        return False

    detail_pairs = _directed_edge_endpoint_pairs(_list_of_dicts(view.get("edges")))
    if not detail_pairs:
        return False
    overview_pairs = _directed_edge_endpoint_pairs(_list_of_dicts(overview.get("edges")))
    return detail_pairs.issubset(overview_pairs)


def _directed_edge_endpoint_pairs(edges: list[dict[str, Any]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for edge in edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id is None or to_id is None:
            continue
        pairs.add((from_id, to_id))
    return pairs


def _overview_edge_labels_by_endpoint(overview: dict[str, Any]) -> dict[tuple[str, str], str]:
    labels: dict[tuple[str, str], str] = {}
    for edge in _list_of_dicts(overview.get("edges")):
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id is None or to_id is None:
            continue
        labels[(from_id, to_id)] = str(edge.get("label") or "")
    return labels


def _small_group_detail_replacement_edge(
    from_id: str,
    to_id: str,
    expanded_group_ids: list[str],
    overview_group_members: dict[str, list[str]],
    non_member_ids: set[str],
) -> tuple[str, str] | None:
    for group_id in expanded_group_ids:
        members = set(overview_group_members.get(group_id, []))
        if from_id in non_member_ids and to_id in members:
            return from_id, group_id
        if from_id in members and to_id in non_member_ids:
            return group_id, to_id
    return None


def _normalized_semantic_label(label: str) -> str:
    return re.sub(r"\s+", "", label).casefold()


def _semantic_labels_equivalent_for_small_group_detail(overview_label: str, detail_label: str) -> bool:
    if _normalized_semantic_label(overview_label) == _normalized_semantic_label(detail_label):
        return True
    return _semantic_label_mentions_business_traffic(overview_label) and _semantic_label_mentions_business_traffic(
        detail_label
    )


def _repair_semantic_views(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
    valid_real_ids: set[str],
    valid_node_ids: set[str],
) -> list[dict[str, Any]]:
    overview_views = [view for view in raw_views if _normalize_semantic_view_id(view.get("id")) == "overview"]
    other_views = [view for view in raw_views if _normalize_semantic_view_id(view.get("id")) != "overview"]
    ordered_views = overview_views + other_views
    repaired_views: list[dict[str, Any]] = []
    overview_ids: set[str] = set()
    needs_placement_overview = _needs_placement_preserving_overview(architecture_context)
    for index, view in enumerate(ordered_views):
        view_id = _normalize_semantic_view_id(view.get("id"))
        if not view_id:
            continue
        if view_id != "overview" and not view_id.startswith("detail_"):
            continue
        repaired_view: dict[str, Any] = {"id": view_id}
        for key in ("title", "purpose"):
            value = view.get(key)
            if isinstance(value, str) and value.strip():
                repaired_view[key] = _single_line_label(value, 48)
        layout = view.get("layout")
        repaired_view["layout"] = (
            layout.strip().lower()
            if isinstance(layout, str)
            and layout.strip().lower()
            in {
                "flat",
                "contained",
            }
            else "flat"
        )
        if view_id == "overview" and needs_placement_overview:
            repaired_view["layout"] = "contained"
        groups = _repair_semantic_groups(_list_of_dicts(view.get("groups")), valid_node_ids, valid_real_ids)
        group_ids = {str(group["id"]) for group in groups}
        known_ids_for_view = valid_real_ids | group_ids
        if groups:
            repaired_view["groups"] = groups
        anchors = _dedupe_strings(view.get("anchors"))
        if view_id.startswith("detail_") and overview_ids:
            anchors = [anchor for anchor in anchors if anchor in overview_ids]
        else:
            anchors = [anchor for anchor in anchors if anchor in valid_real_ids or anchor in overview_ids]
        nodes = [
            node_id
            for node_id in _dedupe_strings(view.get("nodes") or view.get("node_ids") or view.get("container_ids"))
            if node_id in known_ids_for_view
        ]
        max_nodes = 8 if view_id == "overview" else 12
        nodes = nodes[:max_nodes]
        if view_id.startswith("detail_") and not anchors and overview_ids:
            anchors = [node_id for node_id in nodes if node_id in overview_ids]
        if view_id.startswith("detail_") and anchors:
            repaired_view["anchors"] = anchors
        selected_ids = set(nodes)
        view_edges = _repair_semantic_edges(
            _list_of_dicts(view.get("edges")),
            known_ids_for_view,
            max_edges=8,
            require_selected_ids=selected_ids,
        )
        if not nodes and not groups:
            continue
        repaired_view["nodes"] = nodes
        repaired_view["edges"] = view_edges
        repaired_views.append(repaired_view)
        if index == 0 and view_id == "overview":
            overview_ids = selected_ids | group_ids
    return repaired_views


def _normalize_semantic_view_id(raw_view_id: Any) -> str:
    if not isinstance(raw_view_id, str) or not raw_view_id.strip():
        return ""
    view_id = raw_view_id.strip()
    legacy_perspective_ids = {
        "traffic": "detail_app",
        "network": "detail_network",
        "placement": "detail_network",
        "attachments": "detail_network",
        "operations": "detail_operations",
    }
    return legacy_perspective_ids.get(view_id, view_id)


def _repair_semantic_groups(
    raw_groups: list[dict[str, Any]],
    valid_node_ids: set[str],
    valid_real_ids: set[str],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in raw_groups:
        group_id = group.get("id") or group.get("group_id")
        if not isinstance(group_id, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", group_id):
            continue
        if group_id in valid_real_ids or group_id in seen:
            continue
        members = [member for member in _dedupe_strings(group.get("members")) if member in valid_node_ids]
        if not members:
            continue
        cleaned: dict[str, Any] = {"id": group_id, "members": members}
        label = group.get("label")
        if isinstance(label, str) and label.strip():
            cleaned["label"] = _single_line_label(label, 32)
        parent = group.get("parent") or group.get("container")
        if isinstance(parent, str) and parent in valid_real_ids:
            cleaned["parent"] = parent
        groups.append(cleaned)
        seen.add(group_id)
    return groups


def _repair_semantic_edges(
    raw_edges: list[dict[str, Any]],
    known_ids: set[str],
    *,
    max_edges: int,
    require_selected_ids: set[str] | None,
) -> list[dict[str, Any]]:
    repaired_edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in raw_edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id not in known_ids or to_id not in known_ids:
            continue
        if require_selected_ids is not None and (
            from_id not in require_selected_ids or to_id not in require_selected_ids
        ):
            continue
        label = edge.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        kind = edge.get("kind")
        kind = (
            kind
            if isinstance(kind, str) and kind in {"traffic", "dependency", "management", "inferred"}
            else "inferred"
        )
        edge_key = (from_id, to_id, kind)
        if edge_key in seen:
            continue
        cleaned: dict[str, Any] = {
            "from": from_id,
            "to": to_id,
            "kind": kind,
            "label": _single_line_label(label, 18),
        }
        confidence = edge.get("confidence")
        if isinstance(confidence, str) and confidence in {"high", "medium", "low"}:
            cleaned["confidence"] = confidence
        repaired_edges.append(cleaned)
        seen.add(edge_key)
        if len(repaired_edges) >= max_edges:
            break
    return repaired_edges


def _dedupe_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def _single_line_label(value: str, max_chars: int) -> str:
    label = re.sub(r"\s+", " ", value.replace("\\n", " ").replace("\n", " ")).strip()
    return label[:max_chars].rstrip()


def should_retry_semantic_plan_attempt(attempt: int, max_attempts: int, validation_issues: list[str]) -> bool:
    if not validation_issues or attempt >= max_attempts:
        return False
    if attempt < 2:
        return True
    return any(_is_severe_semantic_plan_issue(issue) for issue in validation_issues)


def _is_severe_semantic_plan_issue(issue: str) -> bool:
    return any(
        marker in issue
        for marker in (
            "LLM output was not valid semantic_plan JSON",
            "semantic_plan has no edges",
            "complex architecture should define overview",
            "complex architecture views must start with overview",
            "complex architecture views must include overview",
            "complex architecture views must include at least one detail_",
            "network-heavy architecture should include a detail_network",
            "view overview has isolated nodes",
            "view detail_network uses contained network containers",
            "view detail_network should include CEN attached resources",
            "view detail_network should explain external CEN networks",
            "route it through CEN/TransitRouter",
            "crosses VPCs and should mention CEN or cross-VPC",
            "uses CEN/TransitRouter as a business traffic endpoint",
            "connects CEN/TransitRouter to a load balancer",
            "NAT/SNAT should not be drawn as load balancer ingress",
            "makes CEN/TransitRouter the visual source",
            "network route/config detail nodes",
            "repeats CEN/TransitRouter both as an overview node and as a traffic label",
        )
    )


def validation_issue_score(validation_issues: list[str]) -> tuple[int, int]:
    severe_count = sum(1 for issue in validation_issues if _is_severe_semantic_plan_issue(issue))
    return severe_count, len(validation_issues)


async def create_semantic_plan_with_llm(
    architecture_context: dict[str, Any],
    *,
    model: str,
    max_tokens: int,
    effort_override: str | None = None,
    user_prompt: str | None = None,
    messages: list[Message] | None = None,
    attempt: int | None = None,
    previous_plan: dict[str, Any] | None = None,
    validation_issues: list[str] | tuple[str, ...] = (),
    credentials_override: dict[str, str] | None = None,
    provider_key_override: str | None = None,
    base_url_override: str | None = None,
    provider_config_override: dict[str, Any] | None = None,
    ignore_llm_source: bool = False,
) -> tuple[str, dict[str, Any] | None, Any, str | None]:
    if messages is None and user_prompt is None:
        user_prompt = build_semantic_plan_user_prompt(
            architecture_context,
            attempt=attempt,
            previous_plan=previous_plan,
            validation_issues=validation_issues,
        )
    if messages is None:
        messages = [Message.user(user_prompt or "")]
    manager = ProviderManager(
        model=model,
        credentials=credentials_override if credentials_override is not None else load_credentials(model=model),
        effort_override=effort_override,
        provider_key_override=provider_key_override,
        base_url_override=base_url_override,
        provider_config_override=provider_config_override,
        ignore_llm_source=ignore_llm_source,
    )
    response = await manager.complete(
        messages,
        SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    semantic_plan, parse_error = try_extract_semantic_plan_json(response.text)
    return response.text, semantic_plan, response.usage, parse_error


def _resolve_semantic_plan_effort(model: str, effort_override: str | None) -> str | None:
    if effort_override != "none":
        return effort_override
    provider_key = get_active_provider_key()
    if not provider_key:
        return effort_override
    spec = get_thinking_spec(provider_key, model)
    if spec.supports_disable or not spec.allowed_efforts:
        return effort_override
    return spec.allowed_efforts[0].value


async def create_semantic_plan_for_architecture_with_llm(
    architecture_context: dict[str, Any],
    template_content: str,
    *,
    model: str | None = None,
    max_tokens: int = 3000,
    max_attempts: int = MAX_SEMANTIC_PLAN_ATTEMPTS,
    effort_override: str | None = "none",
    credentials_override: dict[str, str] | None = None,
    provider_key_override: str | None = None,
    base_url_override: str | None = None,
    provider_config_override: dict[str, Any] | None = None,
    ignore_llm_source: bool = False,
) -> dict[str, Any]:
    """Create a repaired semantic plan using the same LLM loop as the preview script."""
    llm_architecture_context = build_llm_architecture_context(architecture_context)
    selected_model = model or load_saved_model() or DEFAULT_MODEL
    effective_effort = _resolve_semantic_plan_effort(selected_model, effort_override)
    max_attempts = max(1, max_attempts)

    validation_issues: list[str] = []
    conversation_messages: list[Message] = []
    best_semantic_plan: dict[str, Any] = {}
    best_validation_issues: list[str] | None = None

    for attempt in range(1, max_attempts + 1):
        sent_validation_issues = list(validation_issues)
        user_prompt = build_semantic_plan_user_prompt(
            llm_architecture_context,
            attempt=attempt,
            previous_plan=None,
            validation_issues=sent_validation_issues,
            include_fact_bundle=attempt == 1,
            include_previous_plan=False,
        )
        request_messages = [*conversation_messages, Message.user(user_prompt)]
        raw_output, parsed_semantic_plan, _usage, parse_error = await create_semantic_plan_with_llm(
            llm_architecture_context,
            model=selected_model,
            max_tokens=max_tokens,
            effort_override=effective_effort,
            user_prompt=user_prompt,
            messages=request_messages,
            attempt=attempt,
            previous_plan=None,
            validation_issues=sent_validation_issues,
            credentials_override=credentials_override,
            provider_key_override=provider_key_override,
            base_url_override=base_url_override,
            provider_config_override=provider_config_override,
            ignore_llm_source=ignore_llm_source,
        )
        conversation_messages = [*request_messages, Message.assistant_text(raw_output)]

        if parsed_semantic_plan is None:
            validation_issues = [f"LLM output was not valid semantic_plan JSON: {parse_error}"]
            if not should_retry_semantic_plan_attempt(attempt, max_attempts, validation_issues):
                break
            continue

        semantic_plan = repair_semantic_plan_locally(architecture_context, parsed_semantic_plan)
        rendered = render_ros_template_architecture(template_content, semantic_plan=semantic_plan)
        validation_issues = validate_semantic_plan_result(
            architecture_context,
            semantic_plan,
            rendered.architecture_context,
        )
        if best_validation_issues is None or validation_issue_score(validation_issues) < validation_issue_score(
            best_validation_issues
        ):
            best_semantic_plan = semantic_plan
            best_validation_issues = validation_issues
        if not should_retry_semantic_plan_attempt(attempt, max_attempts, validation_issues):
            break

    return best_semantic_plan


def _semantic_plan_relationship_edges(
    raw_edges: list[dict[str, Any]],
    raw_views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    relationship_edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in raw_edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        label = str(edge.get("label") or "")
        if not from_id or not to_id:
            continue
        key = (from_id, to_id, label)
        if key in seen:
            continue
        relationship_edges.append(edge)
        seen.add(key)
    for view in raw_views:
        for edge in _list_of_dicts(view.get("edges")):
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            label = str(edge.get("label") or "")
            if not from_id or not to_id:
                continue
            key = (from_id, to_id, label)
            if key in seen:
                continue
            relationship_edges.append(edge)
            seen.add(key)
    return relationship_edges


def validate_semantic_plan_result(
    architecture_context: dict[str, Any],
    semantic_plan: dict[str, Any],
    rendered_context: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    semantic_context = rendered_context.get("semantic_plan")
    semantic_context = semantic_context if isinstance(semantic_context, dict) else {}

    for rejected in _list_of_dicts(semantic_context.get("rejected_node_labels")):
        node_id = rejected.get("id") or "<missing>"
        reason = rejected.get("reason") or "rejected"
        issues.append(f"node label for {node_id} was rejected: {reason}")
    for rejected in _list_of_dicts(semantic_context.get("rejected_edges")):
        from_id = rejected.get("from") or "<missing>"
        to_id = rejected.get("to") or "<missing>"
        reason = rejected.get("reason") or "rejected"
        if reason in {"covered by deterministic edge", "covered by scaled concept"}:
            continue
        issues.append(f"edge {from_id}->{to_id} was rejected: {reason}")

    raw_node_labels = _list_of_dicts(semantic_plan.get("node_labels"))
    raw_edges = _list_of_dicts(semantic_plan.get("edges"))
    raw_views = _list_of_dicts(semantic_plan.get("views"))
    relationship_edges = _semantic_plan_relationship_edges(raw_edges, raw_views)
    if len(raw_node_labels) > 24:
        issues.append(f"semantic_plan has {len(raw_node_labels)} node_labels; keep at most 24")
    if len(raw_edges) > 16:
        issues.append(f"semantic_plan has {len(raw_edges)} edges; keep at most 16")
    if not relationship_edges and _needs_semantic_relationships(architecture_context):
        issues.append("semantic_plan has no edges; include 2-6 key traffic, management, or dependency relationships")
    issues.extend(_semantic_view_plan_issues(architecture_context, raw_views, relationship_edges))
    issues.extend(_semantic_ram_governance_issues(architecture_context, raw_views, relationship_edges))
    issues.extend(_semantic_ack_application_issues(architecture_context, raw_views, relationship_edges))
    issues.extend(_semantic_shared_orchestration_dependency_issues(architecture_context, raw_views, relationship_edges))
    issues.extend(_semantic_scaling_configuration_source_issues(architecture_context, raw_views, relationship_edges))
    accepted_node_labels_by_id = {
        str(accepted.get("id")): str(accepted.get("label") or "")
        for accepted in _list_of_dicts(semantic_context.get("accepted_node_labels"))
        if isinstance(accepted.get("id"), str)
    }
    issues.extend(
        _semantic_generic_network_route_domain_label_issues(
            architecture_context,
            raw_views,
            accepted_node_labels_by_id,
        )
    )
    issues.extend(_semantic_external_cen_network_issues(architecture_context, raw_views, accepted_node_labels_by_id))
    issues.extend(_semantic_route_next_hop_forwarder_issues(architecture_context, raw_views))

    target_language = architecture_context.get("target_language")
    target_code = target_language.get("code") if isinstance(target_language, dict) else None
    hint_values = _node_hint_values_by_id(architecture_context)

    for accepted in _list_of_dicts(semantic_context.get("accepted_node_labels")):
        node_id = str(accepted.get("id") or "")
        label = str(accepted.get("label") or "")
        if len(label) > 32:
            issues.append(f"node label for {node_id} is too long: {label}")
        if target_code == "zh" and _needs_chinese_role_label(label):
            issues.append(f"node label for {node_id} must be a Chinese role label, got: {label}")
        if _copies_raw_identifier_hint(label, hint_values.get(node_id, ())):
            issues.append(f"node label for {node_id} copies raw identifier-like name: {label}")

    for accepted in _list_of_dicts(semantic_context.get("accepted_edges")):
        from_id = accepted.get("from") or "<missing>"
        to_id = accepted.get("to") or "<missing>"
        label = str(accepted.get("label") or "")
        if len(label) > 18:
            issues.append(f"edge label for {from_id}->{to_id} is too long: {label}")
        if "\n" in label or "\\n" in label:
            issues.append(f"edge label for {from_id}->{to_id} must be single-line: {label}")
        if target_code == "zh" and label and not _contains_cjk(label):
            issues.append(f"edge label for {from_id}->{to_id} must use Chinese: {label}")
        if _looks_reversed_scaling_source_edge(architecture_context, str(from_id), str(to_id), label):
            issues.append(
                "ESS scaling configuration source must be drawn from the ECS/template source to the ESS scaling group, "
                f"not {from_id}->{to_id}: {label}"
            )
    accepted_edges = _list_of_dicts(semantic_context.get("accepted_edges"))
    issues.extend(_repeated_edge_shape_issues(architecture_context, accepted_edges))

    return issues


def _semantic_view_plan_issues(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
    raw_edges: list[dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    needs_drilldown = _needs_overview_with_drilldown_views(architecture_context, raw_edges)
    if needs_drilldown and not raw_views:
        issues.append("complex architecture should define overview plus drill-down detail views")
        return issues

    valid_node_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    valid_container_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("containers"))
        if isinstance(item.get("id"), str)
    }
    valid_ids = valid_node_ids | valid_container_ids
    visible_node_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    containment_parent = _semantic_resource_container_map(architecture_context)
    container_parent = _semantic_container_parent_map(architecture_context)
    overview_group_ids = _semantic_overview_group_ids(raw_views)
    overview_group_parents = _semantic_overview_group_parents(raw_views)
    needs_non_network_drilldown = _needs_non_network_overview_with_drilldown_views(
        architecture_context,
        raw_edges,
    )
    needs_network_drilldown = _needs_network_drilldown_view(architecture_context)
    needs_placement_overview = _needs_placement_preserving_overview(architecture_context)
    small_network_covered_by_overview = _small_network_detail_is_covered_by_overview(
        architecture_context,
        raw_views,
    )

    seen_view_ids: set[str] = set()
    detail_view_count = 0
    overview_selected_ids = _semantic_overview_selected_ids(raw_views)
    has_detail_network_view = any(str(view.get("id") or "").strip() == "detail_network" for view in raw_views)
    overview_view = _semantic_view_by_id(raw_views, "overview")
    overview_group_members = _semantic_view_group_members(overview_view) if overview_view is not None else {}
    for index, view in enumerate(raw_views, start=1):
        view_id = view.get("id")
        if not isinstance(view_id, str) or not view_id.strip():
            view_id = f"view_{index}"
            issues.append(f"view {index} is missing id")
        view_id = view_id.strip()
        if view_id in seen_view_ids:
            issues.append(f"view {view_id} is duplicated")
        seen_view_ids.add(view_id)
        if index == 1 and needs_drilldown and view_id != "overview":
            issues.append("complex architecture views must start with overview")
        if view_id in {"traffic", "network", "placement", "operations", "attachments"}:
            issues.append(f"view {view_id} is a perspective view; use overview plus detail_<area> drill-down views")
        if view_id != "overview" and not view_id.startswith("detail_"):
            issues.append(f"view {view_id} should be named detail_<area> unless it is overview")
        if view_id.startswith("detail_"):
            detail_view_count += 1
        group_ids = _semantic_view_group_ids(view)
        group_members = _semantic_view_group_members(view)
        group_parents = _semantic_view_group_parents(view)
        known_ids_for_view = valid_ids | group_ids
        for group_id in group_ids:
            if group_id in valid_ids:
                issues.append(f"view {view_id} group {group_id} conflicts with a real resource id")
        for group_id, member_ids in group_members.items():
            if not member_ids:
                issues.append(f"view {view_id} group {group_id} must include members")
            for member_id in member_ids:
                if member_id not in valid_node_ids:
                    issues.append(f"view {view_id} group {group_id} references unknown member {member_id}")
        for group_id, parent_id in group_parents.items():
            if parent_id not in valid_container_ids:
                issues.append(f"view {view_id} group {group_id} references unknown parent {parent_id}")
        anchor_ids = _semantic_view_anchor_ids(view)
        for anchor_id in anchor_ids:
            if anchor_id not in valid_ids and anchor_id not in overview_selected_ids:
                issues.append(f"view {view_id} references unknown anchor {anchor_id}")
        if view_id.startswith("detail_"):
            if not anchor_ids:
                issues.append(f"view {view_id} must include anchors from overview")
            for anchor_id in sorted(anchor_ids):
                if overview_selected_ids and anchor_id not in overview_selected_ids:
                    issues.append(f"view {view_id} anchor {anchor_id} is not present in overview")

        selected_ids = _semantic_view_selected_ids(view)
        if not selected_ids:
            issues.append(f"view {view_id} has no nodes")
        max_nodes = 8 if view_id == "overview" else 12
        if len(selected_ids) > max_nodes:
            issues.append(f"view {view_id} has {len(selected_ids)} nodes; keep at most {max_nodes}")
        for selected_id in selected_ids:
            if selected_id not in known_ids_for_view:
                issues.append(f"view {view_id} references unknown node {selected_id}")
        if view_id == "detail_network":
            missing_domain_anchors = sorted(
                anchor_id
                for anchor_id in anchor_ids
                if (
                    anchor_id in overview_group_ids
                    and anchor_id in overview_group_parents
                    and anchor_id not in selected_ids
                    and overview_group_parents[anchor_id] not in selected_ids
                )
            )
            if missing_domain_anchors:
                issues.append(
                    "view detail_network should include anchored network domains "
                    f"{', '.join(missing_domain_anchors)} as nodes/groups"
                )
            missing_attached_resources = _semantic_missing_detail_network_attached_child_resources(
                architecture_context,
                selected_ids,
                valid_ids,
            )
            if missing_attached_resources:
                issues.append(
                    "view detail_network should include CEN attached resources "
                    f"{', '.join(missing_attached_resources)} from network_attachments as flat VPC/domain nodes"
                )
            if _semantic_detail_network_missing_multivswitch_domains(
                architecture_context,
                selected_ids,
                group_parents,
            ):
                issues.append(
                    "view detail_network should include VSwitch/network domains for multi-VSwitch architecture"
                )
        layout = view.get("layout")
        if isinstance(layout, str) and layout.strip() and layout.strip().lower() not in {"flat", "contained"}:
            issues.append(f"view {view_id} has invalid layout {layout}; use flat or contained")
        if view_id == "detail_network" and isinstance(layout, str) and layout.strip().lower() == "contained":
            expanding_containers = _semantic_contained_network_detail_application_containers(
                architecture_context,
                selected_ids,
                visible_node_types,
                containment_parent,
                container_parent,
            )
            if expanding_containers:
                issues.append(
                    "view detail_network uses contained network containers "
                    f"{', '.join(expanding_containers)} that expand application resources; "
                    "use flat VPC/domain summary nodes instead"
                )
        if view_id == "overview" and needs_network_drilldown:
            if not isinstance(layout, str) or layout.strip().lower() != "contained":
                issues.append("network-heavy overview should use layout=contained to preserve VPC/resource placement")
            repeated_ids_by_type = _semantic_repeated_real_node_types(selected_ids, visible_node_types)
            for resource_type, node_ids in repeated_ids_by_type.items():
                issues.append(
                    f"view overview repeats equivalent {resource_type} nodes {', '.join(node_ids)}; "
                    "use a groups summary node"
                )
            if has_detail_network_view:
                route_config_ids = sorted(
                    selected_id
                    for selected_id in selected_ids
                    if _semantic_endpoint_is_network_route_config_detail(selected_id, visible_node_types)
                )
                if route_config_ids:
                    issues.append(
                        "view overview includes network route/config detail nodes "
                        f"{', '.join(route_config_ids)}; move route/config domains to detail_network"
                    )
        if view_id == "overview" and needs_placement_overview:
            if not isinstance(layout, str) or layout.strip().lower() != "contained":
                issues.append(
                    "placement-sensitive overview should use layout=contained to preserve VPC/VSwitch placement"
                )

        view_edges = _list_of_dicts(view.get("edges"))
        if view_id == "overview" and needs_drilldown and len(selected_ids) > 1 and not view_edges:
            issues.append("view overview has no edges; connect the main domains/components or move them to detail")
        if len(view_edges) > 8:
            issues.append(f"view {view_id} has {len(view_edges)} edges; keep at most 8")
        issues.extend(
            _semantic_detail_app_ingress_source_issues(
                view_id,
                view,
                overview_view,
                overview_group_members,
                visible_node_types,
            )
        )
        has_transit_router = _semantic_view_has_transit_router(selected_ids, visible_node_types)
        for edge in view_edges:
            from_id = _semantic_edge_endpoint(edge, "from")
            to_id = _semantic_edge_endpoint(edge, "to")
            if from_id is None or to_id is None:
                issues.append(f"view {view_id} has an edge with missing endpoint")
                continue
            if from_id not in selected_ids or to_id not in selected_ids:
                issues.append(f"view {view_id} edge {from_id}->{to_id} references nodes outside the view")
            for endpoint_id in (from_id, to_id):
                if endpoint_id not in known_ids_for_view:
                    issues.append(f"view {view_id} references unknown node {endpoint_id}")
            label = str(edge.get("label") or "")
            kind = str(edge.get("kind") or "")
            connects_cen_and_lb = _semantic_edge_connects_transit_router_and_load_balancer(
                from_id,
                to_id,
                visible_node_types,
                group_members,
                group_parents,
            )
            if connects_cen_and_lb:
                if view_id == "detail_network" or _semantic_label_mentions_vpc_connection(label):
                    issues.append(
                        f"view {view_id} edge {from_id}->{to_id} connects CEN/TransitRouter to a load balancer; "
                        "use a VPC/domain/route summary endpoint instead"
                    )
                elif kind == "traffic" or _semantic_label_mentions_business_traffic(label):
                    issues.append(
                        f"view {view_id} edge {from_id}->{to_id} uses CEN/TransitRouter as a business traffic "
                        "endpoint; draw business traffic to the backend load balancer/group and keep CEN as "
                        "VPC underlay"
                    )
                else:
                    issues.append(
                        f"view {view_id} edge {from_id}->{to_id} connects CEN/TransitRouter to a load balancer; "
                        "use a VPC/domain/route summary endpoint instead"
                    )
            elif (
                view_id == "overview"
                and _semantic_endpoint_is_transit_router(from_id, visible_node_types)
                and _semantic_label_mentions_vpc_connection(label)
            ):
                issues.append(
                    f"view overview edge {from_id}->{to_id} makes CEN/TransitRouter the visual source of VPC "
                    "connectivity; reverse it to VPC/domain -> CEN or keep CEN only in the traffic label"
                )
            elif (
                view_id == "overview"
                and has_transit_router
                and kind == "traffic"
                and _semantic_label_mentions_transit_path(label)
                and not _semantic_endpoint_is_transit_router(from_id, visible_node_types)
                and not _semantic_endpoint_is_transit_router(to_id, visible_node_types)
            ):
                issues.append(
                    f"view overview edge {from_id}->{to_id} repeats CEN/TransitRouter both as an overview node "
                    "and as a traffic label; keep CEN/TransitRouter in detail_network"
                )
            if _semantic_edge_connects_nat_and_load_balancer(
                from_id,
                to_id,
                visible_node_types,
                group_members,
                group_parents,
            ):
                issues.append(
                    f"view {view_id} edge {from_id}->{to_id} connects NAT/SNAT to a load balancer; "
                    "NAT/SNAT should not be drawn as load balancer ingress"
                )
            crosses_network = _semantic_edge_crosses_network_domains(
                from_id,
                to_id,
                visible_node_types,
                containment_parent,
                container_parent,
                group_members,
                group_parents,
            )
            if (
                crosses_network
                and not _semantic_endpoint_is_transit_router(from_id, visible_node_types)
                and not (_semantic_endpoint_is_transit_router(to_id, visible_node_types))
            ):
                if not _semantic_label_mentions_cross_network(label):
                    issues.append(
                        f"view {view_id} edge {from_id}->{to_id} crosses VPCs and should mention CEN or cross-VPC"
                    )
                if view_id == "overview" and has_transit_router and not _semantic_label_mentions_transit_path(label):
                    issues.append(f"overview edge {from_id}->{to_id} crosses VPCs; route it through CEN/TransitRouter")
            if "\n" in label or "\\n" in label:
                issues.append(f"view {view_id} edge {from_id}->{to_id} label must be single-line: {label}")
        if len(selected_ids) > 2:
            isolated_ids = _semantic_view_isolated_ids(
                selected_ids,
                view_edges,
                visible_node_types,
                containment_parent,
                container_parent,
                group_parents,
            )
            if isolated_ids:
                issues.append(
                    f"view {view_id} has isolated nodes {', '.join(isolated_ids)}; connect them or move them to detail"
                )

    if raw_views and needs_drilldown and "overview" not in seen_view_ids:
        issues.append("complex architecture views must include overview")
    needs_any_detail_view = needs_non_network_drilldown or (
        needs_network_drilldown and not small_network_covered_by_overview
    )
    if raw_views and needs_any_detail_view and detail_view_count == 0:
        issues.append("complex architecture views must include at least one detail_<area> drill-down view")
    if raw_views and needs_network_drilldown and not small_network_covered_by_overview:
        if "detail_network" not in seen_view_ids:
            issues.append("network-heavy architecture should include a detail_network drill-down view")
    return list(dict.fromkeys(issues))


def _semantic_ram_governance_issues(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
    raw_edges: list[dict[str, Any]],
) -> list[str]:
    if not _needs_ram_governance_drilldown_view(architecture_context):
        return []
    issues: list[str] = []
    view_ids = {
        str(view.get("id") or f"view_{index}").strip()
        for index, view in enumerate(raw_views, start=1)
        if isinstance(view, dict)
    }
    if "detail_permissions" not in view_ids:
        issues.append(
            "RAM-heavy architecture should include detail_permissions to explain "
            "users, AccessKeys, groups, roles, policies, and resource scopes"
        )
    non_permission_details = view_ids & {"detail_app", "detail_data"}
    if non_permission_details and "detail_permissions" not in view_ids:
        issues.append(
            "RAM-heavy architecture is dominated by application/data-flow views; "
            "make identity and permission governance the primary story"
        )
    if _ram_governance_edges_are_weaker_than_runtime_flow(architecture_context, raw_edges):
        issues.append(
            "RAM-heavy architecture should prioritize governance edges such as "
            "AccessKey->User, User->Group, Group/Role->permission scope over runtime data-flow edges"
        )
    detail_permissions = _semantic_view_by_id(raw_views, "detail_permissions")
    if detail_permissions is not None:
        selected_ids = _semantic_view_selected_ids(detail_permissions)
        if len(_ram_selected_ids(architecture_context, selected_ids)) < 2:
            issues.append("view detail_permissions should include at least two RAM identity/governance nodes")
        if not _permission_scope_selected_ids(architecture_context, selected_ids):
            issues.append("view detail_permissions should include governed resource scope nodes")
    return issues


def _semantic_ack_application_issues(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
    raw_edges: list[dict[str, Any]],
) -> list[str]:
    concept_ids = _ack_application_concept_ids(architecture_context)
    if not concept_ids or not _list_of_dicts(architecture_context.get("kubernetes_applications")):
        return []
    issues: list[str] = []
    node_types = _visible_node_types_by_id(architecture_context)
    cluster_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if str(item.get("type") or "").startswith("ALIYUN::CS::")
    }
    all_edges = list(raw_edges)
    for view in raw_views:
        all_edges.extend(_list_of_dicts(view.get("edges")))
    for edge in all_edges:
        from_id = str(edge.get("from") or "")
        to_id = str(edge.get("to") or "")
        label = str(edge.get("label") or "")
        kind = str(edge.get("kind") or "")
        if (
            from_id in cluster_ids
            and kind in {"traffic", "dependency", "inferred"}
            and _semantic_type_is_data_backend(node_types.get(to_id, ""))
            and _semantic_label_mentions_runtime_data_flow(label)
        ):
            issues.append(
                "ACK/Kubernetes runtime data edges should start from application workload, "
                f"not cluster {from_id}; use a CONCEPT::ACK::ApplicationWorkload node for {to_id}"
            )
            break
    if len(concept_ids) < 2 or not raw_views:
        return issues
    selected_ids: set[str] = set()
    for view in raw_views:
        selected_ids.update(_semantic_view_selected_ids(view))
        for edge in _list_of_dicts(view.get("edges")):
            from_id = edge.get("from")
            to_id = edge.get("to")
            if isinstance(from_id, str):
                selected_ids.add(from_id)
            if isinstance(to_id, str):
                selected_ids.add(to_id)
    if len(concept_ids & selected_ids) >= 2:
        return issues
    issues.append(
        "ACK/Kubernetes application views should include CONCEPT::ACK nodes to show "
        "Ingress/Service/HPA/workload semantics"
    )
    return issues


def _semantic_shared_orchestration_dependency_issues(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
    raw_edges: list[dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    for action in _list_of_dicts(architecture_context.get("orchestration_actions")):
        action_id = str(action.get("id") or "orchestration action")
        target_ids = [
            str(target.get("id"))
            for target in _list_of_dicts(action.get("targets"))
            if target.get("visible") is True and isinstance(target.get("id"), str)
        ]
        target_ids = list(dict.fromkeys(target_ids))
        if len(target_ids) < 2:
            continue
        target_set = set(target_ids)
        referenced_ids = [
            str(reference.get("id"))
            for reference in _list_of_dicts(action.get("referenced_resources"))
            if reference.get("visible") is True and isinstance(reference.get("id"), str)
        ]
        referenced_ids = list(dict.fromkeys(referenced_ids))
        for referenced_id in referenced_ids:
            if raw_views:
                for index, view in enumerate(raw_views, start=1):
                    view_id = str(view.get("id") or f"view_{index}").strip()
                    view_group_members = {
                        group_id: set(members) for group_id, members in _semantic_view_group_members(view).items()
                    }
                    selected_ids = _semantic_view_selected_ids(view)
                    selected_targets = target_set & selected_ids
                    for group_id, members in view_group_members.items():
                        if group_id in selected_ids:
                            selected_targets.update(target_set & members)
                    if len(selected_targets) < 2 or referenced_id not in selected_ids:
                        continue
                    covered_targets, fully_grouped = _semantic_shared_dependency_coverage(
                        _list_of_dicts(view.get("edges")),
                        view_group_members,
                        selected_targets,
                        referenced_id,
                    )
                    if fully_grouped or not covered_targets or covered_targets == selected_targets:
                        continue
                    issues.append(
                        f"view {view_id} partially shows {action_id} dependency {referenced_id}; "
                        f"it only connects {', '.join(sorted(covered_targets))}. "
                        "Use a summary group or mirror the shared dependency for every selected target."
                    )
            else:
                covered_targets, fully_grouped = _semantic_shared_dependency_coverage(
                    raw_edges,
                    {},
                    target_set,
                    referenced_id,
                )
                if fully_grouped or not covered_targets or covered_targets == target_set:
                    continue
                issues.append(
                    f"{action_id} shares {referenced_id} dependency across {', '.join(target_ids)}; "
                    f"plan only connects {', '.join(sorted(covered_targets))}. "
                    "Use a summary group or mirror the shared dependency for every target."
                )
    return issues


def _semantic_shared_dependency_coverage(
    edges: list[dict[str, Any]],
    group_members: dict[str, set[str]],
    target_set: set[str],
    referenced_id: str,
) -> tuple[set[str], bool]:
    covered_targets: set[str] = set()
    fully_grouped = False
    for edge in edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if to_id != referenced_id:
            continue
        if from_id in target_set:
            covered_targets.add(from_id)
            continue
        if from_id in group_members and target_set.issubset(group_members[from_id]):
            fully_grouped = True
    return covered_targets, fully_grouped


def _ram_governance_edges_are_weaker_than_runtime_flow(
    architecture_context: dict[str, Any],
    raw_edges: list[dict[str, Any]],
) -> bool:
    if not raw_edges:
        return True
    node_types = _visible_node_types_by_id(architecture_context)
    governance_count = 0
    runtime_count = 0
    for edge in raw_edges:
        from_id = str(edge.get("from") or "")
        to_id = str(edge.get("to") or "")
        label = str(edge.get("label") or "")
        kind = str(edge.get("kind") or "")
        from_is_ram = _semantic_type_is_ram_governance(node_types.get(from_id, ""))
        to_is_ram = _semantic_type_is_ram_governance(node_types.get(to_id, ""))
        if (
            (from_is_ram and to_is_ram)
            or _semantic_label_mentions_permission_governance(label)
            or (from_is_ram or to_is_ram)
            and kind == "management"
        ):
            governance_count += 1
        if (
            kind in {"traffic", "inferred"}
            and not from_is_ram
            and not to_is_ram
            or _semantic_label_mentions_runtime_data_flow(label)
        ):
            runtime_count += 1
    return runtime_count >= 2 and governance_count < 2


def _ram_selected_ids(architecture_context: dict[str, Any], selected_ids: set[str]) -> set[str]:
    node_types = _visible_node_types_by_id(architecture_context)
    return {node_id for node_id in selected_ids if _semantic_type_is_ram_governance(node_types.get(node_id, ""))}


def _permission_scope_selected_ids(architecture_context: dict[str, Any], selected_ids: set[str]) -> set[str]:
    node_types = _visible_node_types_by_id(architecture_context)
    return {node_id for node_id in selected_ids if _semantic_type_is_permission_scope(node_types.get(node_id, ""))}


def _semantic_label_mentions_permission_governance(label: str) -> bool:
    normalized = re.sub(r"\s+", "", label.casefold())
    return any(
        marker in normalized
        for marker in (
            "accesskey",
            "policy",
            "permission",
            "role",
            "user",
            "凭证",
            "加入",
            "角色",
            "权限",
            "授权",
            "授予",
            "用户",
            "用户组",
            "策略",
            "资源范围",
        )
    )


def _semantic_label_mentions_runtime_data_flow(label: str) -> bool:
    normalized = re.sub(r"\s+", "", label.casefold())
    return any(
        marker in normalized
        for marker in (
            "database",
            "data",
            "objectstorage",
            "storage",
            "traffic",
            "上传",
            "下载",
            "存储",
            "数据",
            "数据库",
            "流量",
            "访问",
        )
    )


def _semantic_type_is_data_backend(resource_type: str) -> bool:
    return (
        any(
            marker in resource_type
            for marker in (
                "::RDS::",
                "::REDIS::",
                "::GPDB::",
                "::POLARDB::",
                "::MongoDB::",
                "::DRDS::",
                "::ADB::",
                "::OSS::",
                "::NAS::",
                "::Kafka::",
                "::RocketMQ::",
                "::MNS::",
                "::SLS::",
                "::DBInstance",
                "::FileSystem",
                "::Bucket",
            )
        )
        and "ALIYUN::CS::" not in resource_type
    )


def _semantic_view_selected_ids(view: dict[str, Any]) -> set[str]:
    selected_ids: set[str] = set()
    for key in ("nodes", "node_ids", "containers", "container_ids"):
        value = view.get(key)
        if isinstance(value, list):
            selected_ids.update(item.strip() for item in value if isinstance(item, str) and item.strip())
    selected_ids.update(_semantic_view_group_ids(view))
    return selected_ids


def _semantic_view_anchor_ids(view: dict[str, Any]) -> set[str]:
    anchor_ids: set[str] = set()
    for key in ("anchors", "anchor_ids"):
        value = view.get(key)
        if isinstance(value, list):
            anchor_ids.update(item.strip() for item in value if isinstance(item, str) and item.strip())
    return anchor_ids


def _semantic_view_group_ids(view: dict[str, Any]) -> set[str]:
    return set(_semantic_view_group_members(view))


def _semantic_view_group_members(view: dict[str, Any]) -> dict[str, list[str]]:
    group_members: dict[str, list[str]] = {}
    for raw_group in _list_of_dicts(view.get("groups")):
        group_id = raw_group.get("id")
        if not isinstance(group_id, str) or not group_id.strip():
            group_id = raw_group.get("group_id")
        if not isinstance(group_id, str) or not group_id.strip():
            continue
        group_id = group_id.strip()
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", group_id):
            continue
        members = raw_group.get("members")
        if not isinstance(members, list):
            members = raw_group.get("member_ids")
        if not isinstance(members, list):
            group_members[group_id] = []
            continue
        valid_members = [member_id.strip() for member_id in members if isinstance(member_id, str) and member_id.strip()]
        group_members[group_id] = list(dict.fromkeys(valid_members))
    return group_members


def _semantic_view_group_parents(view: dict[str, Any]) -> dict[str, str]:
    group_parents: dict[str, str] = {}
    for raw_group in _list_of_dicts(view.get("groups")):
        group_id = raw_group.get("id")
        if not isinstance(group_id, str) or not group_id.strip():
            group_id = raw_group.get("group_id")
        if not isinstance(group_id, str) or not group_id.strip():
            continue
        parent_id = raw_group.get("parent") or raw_group.get("container")
        if isinstance(parent_id, str) and parent_id.strip():
            group_parents[group_id.strip()] = parent_id.strip()
    return group_parents


def _semantic_overview_group_ids(raw_views: list[dict[str, Any]]) -> set[str]:
    for index, view in enumerate(raw_views, start=1):
        view_id = view.get("id")
        if not isinstance(view_id, str) or not view_id.strip():
            view_id = f"view_{index}"
        if view_id.strip() == "overview":
            return _semantic_view_group_ids(view)
    return set()


def _semantic_overview_group_parents(raw_views: list[dict[str, Any]]) -> dict[str, str]:
    for index, view in enumerate(raw_views, start=1):
        view_id = view.get("id")
        if not isinstance(view_id, str) or not view_id.strip():
            view_id = f"view_{index}"
        if view_id.strip() == "overview":
            return _semantic_view_group_parents(view)
    return {}


def _semantic_resource_container_map(architecture_context: dict[str, Any]) -> dict[str, str]:
    parents: dict[str, str] = {}
    for item in _list_of_dicts(architecture_context.get("containment")):
        resource_id = item.get("resource")
        container_id = item.get("container")
        if (
            isinstance(resource_id, str)
            and resource_id.strip()
            and isinstance(container_id, str)
            and container_id.strip()
        ):
            parents[resource_id.strip()] = container_id.strip()
    return parents


def _semantic_container_parent_map(architecture_context: dict[str, Any]) -> dict[str, str]:
    parents: dict[str, str] = {}
    for item in _list_of_dicts(architecture_context.get("containers")):
        container_id = item.get("id")
        parent_id = item.get("parent")
        if isinstance(container_id, str) and container_id.strip() and isinstance(parent_id, str) and parent_id.strip():
            parents[container_id.strip()] = parent_id.strip()
    return parents


def _semantic_missing_detail_network_attached_child_resources(
    architecture_context: dict[str, Any],
    selected_ids: set[str],
    valid_ids: set[str],
) -> list[str]:
    missing: list[str] = []
    for attachment in _list_of_dicts(architecture_context.get("network_attachments")):
        if attachment.get("type") != "ALIYUN::CEN::CenInstanceAttachment":
            continue
        child_resource = attachment.get("child_resource")
        if not isinstance(child_resource, str) or not child_resource.strip():
            continue
        child_resource = child_resource.strip()
        if child_resource in valid_ids and child_resource not in selected_ids:
            missing.append(child_resource)
    return sorted(set(missing))


def _semantic_detail_network_missing_multivswitch_domains(
    architecture_context: dict[str, Any],
    selected_ids: set[str],
    group_parents: dict[str, str],
) -> bool:
    visible_node_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    container_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("containers"))
        if isinstance(item.get("id"), str)
    }
    vswitch_ids = {
        container_id
        for container_id, resource_type in container_types.items()
        if resource_type == "ALIYUN::ECS::VSwitch"
    }
    if len(vswitch_ids) < 3:
        return False

    def is_network_domain(node_id: str) -> bool:
        resource_type = container_types.get(node_id)
        if resource_type in {"ALIYUN::ECS::VPC", "ALIYUN::ECS::VSwitch"}:
            return True
        if _semantic_endpoint_is_network_route_config_detail(node_id, visible_node_types):
            return True
        parent_id = group_parents.get(node_id)
        if parent_id is None:
            return False
        return container_types.get(parent_id) in {"ALIYUN::ECS::VPC", "ALIYUN::ECS::VSwitch"}

    return not any(is_network_domain(selected_id) for selected_id in selected_ids)


def _semantic_detail_app_ingress_source_issues(
    view_id: str,
    view: dict[str, Any],
    overview_view: dict[str, Any] | None,
    overview_group_members: dict[str, list[str]],
    visible_node_types: dict[str, str],
) -> list[str]:
    if view_id != "detail_app" or overview_view is None:
        return []
    selected_ids = _semantic_view_selected_ids(view)
    anchor_ids = _semantic_view_anchor_ids(view)
    detail_group_members = _semantic_view_group_members(view)
    issues: list[str] = []
    for overview_edge in _list_of_dicts(overview_view.get("edges")):
        edge_kind = str(overview_edge.get("kind") or "")
        edge_label = str(overview_edge.get("label") or "")
        if edge_kind != "traffic" and not _semantic_label_mentions_business_traffic(edge_label):
            continue
        from_id = _semantic_edge_endpoint(overview_edge, "from")
        to_id = _semantic_edge_endpoint(overview_edge, "to")
        if from_id is None or to_id is None:
            continue
        if not _semantic_endpoint_is_or_contains_load_balancer(
            from_id,
            visible_node_types,
            overview_group_members,
        ):
            continue
        if not _semantic_endpoint_is_present_or_expanded_in_detail_app(
            to_id,
            selected_ids,
            anchor_ids,
            detail_group_members,
            overview_group_members,
        ):
            continue
        if _semantic_endpoint_is_present_or_expanded_in_detail_app(
            from_id,
            selected_ids,
            anchor_ids,
            detail_group_members,
            overview_group_members,
        ):
            continue
        issues.append(f"view detail_app should include overview traffic source {from_id} when expanding {to_id}")
    return issues


def _semantic_endpoint_is_present_or_expanded_in_detail_app(
    endpoint_id: str,
    selected_ids: set[str],
    anchor_ids: set[str],
    detail_group_members: dict[str, list[str]],
    overview_group_members: dict[str, list[str]],
) -> bool:
    if endpoint_id in selected_ids or endpoint_id in anchor_ids:
        return True
    for member_id in overview_group_members.get(endpoint_id, []):
        if member_id in selected_ids or member_id in anchor_ids:
            return True
    for member_id in detail_group_members.get(endpoint_id, []):
        if member_id in selected_ids or member_id in anchor_ids:
            return True
    return False


def _semantic_endpoint_is_or_contains_load_balancer(
    endpoint_id: str,
    visible_node_types: dict[str, str],
    group_members: dict[str, list[str]],
) -> bool:
    if _semantic_type_is_load_balancer(visible_node_types.get(endpoint_id, "")):
        return True
    return any(
        _semantic_endpoint_is_or_contains_load_balancer(member_id, visible_node_types, group_members)
        for member_id in group_members.get(endpoint_id, [])
    )


def _semantic_generic_network_route_domain_label_issues(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
    accepted_node_labels_by_id: dict[str, str],
) -> list[str]:
    detail_network = _semantic_view_by_id(raw_views, "detail_network")
    if detail_network is None:
        return []
    selected_ids = _semantic_view_selected_ids(detail_network)
    visible_node_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    visible_labels = {
        str(item.get("id")): str(item.get("label") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    generic_ids: list[str] = []
    for selected_id in sorted(selected_ids):
        if not _semantic_endpoint_is_network_route_config_detail(selected_id, visible_node_types):
            continue
        label = accepted_node_labels_by_id.get(selected_id) or visible_labels.get(selected_id, "")
        if _semantic_route_config_label_is_generic(label):
            generic_ids.append(selected_id)
    if not generic_ids:
        return []
    return [
        "view detail_network route/config domain labels should describe purpose, not generic VPC config: "
        + ", ".join(generic_ids)
    ]


def _semantic_route_config_label_is_generic(label: str) -> bool:
    normalized = re.sub(r"\s+", "", label.casefold())
    if not normalized:
        return False
    generic_labels = {
        "vpc配置",
        "vpc路由配置",
        "vpcrouteconfig",
        "vpcrouting",
        "routeconfig",
        "routingconfig",
        "专有网络vpc配置",
        "专有网络配置",
        "专有网络路由配置",
        "路由配置",
        "网络配置",
    }
    if normalized in generic_labels:
        return True
    purpose_tokens = (
        "dmz",
        "prod",
        "production",
        "生产",
        "测试",
        "开发",
        "出口",
        "入口",
        "nat",
        "snat",
        "cen",
        "互联",
        "跨",
        "外部",
        "对端",
        "公网",
        "私网",
        "安全",
        "前端",
        "后端",
        "接入",
        "管理",
        "网关",
        "frontend",
        "backend",
        "front",
        "back",
        "access",
        "management",
        "gateway",
        "入向",
        "出向",
        "转发",
    )
    generic_tokens = ("vpc", "专有网络", "路由", "route", "配置", "config")
    return any(token in normalized for token in generic_tokens) and not any(
        token in normalized for token in purpose_tokens
    )


def _semantic_external_cen_network_issues(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
    accepted_node_labels_by_id: dict[str, str],
) -> list[str]:
    external_attachments = _semantic_external_cen_network_attachments(architecture_context)
    if not external_attachments:
        return []
    detail_network = _semantic_view_by_id(raw_views, "detail_network")
    if detail_network is None:
        return []
    selected_ids = _semantic_view_selected_ids(detail_network)
    cen_config_ids = _semantic_selected_cen_config_ids(architecture_context, selected_ids)
    if not cen_config_ids:
        return []
    text_parts: list[str] = []
    visible_labels = {
        str(item.get("id")): str(item.get("label") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    for node_id in sorted(cen_config_ids):
        text_parts.append(accepted_node_labels_by_id.get(node_id, ""))
        text_parts.append(visible_labels.get(node_id, ""))
    for edge in _list_of_dicts(detail_network.get("edges")):
        text_parts.append(str(edge.get("label") or ""))
    if any(_semantic_text_mentions_external_cen_network(text) for text in text_parts):
        return []
    type_counts: dict[str, int] = {}
    for attachment in external_attachments:
        child_type = str(attachment.get("child_instance_type") or "network").upper()
        type_counts[child_type] = type_counts.get(child_type, 0) + 1
    type_summary = "/".join(
        f"{child_type} x{count}" if count > 1 else child_type for child_type, count in sorted(type_counts.items())
    )
    return [
        "view detail_network should explain external CEN networks "
        f"({type_summary}) instead of only drawing the local VPC to CEN"
    ]


def _semantic_route_next_hop_forwarder_issues(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
) -> list[str]:
    if not _list_of_dicts(architecture_context.get("network_attachments")):
        return []
    forwarder_ids = _semantic_route_next_hop_compute_ids(architecture_context)
    if not forwarder_ids:
        return []
    detail_network = _semantic_view_by_id(raw_views, "detail_network")
    if detail_network is None:
        return []
    selected_ids = _semantic_view_selected_ids(detail_network)
    selected_forwarders = sorted(forwarder_ids & selected_ids)
    if not selected_forwarders:
        return []
    visible_node_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    transit_ids = {
        selected_id
        for selected_id in selected_ids
        if _semantic_endpoint_is_transit_router(selected_id, visible_node_types)
    }
    if not transit_ids:
        return []
    view_edges = _list_of_dicts(detail_network.get("edges"))
    connected_forwarders = {
        forwarder_id
        for forwarder_id in selected_forwarders
        if _semantic_view_has_path(forwarder_id, transit_ids, view_edges)
    }
    missing = sorted(set(selected_forwarders) - connected_forwarders)
    if not missing:
        return []
    return [
        "view detail_network should connect route next-hop compute "
        f"{', '.join(missing)} into the CEN/TransitRouter routing path"
    ]


def _semantic_route_next_hop_compute_ids(architecture_context: dict[str, Any]) -> set[str]:
    compute_ids: set[str] = set()
    for relation in _list_of_dicts(architecture_context.get("explicit_relations")):
        if relation.get("source_type") != "ALIYUN::ECS::Route" or relation.get("property") != "NextHopId":
            continue
        target_id = relation.get("target")
        target_type = str(relation.get("target_type") or "")
        if isinstance(target_id, str) and _semantic_type_is_route_next_hop_compute(target_type):
            compute_ids.add(target_id)
    return compute_ids


def _semantic_type_is_route_next_hop_compute(resource_type: str) -> bool:
    normalized = resource_type.casefold()
    return "::ecs::instance" in normalized or "::ecs::instancegroup" in normalized


def _semantic_view_has_path(
    source_id: str,
    target_ids: set[str],
    view_edges: list[dict[str, Any]],
) -> bool:
    adjacency: dict[str, set[str]] = {}
    for edge in view_edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id is None or to_id is None:
            continue
        adjacency.setdefault(from_id, set()).add(to_id)
        adjacency.setdefault(to_id, set()).add(from_id)
    seen: set[str] = set()
    queue = [source_id]
    while queue:
        current = queue.pop(0)
        if current in target_ids:
            return True
        if current in seen:
            continue
        seen.add(current)
        queue.extend(sorted(adjacency.get(current, set()) - seen))
    return False


def _semantic_external_cen_network_attachments(architecture_context: dict[str, Any]) -> list[dict[str, Any]]:
    external: list[dict[str, Any]] = []
    for attachment in _list_of_dicts(architecture_context.get("network_attachments")):
        if attachment.get("type") != "ALIYUN::CEN::CenInstanceAttachment":
            continue
        child_type = str(attachment.get("child_instance_type") or "").upper()
        if child_type not in {"VPC", "VBR"}:
            continue
        child_resource = attachment.get("child_resource")
        if isinstance(child_resource, str) and child_resource.strip():
            continue
        child_id = str(attachment.get("child_instance_id") or "")
        if child_id.startswith("Ref:") or child_id:
            external.append(attachment)
    return external


def _semantic_view_by_id(raw_views: list[dict[str, Any]], view_id: str) -> dict[str, Any] | None:
    for index, view in enumerate(raw_views, start=1):
        if _semantic_view_id_for_validation(view, index) == view_id:
            return view
    return None


def _semantic_view_id_for_validation(view: dict[str, Any], index: int) -> str:
    value = view.get("id")
    if not isinstance(value, str) or not value.strip():
        return f"view_{index}"
    return value.strip()


def _semantic_selected_cen_config_ids(
    architecture_context: dict[str, Any],
    selected_ids: set[str],
) -> set[str]:
    result: set[str] = set()
    for item in _list_of_dicts(architecture_context.get("visible_nodes")):
        node_id = item.get("id")
        if not isinstance(node_id, str) or node_id not in selected_ids:
            continue
        resource_type = str(item.get("type") or "")
        label = str(item.get("label") or "")
        if resource_type.startswith("CONCEPT::") and ("CEN" in label.upper() or "CEN" in node_id.upper()):
            result.add(node_id)
        elif "::CEN::" in resource_type:
            result.add(node_id)
    return result


def _semantic_text_mentions_external_cen_network(text: str) -> bool:
    normalized = text.casefold()
    return any(
        token in normalized
        for token in (
            "外部",
            "已有",
            "现有",
            "对端",
            "外部网络",
            "外部 vpc",
            "外部vpc",
            "外部 vbr",
            "外部vbr",
            "vpc/vbr",
            "vbr",
            "other vpc",
            "other vbr",
            "external",
            "peer",
        )
    )


def _semantic_contained_network_detail_application_containers(
    architecture_context: dict[str, Any],
    selected_ids: set[str],
    visible_node_types: dict[str, str],
    containment_parent: dict[str, str],
    container_parent: dict[str, str],
) -> list[str]:
    if not _semantic_selected_ids_have_network_interconnect_detail(
        architecture_context, selected_ids, visible_node_types
    ):
        return []
    selected_container_ids = {
        selected_id for selected_id in selected_ids if selected_id not in visible_node_types and selected_id
    }
    expanding_containers: list[str] = []
    for container_id in sorted(selected_container_ids):
        for node_id, parent_id in containment_parent.items():
            if not _semantic_container_is_descendant_or_same(parent_id, container_id, container_parent):
                continue
            if _semantic_type_is_application_resource(visible_node_types.get(node_id, "")):
                expanding_containers.append(container_id)
                break
    return expanding_containers


def _semantic_selected_ids_have_network_interconnect_detail(
    architecture_context: dict[str, Any],
    selected_ids: set[str],
    visible_node_types: dict[str, str],
) -> bool:
    if _list_of_dicts(architecture_context.get("network_attachments")):
        return True
    return any(
        _semantic_endpoint_is_transit_router(selected_id, visible_node_types)
        or _semantic_endpoint_is_network_route_config_detail(selected_id, visible_node_types)
        or _semantic_type_is_nat_gateway(visible_node_types.get(selected_id, ""))
        for selected_id in selected_ids
    )


def _semantic_container_is_descendant_or_same(
    container_id: str,
    ancestor_id: str,
    container_parent: dict[str, str],
) -> bool:
    current = container_id
    seen: set[str] = set()
    while current and current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        current = container_parent.get(current, "")
    return False


def _semantic_type_is_application_resource(resource_type: str) -> bool:
    normalized = resource_type.casefold()
    if not normalized or normalized.startswith("concept::"):
        return False
    return any(
        marker in normalized
        for marker in (
            "::ecs::instance",
            "::ecs::instancegroup",
            "::ess::scalinggroup",
            "::alb::",
            "::nlb::",
            "::slb::",
            "loadbalancer",
            "::rds::",
            "::polardb::",
            "::redis::",
            "::nas::filesystem",
            "::oss::bucket",
            "::ots::",
            "::mongodb::",
        )
    )


def _semantic_edge_crosses_network_domains(
    from_id: str,
    to_id: str,
    visible_node_types: dict[str, str],
    containment_parent: dict[str, str],
    container_parent: dict[str, str],
    group_members: dict[str, list[str]],
    group_parents: dict[str, str],
) -> bool:
    from_roots = _semantic_endpoint_root_containers(
        from_id, visible_node_types, containment_parent, container_parent, group_members, group_parents
    )
    to_roots = _semantic_endpoint_root_containers(
        to_id, visible_node_types, containment_parent, container_parent, group_members, group_parents
    )
    return bool(from_roots and to_roots and from_roots.isdisjoint(to_roots))


def _semantic_endpoint_root_containers(
    endpoint_id: str,
    visible_node_types: dict[str, str],
    containment_parent: dict[str, str],
    container_parent: dict[str, str],
    group_members: dict[str, list[str]],
    group_parents: dict[str, str],
) -> set[str]:
    if endpoint_id in group_parents:
        return {_semantic_root_container(group_parents[endpoint_id], container_parent)}
    if endpoint_id in group_members:
        roots: set[str] = set()
        for member_id in group_members[endpoint_id]:
            roots.update(
                _semantic_endpoint_root_containers(
                    member_id,
                    visible_node_types,
                    containment_parent,
                    container_parent,
                    group_members,
                    group_parents,
                )
            )
        return roots
    if endpoint_id in containment_parent:
        return {_semantic_root_container(containment_parent[endpoint_id], container_parent)}
    if endpoint_id not in visible_node_types and endpoint_id:
        return {_semantic_root_container(endpoint_id, container_parent)}
    return set()


def _semantic_root_container(container_id: str, container_parent: dict[str, str]) -> str:
    current = container_id
    seen: set[str] = set()
    while current in container_parent and current not in seen:
        seen.add(current)
        current = container_parent[current]
    return current


def _semantic_view_has_transit_router(selected_ids: set[str], visible_node_types: dict[str, str]) -> bool:
    return any(_semantic_endpoint_is_transit_router(selected_id, visible_node_types) for selected_id in selected_ids)


def _semantic_endpoint_is_transit_router(endpoint_id: str, visible_node_types: dict[str, str]) -> bool:
    resource_type = visible_node_types.get(endpoint_id, "")
    return "::CEN::" in resource_type or "TransitRouter" in resource_type


def _semantic_endpoint_is_network_route_config_detail(endpoint_id: str, visible_node_types: dict[str, str]) -> bool:
    resource_type = visible_node_types.get(endpoint_id, "")
    normalized = resource_type.casefold()
    return (
        resource_type == "CONCEPT::Layer::AttachmentSummary"
        or normalized.endswith("::route")
        or "routetable" in normalized
        or "routeentry" in normalized
        or "routerinterface" in normalized
    )


def _semantic_edge_connects_transit_router_and_load_balancer(
    from_id: str,
    to_id: str,
    visible_node_types: dict[str, str],
    group_members: dict[str, list[str]],
    group_parents: dict[str, str],
) -> bool:
    return (
        _semantic_endpoint_is_transit_router(from_id, visible_node_types)
        and _semantic_endpoint_is_load_balancer(to_id, visible_node_types, group_members, group_parents)
    ) or (
        _semantic_endpoint_is_transit_router(to_id, visible_node_types)
        and _semantic_endpoint_is_load_balancer(from_id, visible_node_types, group_members, group_parents)
    )


def _semantic_edge_connects_nat_and_load_balancer(
    from_id: str,
    to_id: str,
    visible_node_types: dict[str, str],
    group_members: dict[str, list[str]],
    group_parents: dict[str, str],
) -> bool:
    return (
        _semantic_endpoint_is_nat_gateway(from_id, visible_node_types, group_members, group_parents)
        and _semantic_endpoint_is_load_balancer(to_id, visible_node_types, group_members, group_parents)
    ) or (
        _semantic_endpoint_is_nat_gateway(to_id, visible_node_types, group_members, group_parents)
        and _semantic_endpoint_is_load_balancer(from_id, visible_node_types, group_members, group_parents)
    )


def _semantic_endpoint_is_load_balancer(
    endpoint_id: str,
    visible_node_types: dict[str, str],
    group_members: dict[str, list[str]],
    group_parents: dict[str, str],
) -> bool:
    if endpoint_id in group_parents:
        return False
    resource_type = visible_node_types.get(endpoint_id, "")
    if _semantic_type_is_load_balancer(resource_type):
        return True
    member_ids = group_members.get(endpoint_id)
    if not member_ids:
        return False
    return any(_semantic_type_is_load_balancer(visible_node_types.get(member_id, "")) for member_id in member_ids)


def _semantic_type_is_load_balancer(resource_type: str) -> bool:
    return "LoadBalancer" in resource_type or any(
        marker in resource_type for marker in ("::ALB::", "::NLB::", "::SLB::")
    )


def _semantic_endpoint_is_nat_gateway(
    endpoint_id: str,
    visible_node_types: dict[str, str],
    group_members: dict[str, list[str]],
    group_parents: dict[str, str],
) -> bool:
    if endpoint_id in group_parents:
        return False
    resource_type = visible_node_types.get(endpoint_id, "")
    if _semantic_type_is_nat_gateway(resource_type):
        return True
    member_ids = group_members.get(endpoint_id)
    if not member_ids:
        return False
    return any(_semantic_type_is_nat_gateway(visible_node_types.get(member_id, "")) for member_id in member_ids)


def _semantic_type_is_nat_gateway(resource_type: str) -> bool:
    normalized = resource_type.casefold()
    return "natgateway" in normalized or "::nat::" in normalized


def _semantic_label_mentions_cross_network(label: str) -> bool:
    normalized = label.lower()
    return any(
        token in normalized
        for token in ("cen", "transit", "cross", "external", "vbr", "跨", "经", "互联", "外部", "转发路由")
    )


def _semantic_label_mentions_transit_path(label: str) -> bool:
    normalized = label.lower()
    return any(token in normalized for token in ("cen", "transit", "经", "转发路由"))


def _semantic_label_mentions_business_traffic(label: str) -> bool:
    normalized = label.lower()
    return any(token in normalized for token in ("traffic", "ingress", "backend", "入口", "流量", "后端", "转发"))


def _semantic_label_mentions_vpc_connection(label: str) -> bool:
    normalized = label.lower()
    return any(token in normalized for token in ("vpc", "连接", "接入", "互联", "路由"))


def _semantic_repeated_real_node_types(
    selected_ids: set[str], visible_node_types: dict[str, str]
) -> dict[str, list[str]]:
    ids_by_type: dict[str, list[str]] = {}
    for selected_id in sorted(selected_ids):
        resource_type = visible_node_types.get(selected_id)
        if not resource_type or resource_type.startswith("CONCEPT::"):
            continue
        ids_by_type.setdefault(resource_type, []).append(selected_id)
    return {resource_type: node_ids for resource_type, node_ids in ids_by_type.items() if len(node_ids) > 1}


def _semantic_overview_selected_ids(raw_views: list[dict[str, Any]]) -> set[str]:
    for index, view in enumerate(raw_views, start=1):
        view_id = view.get("id")
        if not isinstance(view_id, str) or not view_id.strip():
            view_id = f"view_{index}"
        if view_id.strip() == "overview":
            return _semantic_view_selected_ids(view)
    return set()


def _semantic_edge_endpoint(raw_edge: dict[str, Any], key: str) -> str | None:
    value = raw_edge.get(key)
    if not isinstance(value, str) or not value.strip():
        value = raw_edge.get(f"{key}_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _semantic_view_isolated_ids(
    selected_ids: set[str],
    view_edges: list[dict[str, Any]],
    visible_node_types: dict[str, str],
    containment_parent: dict[str, str],
    container_parent: dict[str, str],
    group_parents: dict[str, str],
) -> list[str]:
    connected_ids: set[str] = set()
    for edge in view_edges:
        from_id = _semantic_edge_endpoint(edge, "from")
        to_id = _semantic_edge_endpoint(edge, "to")
        if from_id in selected_ids and to_id in selected_ids:
            connected_ids.update((from_id, to_id))
    return sorted(
        selected_id
        for selected_id in selected_ids - connected_ids
        if not _semantic_selected_container_is_connected_boundary(
            selected_id,
            connected_ids,
            visible_node_types,
            containment_parent,
            container_parent,
            group_parents,
        )
    )


def _semantic_selected_container_is_connected_boundary(
    selected_id: str,
    connected_ids: set[str],
    visible_node_types: dict[str, str],
    containment_parent: dict[str, str],
    container_parent: dict[str, str],
    group_parents: dict[str, str],
) -> bool:
    if selected_id in visible_node_types:
        return False
    for group_id, parent_id in group_parents.items():
        if group_id in connected_ids and _semantic_container_is_descendant_or_same(
            parent_id, selected_id, container_parent
        ):
            return True
    for node_id, parent_id in containment_parent.items():
        if node_id in connected_ids and _semantic_container_is_descendant_or_same(
            parent_id, selected_id, container_parent
        ):
            return True
    return False


def _needs_overview_with_drilldown_views(architecture_context: dict[str, Any], raw_edges: list[dict[str, Any]]) -> bool:
    return _needs_non_network_overview_with_drilldown_views(
        architecture_context,
        raw_edges,
    ) or _needs_network_drilldown_view(architecture_context)


def _needs_non_network_overview_with_drilldown_views(
    architecture_context: dict[str, Any], raw_edges: list[dict[str, Any]]
) -> bool:
    visible_nodes = _list_of_dicts(architecture_context.get("visible_nodes"))
    visible_containers = _list_of_dicts(architecture_context.get("containers"))
    visible_edges = _list_of_dicts(architecture_context.get("visible_edges"))
    semantic_edge_count = len(raw_edges)
    visible_element_count = len(visible_nodes) + len(visible_containers)
    return visible_element_count > 14 or semantic_edge_count > 6 or len(visible_edges) > 12


def _needs_placement_preserving_overview(architecture_context: dict[str, Any]) -> bool:
    visible_nodes = _list_of_dicts(architecture_context.get("visible_nodes"))
    if len(visible_nodes) > 10:
        return False
    domains = _placement_vswitch_domains(architecture_context)
    domains_with_core_nodes = [
        domain
        for domain in domains
        if any(_semantic_type_is_placement_core_node(str(member.get("type") or "")) for member in domain["members"])
    ]
    return len(domains_with_core_nodes) >= 2


def _placement_vswitch_domains(architecture_context: dict[str, Any]) -> list[dict[str, Any]]:
    containers = {
        str(item.get("id")): item
        for item in _list_of_dicts(architecture_context.get("containers"))
        if isinstance(item.get("id"), str)
    }
    visible_nodes = {
        str(item.get("id")): item
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    members_by_container: dict[str, list[dict[str, Any]]] = {}
    for item in _list_of_dicts(architecture_context.get("containment")):
        resource_id = item.get("resource")
        container_id = item.get("container")
        if not isinstance(resource_id, str) or not isinstance(container_id, str):
            continue
        member = visible_nodes.get(resource_id)
        if member is not None:
            members_by_container.setdefault(container_id, []).append(_summary_node(member))

    domains: list[dict[str, Any]] = []
    for container_id, container in containers.items():
        resource_type = str(container.get("type") or "")
        if resource_type != "ALIYUN::ECS::VSwitch":
            continue
        members = members_by_container.get(container_id, [])
        if not members:
            continue
        domain: dict[str, Any] = _summary_node(container)
        domain["members"] = members
        parent = container.get("parent")
        if isinstance(parent, str) and parent:
            domain["parent"] = parent
        domains.append(domain)
    return domains


def _semantic_type_is_placement_core_node(resource_type: str) -> bool:
    return any(
        marker in resource_type
        for marker in (
            "::ECS::Instance",
            "::ECS::InstanceGroup",
            "::RDS::DBInstance",
            "::POLARDB::DBCluster",
            "::REDIS::",
            "::SLB::LoadBalancer",
            "::ALB::LoadBalancer",
            "::NLB::LoadBalancer",
        )
    )


def _needs_network_drilldown_view(architecture_context: dict[str, Any]) -> bool:
    network_attachments = _list_of_dicts(architecture_context.get("network_attachments"))
    child_types = {str(item.get("child_instance_type") or "").upper() for item in network_attachments}
    if len(network_attachments) >= 2 or child_types & {"VPC", "VBR"}:
        return True

    resource_like_items = _list_of_dicts(architecture_context.get("visible_nodes")) + _list_of_dicts(
        architecture_context.get("containers")
    )
    network_control_count = 0
    for item in resource_like_items:
        resource_type = str(item.get("type") or "")
        if (
            "::CEN::" in resource_type
            or "TransitRouter" in resource_type
            or "NatGateway" in resource_type
            or "Snat" in resource_type
            or resource_type.endswith("::Route")
            or "RouteTable" in resource_type
        ):
            network_control_count += 1
    return network_control_count >= 3


def _needs_ack_application_drilldown_view(architecture_context: dict[str, Any]) -> bool:
    if not _list_of_dicts(architecture_context.get("kubernetes_applications")):
        return False
    return len(_ack_application_concept_ids(architecture_context)) >= 2


def _ack_application_concept_ids(architecture_context: dict[str, Any]) -> set[str]:
    return {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str) and str(item.get("type") or "").startswith("CONCEPT::ACK::")
    }


def _needs_ram_governance_drilldown_view(architecture_context: dict[str, Any]) -> bool:
    ram_items = _ram_governance_items(architecture_context)
    if len(ram_items) < 6:
        return False
    ram_type_families = {
        _ram_type_family(str(item.get("type") or ""))
        for item in ram_items
        if _ram_type_family(str(item.get("type") or ""))
    }
    if len(ram_type_families) < 2:
        return False
    visible_ram_count = sum(
        1
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if _semantic_type_is_ram_governance(str(item.get("type") or ""))
    )
    return visible_ram_count >= 2


def _ram_governance_items(architecture_context: dict[str, Any]) -> list[dict[str, Any]]:
    resources = _list_of_dicts(architecture_context.get("resources"))
    if resources:
        return [item for item in resources if _semantic_type_is_ram_governance(str(item.get("type") or ""))]
    visible_items = _list_of_dicts(architecture_context.get("visible_nodes")) + _list_of_dicts(
        architecture_context.get("concept_nodes")
    )
    return [item for item in visible_items if _semantic_type_is_ram_governance(str(item.get("type") or ""))]


def _semantic_type_is_ram_governance(resource_type: str) -> bool:
    return resource_type.startswith(RAM_GOVERNANCE_TYPE_PREFIX) and (
        resource_type in RAM_GOVERNANCE_CORE_TYPES
        or resource_type in RAM_GOVERNANCE_PERMISSION_TYPES
        or "Policy" in resource_type
    )


def _ram_type_family(resource_type: str) -> str:
    if not _semantic_type_is_ram_governance(resource_type):
        return ""
    if resource_type in {
        "ALIYUN::RAM::AccessKey",
        "ALIYUN::RAM::User",
        "ALIYUN::RAM::UserToGroupAddition",
    }:
        return "identity"
    if resource_type in {"ALIYUN::RAM::Group", "ALIYUN::RAM::Role"}:
        return "principal_container"
    if resource_type in RAM_GOVERNANCE_PERMISSION_TYPES or "Policy" in resource_type:
        return "permission_policy"
    return "ram"


def _semantic_type_is_permission_scope(resource_type: str) -> bool:
    if not resource_type or _semantic_type_is_ram_governance(resource_type):
        return False
    return any(
        marker in resource_type
        for marker in (
            "::ECS::",
            "::OSS::",
            "::RDS::",
            "::POLARDB::",
            "::REDIS::",
            "::NAS::",
            "::FC::",
            "::ALB::",
            "::SLB::",
            "::NLB::",
            "::VPC::",
        )
    )


def _small_network_detail_is_covered_by_overview(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
) -> bool:
    network_attachments = _list_of_dicts(architecture_context.get("network_attachments"))
    if not network_attachments:
        return False
    overview = _semantic_view_by_id(raw_views, "overview")
    if overview is None:
        return False
    selected_ids = _semantic_view_selected_ids(overview)
    if len(selected_ids) > 8 or len(_list_of_dicts(overview.get("edges"))) > 6:
        return False

    group_members = _semantic_view_group_members(overview)
    covered_ids = set(selected_ids)
    for group_id in selected_ids:
        covered_ids.update(group_members.get(group_id, []))
    marker_to_summary = _attachment_marker_layer_summary_ids(architecture_context)
    for marker_id, summary_id in marker_to_summary.items():
        if summary_id in covered_ids:
            covered_ids.add(marker_id)

    visible_node_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    visible_node_types = {
        str(item.get("id")): str(item.get("type") or "")
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    if len(network_attachments) > 3:
        selected_summary_ids = {
            selected_id
            for selected_id in selected_ids
            if visible_node_types.get(selected_id) == "CONCEPT::Layer::AttachmentSummary"
        }
        return (
            len(visible_node_ids) <= 4
            and visible_node_ids.issubset(covered_ids)
            and (len(_list_of_dicts(overview.get("edges"))) > 0 or bool(selected_summary_ids))
        )

    child_resources = {
        str(attachment.get("child_resource"))
        for attachment in network_attachments
        if attachment.get("type") == "ALIYUN::CEN::CenInstanceAttachment"
        and isinstance(attachment.get("child_resource"), str)
        and str(attachment.get("child_resource")).strip()
    }
    if child_resources and not child_resources.issubset(covered_ids):
        return False

    cen_config_ids = _semantic_selected_cen_config_ids(architecture_context, covered_ids)
    if not cen_config_ids:
        return False

    external_attachments = _semantic_external_cen_network_attachments(architecture_context)
    if external_attachments:
        text_parts: list[str] = []
        for raw_group in _list_of_dicts(overview.get("groups")):
            text_parts.append(str(raw_group.get("label") or ""))
        for edge in _list_of_dicts(overview.get("edges")):
            text_parts.append(str(edge.get("label") or ""))
        if not any(_semantic_text_mentions_external_cen_network(text) for text in text_parts):
            return False

    return True


def _repeated_edge_shape_issues(
    architecture_context: dict[str, Any],
    accepted_edges: list[dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    visible_node_types = _visible_node_types_by_id(architecture_context)
    by_target: dict[tuple[str, str, str], list[str]] = {}
    by_source: dict[tuple[str, str, str], list[str]] = {}
    for edge in accepted_edges:
        from_id = str(edge.get("from") or "")
        to_id = str(edge.get("to") or "")
        kind = str(edge.get("kind") or "")
        label = str(edge.get("label") or "")
        if not from_id or not to_id or not label:
            continue
        by_target.setdefault((to_id, kind, label), []).append(from_id)
        by_source.setdefault((from_id, kind, label), []).append(to_id)
    for (to_id, _kind, label), source_ids in by_target.items():
        if len(set(source_ids)) >= 3:
            if _semantic_repeated_edge_shape_is_expected_network_hub(to_id, label, visible_node_types):
                continue
            issues.append(
                f"too many repeated edges to {to_id} labeled {label}; summarize equivalent fan-in relationships"
            )
    for (from_id, _kind, label), target_ids in by_source.items():
        if len(set(target_ids)) >= 3:
            if _semantic_repeated_edge_shape_is_expected_network_hub(from_id, label, visible_node_types):
                continue
            issues.append(
                f"too many repeated edges from {from_id} labeled {label}; summarize equivalent fan-out relationships"
            )
    return issues


def _semantic_repeated_edge_shape_is_expected_network_hub(
    node_id: str,
    label: str,
    visible_node_types: dict[str, str],
) -> bool:
    return _semantic_endpoint_is_transit_router(
        node_id, visible_node_types
    ) and _semantic_label_mentions_vpc_connection(label)


def _looks_reversed_scaling_source_edge(
    architecture_context: dict[str, Any],
    from_id: str,
    to_id: str,
    label: str,
) -> bool:
    node_types = _visible_node_types_by_id(architecture_context)
    if node_types.get(from_id) != "ALIYUN::ESS::ScalingGroup":
        return False
    if node_types.get(to_id) not in {"ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"}:
        return False
    normalized = label.casefold()
    return any(marker in normalized for marker in ("基准", "模板", "配置", "config", "template", "source", "base"))


def _semantic_scaling_configuration_source_issues(
    architecture_context: dict[str, Any],
    raw_views: list[dict[str, Any]],
    relationship_edges: list[dict[str, Any]],
) -> list[str]:
    valid_node_ids = {
        str(item.get("id"))
        for item in _list_of_dicts(architecture_context.get("visible_nodes"))
        if isinstance(item.get("id"), str)
    }
    selected_ids: set[str] = set()
    for view in raw_views:
        selected_ids.update(_semantic_view_selected_ids(view))
    relationship_endpoint_ids = _edge_endpoint_ids(relationship_edges)
    issues: list[str] = []
    for concept in _list_of_dicts(architecture_context.get("concept_nodes")):
        if concept.get("type") != "CONCEPT::ESS::ScaledECS":
            continue
        scaled_id = concept.get("id")
        source_id = concept.get("source")
        controller_id = concept.get("controller")
        if not (
            isinstance(scaled_id, str)
            and scaled_id.strip()
            and isinstance(source_id, str)
            and source_id.strip()
            and isinstance(controller_id, str)
            and controller_id.strip()
        ):
            continue
        scaled_id = scaled_id.strip()
        source_id = source_id.strip()
        controller_id = controller_id.strip()
        if source_id not in valid_node_ids or controller_id not in valid_node_ids:
            continue
        scaling_is_in_plan = bool({controller_id, scaled_id} & (selected_ids | relationship_endpoint_ids))
        if not scaling_is_in_plan:
            continue
        if _find_existing_edge_index(relationship_edges, source_id, controller_id) is not None:
            continue
        issues.append(
            f"ESS scaling configuration source {source_id}->{controller_id} is missing; "
            "draw seed/template ECS -> ESS scaling group with label 伸缩配置"
        )
    return issues


def _visible_node_types_by_id(architecture_context: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _list_of_dicts(architecture_context.get("visible_nodes")):
        node_id = item.get("id")
        node_type = item.get("type")
        if isinstance(node_id, str) and isinstance(node_type, str):
            result[node_id] = node_type
    return result


def _needs_semantic_relationships(architecture_context: dict[str, Any]) -> bool:
    visible_nodes = _list_of_dicts(architecture_context.get("visible_nodes"))
    explicit_relations = _list_of_dicts(architecture_context.get("explicit_relations"))
    concept_nodes = _list_of_dicts(architecture_context.get("concept_nodes"))
    if len(visible_nodes) + len(concept_nodes) < 3:
        return False
    non_containment_relations = [
        relation
        for relation in explicit_relations
        if relation.get("source_type") != "ALIYUN::ECS::VSwitch"
        and relation.get("source_type") != "ALIYUN::ECS::SecurityGroup"
    ]
    return len(non_containment_relations) >= 2


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _node_hint_values_by_id(architecture_context: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for item in _list_of_dicts(architecture_context.get("node_label_hints")):
        node_id = item.get("id")
        hints = item.get("hints")
        if not isinstance(node_id, str) or not isinstance(hints, dict):
            continue
        values = [value.strip() for value in hints.values() if isinstance(value, str) and value.strip()]
        if values:
            result[node_id] = values
    return {node_id: tuple(values) for node_id, values in result.items()}


def _needs_chinese_role_label(label: str) -> bool:
    return bool(label.strip()) and not _contains_cjk(label)


def _contains_cjk(value: str) -> bool:
    return re.search(r"[\u3400-\u9fff\u3040-\u30ff]", value) is not None


def _copies_raw_identifier_hint(label: str, hint_values: tuple[str, ...]) -> bool:
    normalized_label = _normalize_identifier(label)
    return any(normalized_label == _normalize_identifier(hint) and _looks_identifier_like(hint) for hint in hint_values)


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).casefold()


def _looks_identifier_like(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]*[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*", value))


def render_terminal(console: Console, mermaid_source: str, *, width: int) -> None:
    try:
        preview_console = Console(width=width)
        preview_console.print(_render_terminal_rich(mermaid_source))
    except Exception as exc:
        console.print(f"[yellow]termaid render failed, printing Mermaid source instead: {exc}[/]")
        console.print(f"```mermaid\n{mermaid_source}\n```")


def _terminal_preview_items(
    rendered_mermaid_source: str,
    rendered_views: ArchitectureMultiViewRenderResult,
) -> list[TerminalPreviewItem]:
    if rendered_views.views:
        return [
            TerminalPreviewItem(
                id=view.id,
                title=view.title,
                mermaid_source=view.mermaid_source,
            )
            for view in rendered_views.views
        ]
    return [TerminalPreviewItem(id="", title="", mermaid_source=rendered_mermaid_source)]


def write_terminal_svg(path: Path, mermaid_source: str, *, width: int, title: str) -> Path:
    """Render Mermaid through termaid/Rich and save the terminal view as SVG."""

    path.parent.mkdir(parents=True, exist_ok=True)
    console = Console(width=width, record=True, force_terminal=True, color_system="truecolor", file=io.StringIO())
    console.print(_render_terminal_rich(mermaid_source))
    console.save_svg(str(path), title=title)
    return path


def write_terminal_png(path: Path, mermaid_source: str, *, width: int, title: str) -> Path:
    """Render Mermaid through termaid/Rich and convert the terminal SVG to PNG."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as handle:
        svg_path = Path(handle.name)
    try:
        write_terminal_svg(svg_path, mermaid_source, width=width, title=title)
        convert_svg_to_png(svg_path, path)
    finally:
        svg_path.unlink(missing_ok=True)
    return path


def write_view_mermaid_files(directory: Path, views: ArchitectureMultiViewRenderResult) -> list[Path]:
    """Write one Mermaid source file per architecture view."""

    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, view in enumerate(views.views, start=1):
        path = directory / f"{index:02d}-{_safe_view_filename(view.id)}.mmd"
        path.write_text(view.mermaid_source, encoding="utf-8")
        paths.append(path)
    return paths


def write_view_terminal_svgs(
    directory: Path,
    views: ArchitectureMultiViewRenderResult,
    *,
    width: int,
    title: str,
) -> list[Path]:
    """Render one Rich terminal SVG per architecture view."""

    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, view in enumerate(views.views, start=1):
        path = directory / f"{index:02d}-{_safe_view_filename(view.id)}.terminal.svg"
        write_terminal_svg(path, view.mermaid_source, width=width, title=f"{title} - {view.title}")
        paths.append(path)
    return paths


def write_view_terminal_pngs(
    directory: Path,
    views: ArchitectureMultiViewRenderResult,
    *,
    width: int,
    title: str,
) -> list[Path]:
    """Render one Rich terminal PNG per architecture view."""

    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, view in enumerate(views.views, start=1):
        path = directory / f"{index:02d}-{_safe_view_filename(view.id)}.terminal.png"
        write_terminal_png(path, view.mermaid_source, width=width, title=f"{title} - {view.title}")
        paths.append(path)
    return paths


def record_terminal_preview(args: argparse.Namespace, *, console: Console | None = None) -> None:
    """Record the real terminal preview stream and render its final frame to image files."""

    console = console or Console()
    requested_png = args.record_terminal_png_out
    requested_gif = args.record_terminal_gif_out
    requested_cast = args.record_terminal_cast_out
    needs_gif = requested_png is not None or requested_gif is not None
    required_commands = ["asciinema"]
    if needs_gif:
        required_commands.append("agg")
    if requested_png is not None:
        required_commands.append("ffmpeg")
    missing_commands = [command for command in required_commands if shutil.which(command) is None]
    if missing_commands:
        raise RuntimeError("Missing command(s) for terminal recording: {}".format(", ".join(missing_commands)))

    for path in (requested_png, requested_gif, requested_cast):
        if path is not None:
            path.expanduser().parent.mkdir(parents=True, exist_ok=True)

    cols = max(1, int(args.record_terminal_cols))
    rows = max(1, int(args.record_terminal_rows))
    with tempfile.TemporaryDirectory(prefix="iac-arch-terminal-record-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        cast_path = requested_cast.expanduser() if requested_cast is not None else tmp_path / "terminal.cast"
        gif_path = requested_gif.expanduser() if requested_gif is not None else tmp_path / "terminal.gif"
        png_path = requested_png.expanduser() if requested_png is not None else None

        child_command = _record_terminal_child_command(args, width=cols)
        shell_command = _record_terminal_shell_command(child_command)
        subprocess.run(
            [
                "asciinema",
                "rec",
                "-q",
                "--overwrite",
                "--headless",
                "--return",
                "--window-size",
                f"{cols}x{rows}",
                "-c",
                shell_command,
                str(cast_path),
            ],
            check=True,
        )
        if needs_gif:
            subprocess.run(
                [
                    "agg",
                    "-q",
                    "--cols",
                    str(cols),
                    "--rows",
                    str(rows),
                    "--font-size",
                    str(args.record_terminal_font_size),
                    "--line-height",
                    str(args.record_terminal_line_height),
                    "--theme",
                    args.record_terminal_theme,
                    "--select",
                    "100%",
                    str(cast_path),
                    str(gif_path),
                ],
                check=True,
            )
        if png_path is not None:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(gif_path),
                    "-frames:v",
                    "1",
                    str(png_path),
                ],
                check=True,
            )

    if requested_cast is not None:
        console.print(f"[green]Wrote terminal cast:[/] {requested_cast.expanduser()}")
    if requested_gif is not None:
        console.print(f"[green]Wrote terminal GIF:[/] {requested_gif.expanduser()}")
    if requested_png is not None:
        console.print(f"[green]Wrote terminal PNG:[/] {requested_png.expanduser()}")


def _record_terminal_child_command(args: argparse.Namespace, *, width: int) -> list[str]:
    command = [
        sys.executable,
        str(_preview_script_entrypoint()),
        str(args.template.expanduser()),
        "--quiet",
        "--width",
        str(width),
        "--max-tokens",
        str(args.max_tokens),
        "--max-attempts",
        str(max(1, args.max_attempts)),
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.enable_thinking:
        command.append("--enable-thinking")
    for option, value in (
        ("--mermaid-out", args.mermaid_out),
        ("--view-mermaid-dir", args.view_mermaid_dir),
        ("--plan-out", args.plan_out),
        ("--html-out", args.html_out),
        ("--prompt-debug-html-out", args.prompt_debug_html_out),
    ):
        if value is not None:
            command.extend([option, str(value.expanduser())])
    return command


def _preview_script_entrypoint() -> Path:
    source_path = Path(__file__).resolve()
    repo_root = source_path.parents[4]
    script_path = repo_root / "scripts" / "rendering" / "preview_template_architecture_llm.py"
    if script_path.exists():
        return script_path
    return source_path


def _record_terminal_shell_command(command: list[str]) -> str:
    child = shlex.join(["env", "TERM=xterm-256color", "COLORTERM=truecolor", "FORCE_COLOR=1", *command])
    hide_cursor = shlex.quote("\033[?25l")
    show_cursor = shlex.quote("\033[?25h")
    return f'printf {hide_cursor}; {child}; status=$?; printf {show_cursor}; exit "$status"'


def _safe_view_filename(value: str) -> str:
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return filename or "view"


def convert_svg_to_png(svg_path: Path, png_path: Path) -> None:
    """Convert an SVG file to PNG with a locally available command-line converter."""

    png_path.parent.mkdir(parents=True, exist_ok=True)
    command = _svg_to_png_command(svg_path, png_path)
    if command is None:
        raise RuntimeError("No SVG to PNG converter found. Install rsvg-convert, ImageMagick, or use macOS sips.")
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _svg_to_png_command(svg_path: Path, png_path: Path) -> list[str] | None:
    if shutil.which("rsvg-convert"):
        return ["rsvg-convert", str(svg_path), "-o", str(png_path)]
    if shutil.which("magick"):
        return ["magick", str(svg_path), str(png_path)]
    if shutil.which("convert"):
        return ["convert", str(svg_path), str(png_path)]
    if shutil.which("sips"):
        return ["sips", "-s", "format", "png", str(svg_path), "--out", str(png_path)]
    return None


def _render_terminal_rich(mermaid_source: str) -> Any:
    render_rich = import_module("termaid").render_rich

    return style_attachment_lines(render_rich(mermaid_source))


def browser_mermaid_source(mermaid_source: str) -> str:
    """Convert terminal-friendly Mermaid to browser Mermaid v11-friendly syntax."""

    def replace_subgraph(match: re.Match[str]) -> str:
        indent, subgraph_id, label = match.group(1), match.group(2), match.group(3)
        return '{}subgraph {}["{}"]'.format(indent, subgraph_id, label.replace('"', "#quot;"))

    return re.sub(
        r"^(\s*)subgraph\s+([A-Za-z0-9_]+)\s+\[(.+?)\]\s*$",
        replace_subgraph,
        mermaid_source,
        flags=re.M,
    )


def write_html(path: Path | None, *, title: str, mermaid_source: str, open_browser: bool) -> Path:
    browser_source = html.escape(browser_mermaid_source(mermaid_source))
    if path is None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
        with handle:
            output_path = Path(handle.name)
            handle.write(HTML_TEMPLATE.format(title=html.escape(title), mermaid=browser_source))
    else:
        output_path = path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(HTML_TEMPLATE.format(title=html.escape(title), mermaid=browser_source), encoding="utf-8")
    if open_browser:
        webbrowser.open(f"file://{output_path}")
    return output_path


def write_prompt_debug_html(
    path: Path,
    *,
    title: str,
    model: str,
    records: list[dict[str, Any]],
    timings: dict[str, float],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(_prompt_debug_row(record) for record in records)
    attempts = "\n".join(_prompt_debug_attempt_section(record) for record in records)
    metrics = "\n".join(
        _prompt_debug_metric(label, value)
        for label, value in (
            ("Attempts", str(len(records))),
            ("LLM Time", _format_elapsed(timings.get("llm", 0.0))),
            ("Total Time", _format_elapsed(timings.get("total", 0.0))),
            ("Total Prompt Chars", str(sum(int(record.get("prompt_chars") or 0) for record in records))),
            (
                "Total Cache Read Tokens",
                str(sum(int(record.get("cache_read_input_tokens") or 0) for record in records)),
            ),
        )
    )
    page = PROMPT_DEBUG_HTML_TEMPLATE.format(
        title=html.escape(f"Prompt Debug: {title}"),
        model=html.escape(model),
        generated_at=html.escape(time.strftime("%Y-%m-%d %H:%M:%S %z")),
        metrics=metrics,
        rows=rows,
        attempts=attempts,
    )
    path.write_text(page, encoding="utf-8")
    return path


def _prompt_debug_metric(label: str, value: str) -> str:
    return f'<div class="metric"><strong>{html.escape(label)}</strong><span>{html.escape(value)}</span></div>'


def _prompt_debug_row(record: dict[str, Any]) -> str:
    return (
        "<tr>"
        f"<td>{html.escape(str(record.get('attempt') or ''))}</td>"
        f"<td>{_prompt_debug_selected(record)}</td>"
        f"<td>{html.escape(_format_elapsed(float(record.get('llm_seconds') or 0.0)))}</td>"
        f"<td>{html.escape(str(record.get('prompt_chars') or 0))}</td>"
        f"<td>{html.escape(str(record.get('cacheable_prefix_chars') or 0))}</td>"
        f"<td>{html.escape(str(len(record.get('sent_validation_issues') or [])))}</td>"
        f"<td>{html.escape(str(len(record.get('validation_issues') or [])))}</td>"
        f"<td>{html.escape(str(record.get('cache_read_input_tokens') or 0))}</td>"
        f"<td>{html.escape(str(record.get('raw_output_chars') or len(str(record.get('raw_output') or ''))))}</td>"
        "</tr>"
    )


def _prompt_debug_selected(record: dict[str, Any]) -> str:
    if record.get("selected"):
        return '<span class="badge">selected</span>'
    return ""


def _prompt_debug_attempt_section(record: dict[str, Any]) -> str:
    attempt = html.escape(str(record.get("attempt") or ""))
    selected = ' <span class="badge">selected</span>' if record.get("selected") else ""
    sent_issues = _prompt_debug_issue_list(record.get("sent_validation_issues"))
    result_issues = _prompt_debug_issue_list(record.get("validation_issues"))
    system_prompt = str(record.get("system_prompt") or "")
    user_prompt = str(record.get("user_prompt") or "")
    messages = record.get("messages")
    if not isinstance(messages, list):
        messages = [{"role": "user", "content": user_prompt}]
    prompt_order = _prompt_debug_prompt_order(system_prompt, messages)
    full_request_prompt = _prompt_debug_full_request_prompt(system_prompt, messages)
    return f"""
  <div class="card">
    <h2>Attempt {attempt}{selected}</h2>
    <div class="grid">
      {_prompt_debug_metric("LLM Time", _format_elapsed(float(record.get("llm_seconds") or 0.0)))}
      {_prompt_debug_metric("System Prompt Chars", str(record.get("system_prompt_chars") or 0))}
      {_prompt_debug_metric("User Prompt Chars", str(record.get("user_prompt_chars") or 0))}
      {_prompt_debug_metric("Cacheable Prefix Chars", str(record.get("cacheable_prefix_chars") or 0))}
      {_prompt_debug_metric("Common Prefix With Previous", str(record.get("common_prefix_with_previous_chars") or 0))}
      {_prompt_debug_metric("Total Prompt Chars", str(record.get("prompt_chars") or 0))}
      {_prompt_debug_metric("Input Tokens", str(record.get("input_tokens") or 0))}
      {_prompt_debug_metric("Output Tokens", str(record.get("output_tokens") or 0))}
      {_prompt_debug_metric("Cache Read Tokens", str(record.get("cache_read_input_tokens") or 0))}
      {_prompt_debug_metric("Cache Create Tokens", str(record.get("cache_creation_input_tokens") or 0))}
      {_prompt_debug_metric("Parse Error", str(record.get("parse_error") or "None"))}
      {_prompt_debug_metric("Raw Output Chars", str(record.get("raw_output_chars") or 0))}
    </div>
    <details open>
      <summary>Prompt Sent Order</summary>
      {prompt_order}
    </details>
    <details open>
      <summary>Full Request Prompt</summary>
      <pre>{html.escape(full_request_prompt)}</pre>
    </details>
    <details open>
      <summary>Sent Validation Issues ({len(record.get("sent_validation_issues") or [])})</summary>
      {sent_issues}
    </details>
    <details>
      <summary>Result Validation Issues ({len(record.get("validation_issues") or [])})</summary>
      {result_issues}
    </details>
    <details>
      <summary>Raw LLM Output</summary>
      <pre>{html.escape(str(record.get("raw_output") or ""))}</pre>
    </details>
  </div>
"""


def _prompt_debug_full_request_prompt(system_prompt: str, messages: list[Any]) -> str:
    parts = [f"system:\n{system_prompt}"]
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        content = str(message.get("content") or "")
        parts.append(f"{role}:\n{content}")
    return "\n\n".join(parts)


def _prompt_debug_prompt_order(system_prompt: str, messages: list[Any]) -> str:
    steps: list[str] = []
    step_index = 1
    user_index = 0
    assistant_index = 0
    steps.append(_prompt_debug_prompt_step(f"{step_index}. System Prompt", system_prompt))
    step_index += 1
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role == "user":
            user_index += 1
            cacheable_prompt, dynamic_prompt = split_by_dynamic_boundary(content)
            if dynamic_prompt:
                steps.append(
                    _prompt_debug_prompt_step(
                        f"{step_index}. User Message {user_index} Cacheable Prefix",
                        cacheable_prompt,
                    )
                )
                step_index += 1
                steps.append(_prompt_debug_prompt_step(f"{step_index}. Dynamic Boundary", DYNAMIC_BOUNDARY))
                step_index += 1
                steps.append(
                    _prompt_debug_prompt_step(
                        f"{step_index}. User Message {user_index} Dynamic Instruction",
                        dynamic_prompt,
                        open_by_default=True,
                    )
                )
                step_index += 1
            else:
                steps.append(
                    _prompt_debug_prompt_step(
                        f"{step_index}. User Message {user_index}",
                        content,
                        open_by_default=True,
                    )
                )
                step_index += 1
        elif role == "assistant":
            assistant_index += 1
            steps.append(_prompt_debug_prompt_step(f"{step_index}. Assistant Message {assistant_index}", content))
            step_index += 1
        else:
            steps.append(_prompt_debug_prompt_step(f"{step_index}. {role or 'Message'}", content))
            step_index += 1
    return "\n".join(steps)


def _prompt_debug_prompt_step(title: str, content: str, *, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    return (
        f'<details class="prompt-step"{open_attr}>'
        f"<summary>{html.escape(title)}</summary>"
        f"<pre>{html.escape(content)}</pre>"
        "</details>"
    )


def _prompt_debug_issue_list(value: Any) -> str:
    issues = value if isinstance(value, list) else []
    if not issues:
        return '<div class="muted" style="padding: 0 12px 12px;">None</div>'
    items = "\n".join(f'<li class="issue">{html.escape(str(issue))}</li>' for issue in issues)
    return f"<ul>{items}</ul>"


def _message_text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        content = getattr(block, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts)


def _message_debug_dict(message: Message) -> dict[str, str]:
    return {"role": message.role, "content": _message_text(message)}


def _prompt_request_text(system_prompt: str, messages: list[Message]) -> str:
    parts = [f"system:\n{system_prompt}"]
    for message in messages:
        parts.append(f"{message.role}:\n{_message_text(message)}")
    return "\n\n".join(parts)


def _cacheable_message_prefix_chars(messages: list[Message]) -> int:
    total = 0
    for message in messages:
        if message.role != "user":
            continue
        cacheable_prompt, dynamic_prompt = split_by_dynamic_boundary(_message_text(message))
        if dynamic_prompt:
            total += len(cacheable_prompt)
    return total


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a ROS template architecture diagram with real LLM semantics.")
    parser.add_argument("template", type=Path, help="ROS template YAML file path.")
    parser.add_argument("--model", default=None, help="Override model. Defaults to saved iac-code model.")
    parser.add_argument("--max-tokens", type=int, default=3000, help="Max tokens for the semantic plan. Default: 3000.")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=MAX_SEMANTIC_PLAN_ATTEMPTS,
        help="Max LLM validation attempts. Default: 3.",
    )
    parser.add_argument("--width", type=int, default=180, help="Terminal render width. Default: 180.")
    parser.add_argument("--print-facts", action="store_true", help="Print the fact bundle sent to the LLM.")
    parser.add_argument("--print-raw", action="store_true", help="Print raw LLM output.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress verbose validation, semantic_plan, and Mermaid dumps; still writes outputs and renders previews."
        ),
    )
    parser.add_argument("--no-terminal", action="store_true", help="Skip termaid terminal rendering.")
    parser.add_argument("--mermaid-out", type=Path, default=None, help="Write final Mermaid source to this file.")
    parser.add_argument("--view-mermaid-dir", type=Path, default=None, help="Write per-view Mermaid sources here.")
    parser.add_argument("--plan-out", type=Path, default=None, help="Write parsed LLM semantic_plan JSON to this file.")
    parser.add_argument("--terminal-svg-out", type=Path, default=None, help="Write Rich terminal SVG to this file.")
    parser.add_argument("--terminal-png-out", type=Path, default=None, help="Write Rich terminal PNG to this file.")
    parser.add_argument("--terminal-svg-dir", type=Path, default=None, help="Write per-view Rich terminal SVGs here.")
    parser.add_argument("--terminal-png-dir", type=Path, default=None, help="Write per-view Rich terminal PNGs here.")
    parser.add_argument(
        "--record-terminal-png-out",
        type=Path,
        default=None,
        help="Record the real terminal preview with asciinema/agg and write the final frame PNG.",
    )
    parser.add_argument(
        "--record-terminal-gif-out",
        type=Path,
        default=None,
        help="Record the real terminal preview with asciinema/agg and write the final frame GIF.",
    )
    parser.add_argument(
        "--record-terminal-cast-out",
        type=Path,
        default=None,
        help="Write the asciinema cast used for terminal preview image rendering.",
    )
    parser.add_argument(
        "--record-terminal-cols",
        type=int,
        default=220,
        help="Recording terminal width in columns. Default: 220.",
    )
    parser.add_argument(
        "--record-terminal-rows",
        type=int,
        default=140,
        help="Recording terminal height in rows. Default: 140.",
    )
    parser.add_argument(
        "--record-terminal-font-size",
        type=int,
        default=16,
        help="agg font size in pixels. Default: 16.",
    )
    parser.add_argument(
        "--record-terminal-line-height",
        default="1.25",
        help="agg line-height value. Default: 1.25.",
    )
    parser.add_argument(
        "--record-terminal-theme",
        default="github-dark",
        help="agg color theme. Default: github-dark.",
    )
    parser.add_argument("--html-out", type=Path, default=None, help="Write Mermaid HTML preview to this file.")
    parser.add_argument(
        "--prompt-debug-html-out",
        type=Path,
        default=None,
        help="Write an HTML report containing the exact system/user prompt sent for every LLM attempt.",
    )
    parser.add_argument(
        "--enable-thinking",
        action="store_true",
        help=(
            "Use the saved provider thinking/effort setting. By default this preview disables thinking when supported."
        ),
    )
    parser.add_argument("--open-html", action="store_true", help="Open HTML preview in the default browser.")
    return parser.parse_args(argv)


def _record_terminal_requested(args: argparse.Namespace) -> bool:
    return any(
        path is not None
        for path in (
            args.record_terminal_png_out,
            args.record_terminal_gif_out,
            args.record_terminal_cast_out,
        )
    )


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def format_timing_summary(timings: dict[str, float]) -> str:
    labels = {
        "load_and_facts": "load/facts",
        "llm": "llm",
        "validate": "validate/render",
        "select_plan": "select plan",
        "views": "views",
        "write_outputs": "write outputs",
        "terminal_preview": "terminal preview",
        "total": "total",
    }
    parts = []
    for key in (
        "load_and_facts",
        "llm",
        "validate",
        "select_plan",
        "views",
        "write_outputs",
        "terminal_preview",
        "total",
    ):
        if key in timings:
            parts.append(f"{labels[key]}={_format_elapsed(timings[key])}")
    return "timing: " + ", ".join(parts)


async def async_main(argv: list[str]) -> int:
    started_at = time.perf_counter()
    timings: dict[str, float] = {}
    setup_i18n()
    args = parse_args(argv)
    console = Console()
    template_path = args.template.expanduser()
    if not template_path.is_file():
        console.print(f"[red]Template file does not exist: {template_path}[/]")
        return 1
    if _record_terminal_requested(args):
        try:
            record_terminal_preview(args, console=console)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            console.print(f"[red]Terminal recording failed: {exc}[/]")
            return 1
        return 0

    phase_started = time.perf_counter()
    template_content = template_path.read_text(encoding="utf-8")
    base_result = render_ros_template_architecture(template_content)
    llm_architecture_context = build_llm_architecture_context(base_result.architecture_context)
    model = args.model or load_saved_model() or DEFAULT_MODEL
    effort_override = None if args.enable_thinking else "none"
    timings["load_and_facts"] = time.perf_counter() - phase_started

    console.print(f"[dim]Template:[/] {template_path}")
    console.print(f"[dim]Model:[/] {model}")
    if effort_override == "none":
        console.print("[dim]Thinking:[/] disabled for this preview request")
    if args.print_facts:
        console.print("[bold]--- FACT BUNDLE ---[/]")
        console.print_json(json.dumps(llm_architecture_context, ensure_ascii=False))

    max_attempts = max(1, args.max_attempts)
    raw_output = ""
    semantic_plan: dict[str, Any] = {}
    rendered = base_result
    validation_issues: list[str] = []
    attempt_records: list[dict[str, Any]] = []
    prompt_debug_records: list[dict[str, Any]] = []
    best_raw_output = ""
    best_semantic_plan: dict[str, Any] = {}
    best_rendered = base_result
    best_validation_issues: list[str] | None = None
    best_attempt = 0
    llm_elapsed = 0.0
    validate_elapsed = 0.0
    conversation_messages: list[Message] = []
    previous_request_text = ""
    for attempt in range(1, max_attempts + 1):
        sent_validation_issues = list(validation_issues)
        sent_previous_plan = None
        user_prompt = build_semantic_plan_user_prompt(
            llm_architecture_context,
            attempt=attempt,
            previous_plan=sent_previous_plan,
            validation_issues=sent_validation_issues,
            include_fact_bundle=attempt == 1,
            include_previous_plan=False,
        )
        request_messages = [*conversation_messages, Message.user(user_prompt)]
        request_text = _prompt_request_text(SYSTEM_PROMPT, request_messages)
        common_prefix_chars = 0
        if previous_request_text:
            for left, right in zip(previous_request_text, request_text):
                if left != right:
                    break
                common_prefix_chars += 1
        prompt_debug_record: dict[str, Any] = {
            "attempt": attempt,
            "system_prompt": SYSTEM_PROMPT,
            "user_prompt": user_prompt,
            "messages": [_message_debug_dict(message) for message in request_messages],
            "system_prompt_chars": len(SYSTEM_PROMPT),
            "user_prompt_chars": len(user_prompt),
            "cacheable_prefix_chars": _cacheable_message_prefix_chars(request_messages),
            "common_prefix_with_previous_chars": common_prefix_chars,
            "prompt_chars": len(SYSTEM_PROMPT) + sum(len(_message_text(message)) for message in request_messages),
            "sent_validation_issues": sent_validation_issues,
            "sent_previous_plan": sent_previous_plan,
        }
        prompt_debug_records.append(prompt_debug_record)
        phase_started = time.perf_counter()
        raw_output, parsed_semantic_plan, usage, parse_error = await create_semantic_plan_with_llm(
            llm_architecture_context,
            model=model,
            max_tokens=args.max_tokens,
            effort_override=effort_override,
            user_prompt=user_prompt,
            messages=request_messages,
            attempt=attempt,
            previous_plan=sent_previous_plan,
            validation_issues=sent_validation_issues,
        )
        previous_request_text = request_text
        conversation_messages = [*request_messages, Message.assistant_text(raw_output)]
        attempt_llm_elapsed = time.perf_counter() - phase_started
        llm_elapsed += attempt_llm_elapsed
        prompt_debug_record["llm_seconds"] = attempt_llm_elapsed
        prompt_debug_record["input_tokens"] = getattr(usage, "input_tokens", 0) or 0
        prompt_debug_record["output_tokens"] = getattr(usage, "output_tokens", 0) or 0
        prompt_debug_record["cache_creation_input_tokens"] = getattr(usage, "cache_creation_input_tokens", 0) or 0
        prompt_debug_record["cache_read_input_tokens"] = getattr(usage, "cache_read_input_tokens", 0) or 0
        prompt_debug_record["raw_output"] = raw_output
        prompt_debug_record["raw_output_chars"] = len(raw_output)
        prompt_debug_record["parse_error"] = parse_error
        if parsed_semantic_plan is None:
            semantic_plan = {}
            validation_issues = [f"LLM output was not valid semantic_plan JSON: {parse_error}"]
            prompt_debug_record["validation_issues"] = list(validation_issues)
            attempt_records.append({"attempt": attempt, "issues": validation_issues, "parse_error": parse_error})
            if not should_retry_semantic_plan_attempt(attempt, max_attempts, validation_issues):
                break
            continue
        semantic_plan = repair_semantic_plan_locally(base_result.architecture_context, parsed_semantic_plan)
        if semantic_plan != parsed_semantic_plan:
            prompt_debug_record["locally_repaired"] = True
        phase_started = time.perf_counter()
        rendered = render_ros_template_architecture(template_content, semantic_plan=semantic_plan)
        validation_issues = validate_semantic_plan_result(
            base_result.architecture_context,
            semantic_plan,
            rendered.architecture_context,
        )
        prompt_debug_record["validation_issues"] = list(validation_issues)
        validate_elapsed += time.perf_counter() - phase_started
        attempt_records.append({"attempt": attempt, "issues": validation_issues})
        if best_validation_issues is None or validation_issue_score(validation_issues) < validation_issue_score(
            best_validation_issues
        ):
            best_raw_output = raw_output
            best_semantic_plan = semantic_plan
            best_rendered = rendered
            best_validation_issues = validation_issues
            best_attempt = attempt
        if not should_retry_semantic_plan_attempt(attempt, max_attempts, validation_issues):
            break
    timings["llm"] = llm_elapsed
    timings["validate"] = validate_elapsed
    phase_started = time.perf_counter()
    if best_attempt:
        raw_output = best_raw_output
        semantic_plan = best_semantic_plan
        rendered = best_rendered
        validation_issues = best_validation_issues or []
        for record in attempt_records:
            record["selected"] = record["attempt"] == best_attempt
        for record in prompt_debug_records:
            record["selected"] = record["attempt"] == best_attempt
    timings["select_plan"] = time.perf_counter() - phase_started

    if args.print_raw:
        console.print("[bold]--- RAW LLM OUTPUT ---[/]")
        console.print(raw_output)
    phase_started = time.perf_counter()
    rendered_views = render_ros_template_architecture_views(template_content, semantic_plan=semantic_plan)
    timings["views"] = time.perf_counter() - phase_started
    if not args.quiet:
        console.print("[bold]--- VALIDATION ATTEMPTS ---[/]")
        console.print_json(json.dumps(attempt_records, ensure_ascii=False))
        console.print("[bold]--- PARSED semantic_plan ---[/]")
        console.print_json(json.dumps(semantic_plan, ensure_ascii=False))
        console.print("[bold]--- ACCEPTED / REJECTED ---[/]")
        console.print_json(json.dumps(rendered.architecture_context["semantic_plan"], ensure_ascii=False))
        console.print("[bold]--- MERMAID ---[/]")
        console.print(rendered.mermaid_source)
        if rendered_views.views:
            console.print("[bold]--- MERMAID VIEWS ---[/]")
            for view in rendered_views.views:
                console.print(f"[cyan]{view.title}[/] [dim]({view.id})[/]")
                console.print(view.mermaid_source)
    else:
        selected_attempt = next((record["attempt"] for record in attempt_records if record.get("selected")), None)
        selected_attempt = selected_attempt or (attempt_records[-1]["attempt"] if attempt_records else 0)
        console.print(f"[dim]LLM attempts:[/] {len(attempt_records)} [dim]selected:[/] {selected_attempt}")
        if validation_issues:
            console.print(f"[yellow]Validation issues kept:[/] {len(validation_issues)}")

    phase_started = time.perf_counter()
    if args.plan_out is not None:
        args.plan_out.parent.mkdir(parents=True, exist_ok=True)
        args.plan_out.write_text(json.dumps(semantic_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]Wrote semantic plan:[/] {args.plan_out}")
    if args.mermaid_out is not None:
        args.mermaid_out.parent.mkdir(parents=True, exist_ok=True)
        args.mermaid_out.write_text(rendered.mermaid_source, encoding="utf-8")
        console.print(f"[green]Wrote Mermaid:[/] {args.mermaid_out}")
    if args.view_mermaid_dir is not None:
        paths = write_view_mermaid_files(args.view_mermaid_dir, rendered_views)
        console.print(f"[green]Wrote {len(paths)} Mermaid views:[/] {args.view_mermaid_dir}")
    if args.html_out is not None or args.open_html:
        html_path = write_html(
            args.html_out,
            title=template_path.name,
            mermaid_source=rendered.mermaid_source,
            open_browser=args.open_html,
        )
        console.print(f"[green]Wrote HTML preview:[/] {html_path}")
    if args.prompt_debug_html_out is not None:
        timings["total"] = time.perf_counter() - started_at
        prompt_debug_path = write_prompt_debug_html(
            args.prompt_debug_html_out,
            title=template_path.name,
            model=model,
            records=prompt_debug_records,
            timings=timings,
        )
        console.print(f"[green]Wrote prompt debug HTML:[/] {prompt_debug_path}")
    if args.terminal_svg_out is not None:
        write_terminal_svg(
            args.terminal_svg_out,
            rendered.mermaid_source,
            width=args.width,
            title=template_path.name,
        )
        console.print(f"[green]Wrote terminal SVG:[/] {args.terminal_svg_out}")
    if args.terminal_png_out is not None:
        write_terminal_png(
            args.terminal_png_out,
            rendered.mermaid_source,
            width=args.width,
            title=template_path.name,
        )
        console.print(f"[green]Wrote terminal PNG:[/] {args.terminal_png_out}")
    if args.terminal_svg_dir is not None:
        paths = write_view_terminal_svgs(
            args.terminal_svg_dir,
            rendered_views,
            width=args.width,
            title=template_path.name,
        )
        console.print(f"[green]Wrote {len(paths)} terminal SVG views:[/] {args.terminal_svg_dir}")
    if args.terminal_png_dir is not None:
        paths = write_view_terminal_pngs(
            args.terminal_png_dir,
            rendered_views,
            width=args.width,
            title=template_path.name,
        )
        console.print(f"[green]Wrote {len(paths)} terminal PNG views:[/] {args.terminal_png_dir}")
    timings["write_outputs"] = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    if not args.no_terminal:
        preview_items = _terminal_preview_items(rendered.mermaid_source, rendered_views)
        if any(item.id for item in preview_items):
            console.print("[bold]--- TERMAID VIEW PREVIEWS ---[/]")
            for item in preview_items:
                console.print(f"[bold]{item.title}[/] [dim]({item.id})[/]")
                render_terminal(console, item.mermaid_source, width=args.width)
        else:
            console.print("[bold]--- TERMAID PREVIEW ---[/]")
            render_terminal(console, preview_items[0].mermaid_source, width=args.width)
    timings["terminal_preview"] = time.perf_counter() - phase_started
    timings["total"] = time.perf_counter() - started_at
    console.print(f"[dim]{format_timing_summary(timings)}[/]")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
