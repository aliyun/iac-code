#!/usr/bin/env python3
"""Read-only real LLM + local A2A Wiki acceptance; invoke through run_e2e.py.

Each normal/handoff repetition starts a fresh context. No scenario retries,
answer hints, substituted tool results or cloud writes. Private HTTP/session
evidence stays in the run directory; summaries contain only bounded metrics.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY  # noqa: E402
from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    fetch_pipeline_state,
    stream_message,
    wait_for_server,
)
from scripts.ci.ecs_dryrun_guard import explicit_ecs_dry_run  # noqa: E402
from scripts.ci.run_coupon_budget_a2a import tool_inputs  # noqa: E402
from scripts.ci.run_plan_cost_refresh_a2a import envelopes  # noqa: E402
from scripts.ci.run_promotional_offer_followup_a2a import visible_pipeline_text, walk  # noqa: E402
from scripts.ci.run_step_followup_a2a import results  # noqa: E402
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402

P19 = (
    "我要购买按量付费的tdx，可信计算服务器",
    "TDX 机密虚拟机（CVM）\tTDVM、TDX Module quote、DCAP\tg8i/c8i/r8i（+部分地域 g9i 系）\t"
    "Sapphire Rapids（四代至强）。你推荐的是sgx机型？",
    "阿里云有没有B70 intel的服务器？",
    "B70是一款intel显卡",
)
NORMAL = "Intel Arc Pro B70 是什么显卡？架构和显存规格是什么？"
SIZING = "阿里云经济型 e 和突发性能型 t 有什么区别？它们都是靠 CPU 积分控制性能的吗？"
BOUNDARIES = (
    "RISC-V 向量扩展 RVV 1.0 的 VLEN 与 AVX-512 的向量寄存器宽度有什么区别？",
    "帮我把“祝你生日快乐”翻译成英文。",
)
CANCEL = "取消这次购买，结束当前方案流程，接下来只进行普通聊天。"
READ_PREFIXES = ("Describe", "List", "Get", "Query")
AUTO_NORMAL = (
    "What are the architecture and VRAM specifications of Intel Arc Pro B70?",
    "把刚才的解释整理成两行，不增加新内容。",
    "阿里云 ECS 按量计费询价时，DescribePrice 的 PriceUnit 应该怎么传？",
)
AUTO_PIPELINE = (
    "在杭州临时用 2 天部署 nginx 静态站点，2 核 2 GiB、40 GiB 系统盘，需要公网访问，"
    "按量付费，先给方案和费用估计，不部署。"
)


def _reference_blocks(value: Any) -> list[str]:
    if isinstance(value, str):
        if value.startswith('<knowledge_search_results source="runtime">'):
            return [value]
        if value.startswith(("[", "{")):
            try:
                return _reference_blocks(json.loads(value))
            except ValueError:
                pass
    if isinstance(value, dict):
        return [block for child in value.values() for block in _reference_blocks(child)]
    if isinstance(value, list):
        return [block for child in value for block in _reference_blocks(child)]
    return []


def auto_evidence(config: Path, records: list[dict] | None = None) -> dict[str, Any]:
    """Count excerpts only when their actual numbered source text reached a main request."""
    queries = {}
    for path in (config / "projects").rglob("session.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line).get("metadata", {}).get("wiki_auto_search")
            if isinstance(record, dict) and record.get("query_id"):
                queries[record["query_id"]] = record
    requests = []
    for record in records or []:
        attrs = record.get("attributes", {})
        if record.get("kind") != "span" or attrs.get("gen_ai.span.kind") != "LLM":
            continue
        system = str(attrs.get("gen_ai.system_instructions", ""))
        messages = str(attrs.get("gen_ai.input.messages", ""))
        helper = "Wiki keyword extraction" in system
        blocks = [] if helper else _reference_blocks(attrs.get("gen_ai.input.messages", ""))
        pages = sorted({hit["path"] for query in queries.values() for hit in query.get("hits", [])
                        if any(f"- {line['number']}: {html.escape(line['text'], quote=False)}" in block
                               for line in hit.get("lines", []) for block in blocks)})
        plain = "\n".join(html.unescape(block).replace("**", "") for block in blocks)
        requests.append({
            "kind": "helper" if helper else "main", "model": attrs.get("gen_ai.request.model"),
            "reference_blocks": messages.count("<knowledge_search_results"),
            "system_has_results": "<knowledge_search_results" in system,
            "excerpt_pages": pages,
            "hardware_excerpt": "32 GB GDDR6" in plain and "Xe2" in plain,
            "step_id": attrs.get("step_id"),
            "ttft_ns": attrs.get("gen_ai.response.time_to_first_token"),
            "input_tokens": attrs.get("gen_ai.usage.input_tokens"),
            "output_tokens": attrs.get("gen_ai.usage.output_tokens"),
        })
    return {"queries": list(queries.values()), "requests": requests,
            "serial_queries": sum(q.get("dispatch_mode") == "serial" for q in queries.values()),
            "async_queries": sum(q.get("dispatch_mode") == "async" for q in queries.values())}


def auto_checks(evidence: dict[str, Any]) -> dict[str, bool]:
    requests = evidence["requests"]
    mains = [r for r in requests if r["kind"] == "main"]
    return {
        "real keyword helper requests captured": any(r["kind"] == "helper" for r in requests),
        "real main request consumed search navigation": any(r["reference_blocks"] == 1 for r in mains),
        "request navigation never enters system or accumulates": bool(mains) and all(
            not r["system_has_results"] and r["reference_blocks"] <= 1 for r in mains),
    }


def wiki_evidence(calls: list[dict], outcomes: dict[str, dict]) -> dict[str, Any]:
    """Count actual successful bodies, not filenames merely mentioned in tool inputs."""
    pages = set()
    found = False
    hardware_body = False
    for call in calls:
        output = outcomes.get(call["id"], {})
        if output.get("is_error") is not False:
            continue
        content = str(output.get("content", ""))
        normalized = content.replace("\\", "/").replace("knowledge://", "/knowledge/")
        normalized = normalized.replace("/references/cloud-products/", "/knowledge/clouds/aliyun/products/")
        if call["name"] in {"grep", "glob"} and "/knowledge/" in normalized:
            found = True
        if call["name"] != "read_file":
            continue
        header = re.match(r"File: (.+?) \(", normalized)
        if header and "/knowledge/" in header[1]:
            pages.add(header[1].split("/knowledge/", 1)[1])
            hardware_body |= "32 GB GDDR6" in content and "Xe2" in content
    return {"found": found or bool(pages), "read": bool(pages), "pages": sorted(pages),
            "hardware_body_read": hardware_body}


def b70_used_correctly(answer: str) -> bool:
    return bool(
        re.search(r"B70", answer, re.I)
        and re.search(r"Xe2|Battlemage", answer, re.I)
        and re.search(r"32\s*(?:G(?:i?B)?|GB|吉)", answer, re.I)
        and re.search(r"GDDR\s*6", answer, re.I)
        and not b70_identity_denied(answer)
    )


def b70_identity_denied(answer: str) -> bool:
    if re.search(r"B70.{0,12}(?:不是显卡|并非显卡|不是\s*GPU|并非\s*GPU)", answer, re.I):
        return True
    return any(not re.search(r"目录|库存|实例|规格|云", match[1]) for match in re.finditer(
        r"B70([^。\n]{0,12}?)(?:并不|不)存在", answer, re.I,
    ))


def b70_identity_confirmed(answer: str) -> bool:
    """P19's correction asks for GPU recognition, not another full specification."""
    return bool(
        re.search(r"B70", answer, re.I)
        and re.search(r"GPU|显卡|graphics card", answer, re.I)
        and not b70_identity_denied(answer)
    )


_INSTANCE_TYPE = r"ecs\.[a-z0-9-]+\.[a-z0-9]+"
_INSTANCE_FAMILY = r"(?<![a-z0-9])(?:ecs\.)?([a-z]+\d+[a-z][a-z0-9-]*)(?![a-z0-9-])"
_CPU_COUNT = r"(\d+(?:\.\d+)?)\s*(?:vCPU|核|cores?|c(?=\b|\d))"
_DISK_LABEL = (
    r"系统盘|数据盘|云盘|磁盘|(?:system\s*|data\s*)?disk|cloud_[a-z_]+"
    r"|\b(?:ESSD|SSD)(?:\s+(?:Entry|PL[0-3]))?\b"
)
_MEMORY_LABEL = r"内存|memory|RAM"
_ZONE_ID = r"(?<![a-z0-9])((?:cn|ap|us|eu|me)-[a-z]+(?:-\d+)?)-([a-z])(?![a-z0-9])"


def _p19_location_text(value: Any, regions: dict[str, str]) -> str:
    text = json.dumps(value, ensure_ascii=False).lower()
    for region, label in regions.items():
        text = re.sub(
            r"(?:" + re.escape(region) + "|" + re.escape(label) + r")[\s()（）/-]*"
            r"(?:可用区[\s()（）/-]*)?([a-z])(?![a-z0-9])",
            lambda match: region + "-" + match[1], text,
        )
    return text


def _p19_named_zones(text: str) -> set[str]:
    return {before or after for before, after in re.findall(
        r"可用区\s*([a-z])(?![a-z0-9])|(?<![a-z0-9])([a-z])\s*可用区", text,
    )}


def _p19_api_content(outcome: dict, config: Path | None) -> str:
    metadata = outcome.get("metadata")
    external = metadata.get(EXTERNALIZED_RESULT_PATH_METADATA_KEY) if isinstance(metadata, dict) else None
    if config is not None and isinstance(external, str):
        try:
            path = Path(external).resolve()
            if path.is_relative_to(config.resolve()) and "tool-results" in path.parts and path.is_file():
                return path.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeError):
            pass
    return outcome.get("content", "")


def _p19_instance_row(row: dict) -> bool:
    resource_type = row.get("resource_type", "")
    if resource_type in {"ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"}:
        return True
    if row.get("product", "").lower() != "ecs":
        return False
    spec = str(row.get("recommended_spec", ""))
    # A disk cost may be a separate ECS row, including an annotated Instance type.
    # Missing instance identifiers/sizes on an actual instance must still fail.
    disk = re.search(_DISK_LABEL, resource_type + " " + str(row.get("purpose", "")) + " " + spec, re.I)
    return not (disk and not re.search(_INSTANCE_TYPE + "|" + _CPU_COUNT, spec, re.I))


def _p19_memory_values(spec: str) -> set[float]:
    labeled, unlabeled = set(), set()
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:GiB|GB|G)(?![a-z0-9])", spec, re.I):
        before, after = spec[:match.start()], spec[match.end():]
        clause = re.split(r"[,，;；]", before)[-1] + match[0] + re.split(r"[,，;；]", after)[0]
        disk_clause = re.search(_DISK_LABEL, clause, re.I) and not re.search(
            _CPU_COUNT + "|" + _MEMORY_LABEL, clause, re.I,
        )

        def adjacent(label: str) -> bool:
            return bool(re.search(r"(?:" + label + r")\s*[:：=]?\s*$", before, re.I)
                        or re.match(r"\s*(?:" + label + r")", after, re.I))

        if adjacent(_MEMORY_LABEL):
            labeled.add(float(match[1]))
        elif not disk_clause and not adjacent(_DISK_LABEL):
            unlabeled.add(float(match[1]))
    return labeled or unlabeled


def _p19_pending_tdx_disclosure(detail: dict, row: dict) -> str | None:
    # Keep disclosures scoped to this candidate/instance, not other candidates or tool output.
    text = json.dumps({key: detail.get(key) for key in (
        "candidate_name", "decision_notes", "cost_assumptions",
    )} | {"instance": row}, ensure_ascii=False)
    text = text.replace("**", "").replace("`", "").replace(r"\n", " ")
    tdx = r"TDX\s*(?:支持(?:情况|范围)?|特性可用性|可用性|特性|功能|support|availability)?\s*[:：]?\s*"
    confirmed = tdx + r"(?:已(?:经)?(?:确认|核实|验证|支持)|确定支持|保证可用|is confirmed|verified)"
    confirmed += r"|(?:已(?:经)?确认|保证)\s*(?:支持|可用)\s*TDX|confirmed\s+TDX\s+support"
    if re.search(confirmed, text, re.I):
        return None
    pending = re.search(
        tdx + r"(?:(?:尚未|未经|未|待|有待|尚待|需(?:要)?)(?:进一步)?(?:实测)?(?:核实|验证|确认)"
        r"|(?:is\s+)?(?:pending\s+(?:verification|confirmation)|unverified|not yet verified))"
        r"|(?<![无不])(?:尚未|未|待|有待|需(?:要)?)(?:进一步)?(?:实测)?(?:核实|验证|确认)\s*TDX\s*支持",
        text, re.I,
    )
    return pending[0] if pending else None


def p19_plan_review(
    calls: list[dict], outcomes: dict[str, dict], *, config: Path | None = None,
) -> dict[str, Any]:
    """Check bounded P19 plan facts against real API records and the verified TDX support table.

    Explanatory prose, CPU-generation generalizations and global availability
    assertions still require review; these checks never certify all prose.
    Step 1 may defer a concrete SKU, size or zone to Step 2; omitted facts are not contradictions.
    """
    actual = {}
    for call in calls:
        if call["name"] != "aliyun_api" or call["input"].get("action") != "DescribeInstanceTypes":
            continue
        outcome = outcomes.get(call["id"], {})
        if outcome.get("is_error") is not False:
            continue
        try:
            for node in walk(json.loads(_p19_api_content(outcome, config))):
                if {"InstanceTypeId", "CpuCoreCount", "MemorySize"} <= node.keys():
                    actual[node["InstanceTypeId"]] = (float(node["CpuCoreCount"]), float(node["MemorySize"]))
        except (ValueError, TypeError):
            continue
    details = [call["input"] for call in calls if call["name"] == "show_candidate_detail"
               and outcomes.get(call["id"], {}).get("is_error") is False]
    sizes, supported, quantities, specification_evidence, placement_evidence = [], [], [], [], []
    observations = []
    # From the previously verified official table; these are test expectations, not runtime routing rules.
    table = (
        ("cn-hangzhou", "杭州", "j", ("r9i",)),
        ("cn-beijing", "北京", "l", ("g8i",)),
        ("cn-beijing", "北京", "i", ("g8i", "g9i", "c9i", "r9i", "ebmg8i")),
        ("cn-hongkong", "香港", "bcd", ("g8i",)),
        ("ap-southeast-1", "新加坡", "b", ("g8i",)),
    )
    regions = {region: chinese for region, chinese, _, _ in table}
    for detail in details:
        inventory = detail.get("resource_inventory", [])
        location_sources = {
            "inventory": inventory, "topology": detail.get("topology_graph"),
        }
        context = _p19_location_text(location_sources, regions)
        context_regions = {region for region, label in regions.items() if region in context or label in context}
        # Deployment rows/topology take precedence over cost commentary about
        # discarded locations. Assumptions can supply an otherwise missing location.
        if not (re.search(_ZONE_ID, context) or context_regions and _p19_named_zones(context)):
            context = _p19_location_text({**location_sources, "assumptions": detail.get("cost_assumptions")}, regions)
            context_regions = {region for region, label in regions.items() if region in context or label in context}
        placements = set(re.findall(_ZONE_ID, context))
        if len(context_regions) == 1:
            placements.update((next(iter(context_regions)), zone) for zone in _p19_named_zones(context))
        for row in inventory:
            if not _p19_instance_row(row):
                continue
            spec = str(row.get("recommended_spec", ""))
            instance = re.search(_INSTANCE_TYPE, spec, re.I)
            cpu = re.search(_CPU_COUNT, spec, re.I)
            memory_values = _p19_memory_values(spec)
            memory = next(iter(memory_values)) if len(memory_values) == 1 else None
            pair = actual.get(instance[0]) if instance else None
            sizes.append(bool(pair and cpu and memory is not None and pair == (float(cpu[1]), memory)))
            mismatch = pair is not None and (
                cpu is not None and float(cpu[1]) != pair[0] or memory is not None and memory != pair[1]
            )
            specification_evidence.append(
                "mismatch" if mismatch else
                "unparseable_specification" if len(memory_values) > 1 else
                "specification_deferred_to_step2" if not (instance and cpu and memory is not None) else
                "missing_api_evidence" if pair is None else
                "verified"
            )
            quantities.append(isinstance(row.get("quantity"), int) and not isinstance(row["quantity"], bool)
                              and row["quantity"] > 0)
            family_match = re.search(_INSTANCE_FAMILY, spec, re.I)
            family = instance[0].split(".")[1] if instance else family_match[1].lower() if family_match else ""
            region_family_valid = len(context_regions) == 1 and any(
                region in context_regions and family in families for region, _, _, families in table
            )
            # Every declared placement must be supported; a valid VSwitch cannot
            # conceal an unsupported location explicitly written on an ECS row.
            placement_valid = region_family_valid and all(any(
                region == placed_region and zone in zones and family in families
                for region, _, zones, families in table
            ) for placed_region, zone in placements)
            declared_cpu = pair[0] if pair else float(cpu[1]) if cpu else None
            sku_valid = not instance or bool(re.fullmatch(r"(?:[1-9]\d*)?xlarge", instance[0].split(".")[-1]))
            supported.append(bool(declared_cpu is not None and declared_cpu >= 4 and sku_valid and placement_valid))
            placement_evidence.append(
                "unconfirmed_region_or_family" if not region_family_valid else
                "unsupported_zone" if not placement_valid else
                "unsupported_size" if not sku_valid or declared_cpu is not None and declared_cpu < 4 else
                "size_deferred_to_step2" if declared_cpu is None else
                "verified" if placements else "zone_deferred_to_step2"
            )
            disclosure = _p19_pending_tdx_disclosure(detail, row)
            if (placement_evidence[-1] == "unsupported_zone" and sku_valid
                    and (declared_cpu is None or declared_cpu >= 4) and disclosure):
                observations.append({
                    "kind": "tdx_support_pending_verification", "blocking": False,
                    "row_index": len(placement_evidence) - 1, "candidate_index": detail.get("candidate_index"),
                    "disclosure": disclosure, "raw_placement_evidence": placement_evidence[-1],
                })
    return {"checks": {
        "instance sizes verified by successful DescribeInstanceTypes": bool(sizes) and all(sizes),
        "declared TDX family, region and zone match verified support table": bool(supported) and all(supported),
        "instance quantities are explicit positive integers": bool(quantities) and all(quantities),
    }, "specification_evidence": specification_evidence or ["missing_ecs_inventory"],
        "placement_evidence": placement_evidence or ["missing_ecs_inventory"],
        "model_quality_observations": observations}


def p19_plan_facts(calls: list[dict], outcomes: dict[str, dict]) -> dict[str, bool]:
    return p19_plan_review(calls, outcomes)["checks"]


def p19_acceptance(plan: dict) -> dict[str, bool]:
    """Allow omissions/disclosed unverified zones; retain other failures and all raw facts."""
    checks = dict(plan["checks"])
    checks.pop("instance sizes verified by successful DescribeInstanceTypes")
    if "placement_evidence" in plan:
        pending = {o["row_index"] for o in plan.get("model_quality_observations", [])
                   if o["kind"] == "tdx_support_pending_verification" and o["blocking"] is False}
        key = "declared TDX family, region and zone match verified support table"
        if pending:
            checks.pop(key)
            key = "TDX placement verified, deferred or explicitly marked pending verification"
        checks[key] = bool(
            plan["placement_evidence"]
        ) and all(value in {"verified", "zone_deferred_to_step2", "size_deferred_to_step2"}
                  or value == "unsupported_zone" and index in pending
                  for index, value in enumerate(plan["placement_evidence"]))
    evidence = plan["specification_evidence"]
    checks["displayed sizes consistent with API evidence or deferred to Step2"] = bool(evidence) and all(
        value in {"verified", "missing_api_evidence", "specification_deferred_to_step2"} for value in evidence
    )
    return checks


def planning_phase(conclusion: dict, events: list[dict]) -> str:
    if conclusion.get("status") == "awaiting_selection":
        return "selection"
    if any(e.get("eventType") == "input_required" and e.get("data", {}).get("kind") == "ask_user_question"
           for e in events):
        return "clarification"
    return "other"


def clarification_conclusion(state: dict, events: list[dict]) -> dict:
    """Read retained candidates only from the current server-side question pause."""
    question = next((e for e in reversed(events) if e.get("eventType") == "input_required"
                     and e.get("data", {}).get("kind") == "ask_user_question"), {})
    snapshot = state.get("snapshot", {})
    pending = snapshot.get("pendingInput") or {}
    coordinate = question.get("step", {})
    if (not question.get("eventId") or not coordinate.get("runId")
            or coordinate.get("id") != "solution_planning_and_selection"
            or snapshot.get("status") != "waiting_input" or snapshot.get("normalHandoff")
            or pending.get("kind") != "ask_user_question" or pending.get("eventId") != question["eventId"]
            or pending.get("step") != coordinate):
        return {}
    step = next((s for s in snapshot.get("steps", []) if s.get("runId") == coordinate["runId"]
                 and s.get("id") == coordinate["id"] and s.get("status") == "waiting_input"), {})
    conclusion = step.get("conclusion")
    return conclusion if isinstance(conclusion, dict) else {}


def pipeline_turn_review(
    scenario: str, number: int, *, phase: str, had_candidate: bool, handoff: bool, unchanged: bool,
    prior_conclusion: dict | None = None, conclusion: dict | None = None, clarification: dict | None = None,
) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    observations: dict[str, Any] = {"planning_phase": phase}
    proceed = True
    if number == 0:
        checks["purchase reaches candidate selection or clarification"] = phase in {"selection", "clarification"}
        if scenario == "handoff" and not checks["purchase reaches candidate selection or clarification"]:
            observations["not_exercised"] = ["cancellation", "normal consultation"]
            proceed = False
    if scenario == "p19" and number == 1:
        if had_candidate:
            current = clarification if phase == "clarification" else conclusion
            checks["challenge preserves existing candidate"] = (
                not handoff and (phase == "clarification" or phase == "selection" and unchanged)
                and bool(prior_conclusion and prior_conclusion.get("candidate_set_id")
                         and prior_conclusion.get("candidates")) and current == prior_conclusion
            )
        else:
            observations["candidate_preservation"] = "not_exercised_no_prior_candidate"
    if scenario == "p19" and number in {2, 3}:
        if had_candidate:
            checks["side consultation keeps selection active"] = phase == "selection" and unchanged and not handoff
            checks["side consultation preserves candidate payload"] = bool(
                prior_conclusion and prior_conclusion.get("candidate_set_id") and prior_conclusion.get("candidates")
            ) and conclusion == prior_conclusion
        else:
            observations["candidate_preservation"] = "not_exercised_no_prior_candidate"
    if scenario == "handoff" and number == 1:
        checks["explicit cancellation hands off to normal"] = handoff
        if not handoff:
            observations["not_exercised"] = ["normal consultation", "normal followup"]
            proceed = False  # Do not mislabel further pipeline replies as normal-chat failures.
    return {"checks": checks, "observations": observations, "proceed": proceed}


def requires_skill_free_lookup(scenario: str, *, automatic: bool, number: int) -> bool:
    # The automatic normal scenario's third question is a cloud-pricing workflow.
    return scenario != "normal" or not automatic or number < 2


def is_knowledge_location(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith("knowledge://") or "/knowledge/" in normalized or normalized.endswith("/knowledge")


def final_text(path: Path) -> str:
    final = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        root = obj.get("result", obj)
        if not isinstance(root, dict):
            continue
        update = root.get("statusUpdate", {})
        if update.get("metadata", {}).get("iac_code", {}).get("assistantFinal", {}).get("complete"):
            final = "".join(part.get("text", "") for part in update.get("status", {}).get(
                "message", {}).get("parts", []) if isinstance(part.get("text"), str))
    return final


def _normal_has_wiki_body(config: Path, context_id: str, *, hardware: bool = False) -> bool:
    for path in (config / "projects").rglob("session.jsonl"):
        if "pipeline/transcripts" in path.as_posix() or path.parent.name != context_id:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            for node in walk(json.loads(line)):
                content = str(node.get("content", "")).replace("\\", "/").replace("knowledge://", "/knowledge/")
                content = content.replace("/references/cloud-products/", "/knowledge/clouds/aliyun/products/")
                if node.get("type") == "tool_result" and re.match(r"File: .*[/\\]knowledge[/\\]", content) and (
                    not hardware or "32 GB GDDR6" in content and "Xe2" in content
                ):
                    return True
    return False


def configure_permissions(config: Path) -> None:
    path = config / "settings.yml"
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings["permissions"] = {
        "mode": "default", "allow": ["read_file", "glob", "grep", "web_fetch", "web_search",
                                     "write_file", "edit_file"],
        "deny": ["bash", "ros_deploy", "ros_stack", "ros_stack_instances"],
    }
    path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config.resolve().is_relative_to(args.run_dir.resolve()):
        raise ValueError("Use isolated configuration through run_e2e.py")
    configure_permissions(config)
    auto = getattr(args, "wiki_auto_search", False)
    if auto:
        os.environ["IAC_CODE_WIKI_AUTO_SEARCH"] = "true"
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_ONLY"
    if args.scenario == "step2":
        from scripts.ci.run_step_followup_a2a import run as run_step_followup

        args.scenario = "step2"
        args.auto_approve_permissions = False
        if auto:
            args.initial_prompt = AUTO_PIPELINE
        report = run_step_followup(args)
        # Only calls made in the actual materialization step count as Step2 reads.
        ids = {
            str(event.get("data", {}).get("toolUseId"))
            for path in args.run_dir.glob("turn-*.events.jsonl") for event in envelopes(path)
            if event.get("eventType") == "tool_started"
            and event.get("step", {}).get("id") == "materialize_selected_candidate"
        }
        evidence = wiki_evidence([call for call in tool_inputs(config) if call["id"] in ids], results(config))
        report["wiki"] = evidence
        evidence["used"] = report["checks"].get("step2 real template, preview and quote") is True
        report["checks"]["actual Step2 Wiki source available"] = evidence["read"]
        if auto:
            records = [json.loads(line) for line in (args.run_dir / "telemetry/spans.jsonl").read_text().splitlines()]
            report["wiki_auto"] = auto_evidence(config, records)
            step2 = auto_evidence(config, [r for r in records if r.get("attributes", {}).get("step_id")
                                          == "materialize_selected_candidate"])
            evidence["excerpt_pages"] = sorted({page for r in step2["requests"]
                                                for page in r.get("excerpt_pages", [])})
            report["checks"]["actual Step2 Wiki source available"] = evidence["read"] or bool(evidence["excerpt_pages"])
            report["checks"].update(auto_checks(report["wiki_auto"]))
            report["checks"]["both knowledge steps retain serial and async scopes"] = (
                report["wiki_auto"]["serial_queries"] >= 2 and report["wiki_auto"]["async_queries"] >= 2
            )
        report["status"] = "passed" if all(report["checks"].values()) else "failed"
        return report
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    isolated_home = args.run_dir / "home"
    isolated_home.mkdir(mode=0o700, exist_ok=True)
    capture = ObserveCapture(args.run_dir / "telemetry").start()
    normal_mode = args.scenario in {"normal", "boundaries", "sizing"}
    env = {**os.environ, **capture.env, "PYTHONPATH": str(REPO / "src"),
           "IAC_CODE_MODE": "normal" if normal_mode else "pipeline",
           "IAC_CODE_PIPELINE_NAME": "selling_solution_first", "IAC_CODE_ROS_AGENT_AWARENESS": "true",
           "IAC_CODE_PROVIDER": args.provider, "IAC_CODE_MODEL": args.model,
           "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1", "IAC_CODE_CONFIG_BACKUP_DIR": str(args.run_dir / "backup"),
           "HOME": str(isolated_home), "USERPROFILE": str(isolated_home)}
    env["IAC_CODE_WIKI_AUTO_SEARCH"] = "true" if auto else "false"
    port = _free_port("127.0.0.1")
    url = f"http://127.0.0.1:{port}"
    allow_dry_run = getattr(args, "allow_ecs_dry_run", False)
    guard_audit = args.run_dir / "cloud-dispatch-guard.jsonl"
    server_args = ["a2a", "--config", str(args.run_dir / "a2a-server.yml")] if args.runtime_executable else None
    if allow_dry_run:
        if args.scenario != "p19" or args.runtime_executable:
            raise ValueError("ECS DryRun test permission requires source P19 with the dispatch guard")
        server_args = [str(REPO / "scripts/ci/ecs_dryrun_guard.py"), "--audit", str(guard_audit),
                       "--config", str(args.run_dir / "a2a-server.yml")]
    server = ManagedServer(
        python_cmd=[str(args.runtime_executable)] if args.runtime_executable else [args.python],
        config_path=_write_server_config(
            args.run_dir, host="127.0.0.1", port=port, auto_approve_permissions=False),
        process_cwd=str(REPO), allowed_cwd=str(workspace), env=env, log_prefix=args.run_dir / "server",
        run_mode="normal" if normal_mode else "pipeline",
        server_args=server_args,
    )
    turns, answers, turn_sources = [], [], {}
    checks = {}
    try:
        server.start()
        wait_for_server(url, timeout=60, owned_server=server)
        for repetition in range(args.repeat):
            context = task = ""
            has_candidate = False
            prior_conclusion = {}
            plan_reviewed = False
            conversation_api_calls = []
            if args.scenario == "sizing":
                prompts = (SIZING,)
            elif args.scenario == "boundaries":
                prompts = BOUNDARIES
            elif args.scenario == "p19":
                prompts = P19
            elif args.scenario == "normal":
                prompts = AUTO_NORMAL if auto else (NORMAL,)
            else:
                prompts = (P19[0], CANCEL, NORMAL, "B70 适合哪些工作负载？") if auto else (P19[0], CANCEL, NORMAL)
            handoff_seen = False
            for number, prompt in enumerate(prompts):
                if args.scenario == "boundaries":
                    context = task = ""  # Domain-boundary questions are independent contexts.
                before = {call["id"] for call in tool_inputs(config)}
                name = f"repeat-{repetition + 1}-turn-{number + 1}"
                no_inherited_body = not _normal_has_wiki_body(config, context)
                prior_normal_hardware = _normal_has_wiki_body(config, context, hardware=True)
                started = time.monotonic()
                request_started_ns = time.time_ns()
                timings = {}

                def observe(event):
                    elapsed = round(time.monotonic() - started, 3)
                    timings.setdefault("first_event_seconds", elapsed)
                    if any(node.get("wikiSearch") or node.get("eventType") == "wiki_search" for node in walk(event)):
                        timings.setdefault("first_wiki_event_seconds", elapsed)

                response = stream_message(
                    server_url=url, cwd=str(workspace), prompt=prompt, context_id=context, task_id=task,
                    name=name, run_dir=args.run_dir, timeout=args.timeout, redaction_env=env,
                    on_event=observe if auto else None,
                )
                from scripts.ci.a2a_context_evidence import finish_permissions

                response = finish_permissions(response, name=name, run_dir=args.run_dir, config=config,
                                              server_url=url, cwd=str(workspace), timeout=args.timeout, env=env,
                                              allow_ecs_dry_run=allow_dry_run)
                request_finished_ns = time.time_ns()
                context, task = response.context_id, response.task_id
                path = args.run_dir / (name + ".events.jsonl")
                calls = [call for call in tool_inputs(config) if call["id"] not in before]
                outcomes = results(config)
                evidence = wiki_evidence(calls, outcomes)
                # Body availability and reuse are checked against the actual current provider requests.
                hardware_available = prior_normal_hardware or evidence["hardware_body_read"]
                events = envelopes(path)
                completed = [event for event in events if event.get("eventType") == "step_completed"]
                conclusion = next((event.get("data", {}).get("conclusion") for event in reversed(completed)
                                   if isinstance(event.get("data", {}).get("conclusion"), dict)), {})
                answer = visible_pipeline_text(path) or final_text(path) or response.text
                cloud_calls = [call for call in calls if call["name"] == "aliyun_api"]
                item = {"name": name, "seconds": round(time.monotonic() - started, 2), **evidence,
                        **timings,
                        "state": response.last_status_state, "handoff": response.normal_handoff_ready,
                        "conclusion_unchanged": any(e.get("data", {}).get("conclusion_unchanged") is True
                                                    for e in completed),
                        "step_status": conclusion.get("status"),
                        "cloud_successes": sum(outcomes.get(c["id"], {}).get("is_error") is False for c in cloud_calls),
                        "cloud_operations": sorted({str(c["input"].get("action", "")) for c in cloud_calls}),
                        "tool_errors": sum(outcomes.get(c["id"], {}).get("is_error") is True for c in calls),
                        "skill_loaded": any(c["name"] == "skill" for c in calls),
                        "tools": [call["name"] for call in calls]}
                turns.append(item)
                answers.append({"name": name, "answer": answer})
                checks[name + ": real HTTP/SSE and completed turn"] = (
                    response.response_content_type == "text/event-stream"
                    and response.last_status_state in {"TASK_STATE_COMPLETED", "TASK_STATE_INPUT_REQUIRED"}
                )
                checks[name + ": no cloud writes"] = (
                    all(str(c["input"].get("action", "")).startswith(READ_PREFIXES)
                        or allow_dry_run and explicit_ecs_dry_run(c) for c in cloud_calls)
                    and not any(c["name"] in {"ros_deploy", "ros_stack", "ros_stack_instances"} for c in calls)
                )
                gpu_turn = args.scenario == "normal" and (not auto or number == 0) or (
                    args.scenario == "handoff" and number == 2
                ) or (
                    args.scenario == "p19" and number == 3
                )
                turn_sources[name] = {
                    "window": (request_started_ns, request_finished_ns), "gpu": gpu_turn,
                    "prior_hardware": prior_normal_hardware,
                    "planning": not normal_mode and number == 0,
                    "sizing": args.scenario == "sizing", "handoff": args.scenario == "handoff" and number == 2,
                    "fresh": handoff_seen and no_inherited_body,
                }
                if gpu_turn:
                    confirmation = args.scenario == "p19"
                    item["used"] = b70_identity_confirmed(answer) if confirmation else b70_used_correctly(answer)
                    item["hardware_body_available"] = hardware_available
                    if confirmation:
                        checks[name + ": B70 GPU identity confirmed"] = item["used"]
                    else:
                        checks[name + ": actual B70 body available"] = hardware_available
                        checks[name + ": B70 facts used correctly"] = item["used"]
                    if args.runtime_executable:
                        root = str(args.runtime_executable.resolve().parent)
                        checks[name + ": frozen package Wiki body read"] = any(
                            call["name"] == "read_file"
                            and outcomes.get(call["id"], {}).get("is_error") is False
                            and str(outcomes.get(call["id"], {}).get("content", "")).startswith("File: " + root)
                            for call in calls
                        )
                if normal_mode:
                    if requires_skill_free_lookup(args.scenario, automatic=auto, number=number):
                        checks[name + ": no Skill loaded"] = not item["skill_loaded"]
                    checks[name + ": actual normal mode"] = (
                        not events and not response.normal_handoff_ready
                        and not any(c["name"] in {"complete_step", "ask_user_question"} for c in calls)
                    )
                    if args.scenario == "sizing":
                        item["sizing_page_read"] = "clouds/aliyun/products/ecs/sizing.md" in evidence["pages"]
                        checks[name + ": actual ECS Wiki source available"] = any(
                            page.startswith("clouds/aliyun/products/ecs/") for page in evidence["pages"]
                        )
                    elif args.scenario == "boundaries" and number == 0:
                        missed = any(
                            c["name"] == "grep" and is_knowledge_location(str(c["input"].get("path", "")))
                            and outcomes.get(c["id"], {}).get("is_error") is False
                            and outcomes.get(c["id"], {}).get("content", "").strip() == "No matches"
                            for c in calls
                        )
                        fallback = any(
                            c["name"] in {"web_fetch", "web_search", "aliyun_doc_search"} for c in calls
                        ) or bool(re.search(r"无法.{0,8}(?:核实|确认)|未能.{0,8}查证", answer))
                        item["search_miss"] = missed
                        item["fallback_or_uncertainty"] = fallback
                        checks[name + ": actual Wiki search miss observed"] = missed
                        checks[name + ": Wiki miss still receives an answer"] = bool(answer.strip())
                    elif args.scenario == "boundaries":
                        checks[name + ": unrelated request avoids Wiki tools"] = not any(
                            c["name"] in {"read_file", "glob", "grep"} for c in calls
                        )
                        checks[name + ": unrelated request answered"] = bool(re.search(r"happy birthday", answer, re.I))
                proceed = True
                if not normal_mode:
                    phase = planning_phase(conclusion, events)
                    clarification = {}
                    if (args.scenario == "p19" and number == 1 and has_candidate and phase == "clarification"
                            and response.last_status_state == "TASK_STATE_INPUT_REQUIRED"):
                        state = fetch_pipeline_state(
                            server_url=url, context_id=context, task_id=task,
                            run_dir=args.run_dir, name=name, redaction_env=env,
                        )
                        clarification = clarification_conclusion(state, events)
                    review = pipeline_turn_review(
                        args.scenario, number, phase=phase, had_candidate=has_candidate,
                        handoff=response.normal_handoff_ready, unchanged=item["conclusion_unchanged"],
                        prior_conclusion=prior_conclusion, conclusion=conclusion, clarification=clarification,
                    )
                    checks.update({name + ": " + key: value for key, value in review["checks"].items()})
                    item["runner_observations"] = review["observations"]
                    proceed = review["proceed"]
                    if number == 0:
                        checks[name + ": planning Wiki source available"] = evidence["read"]
                    conversation_api_calls.extend(cloud_calls)
                    details = [c for c in calls if c["name"] == "show_candidate_detail"
                               and outcomes.get(c["id"], {}).get("is_error") is False]
                    if args.scenario == "p19":
                        if details or phase == "selection" and not has_candidate and not plan_reviewed:
                            plan = p19_plan_review(conversation_api_calls + details, outcomes, config=config)
                            plan_reviewed = True
                            item["plan_facts"] = plan["checks"]
                            item["specification_evidence"] = plan["specification_evidence"]
                            item["placement_evidence"] = plan["placement_evidence"]
                            if plan["model_quality_observations"]:
                                item["runner_observations"]["model_quality"] = plan["model_quality_observations"]
                            checks.update({name + ": " + key: value for key, value in p19_acceptance(plan).items()})
                        elif not has_candidate:
                            item["runner_observations"]["plan_verification"] = "not_exercised_no_candidate"
                    has_candidate = phase == "selection" or has_candidate and not response.normal_handoff_ready
                    if phase == "selection":
                        prior_conclusion = conclusion
                if args.scenario == "handoff" and number == 1:
                    handoff_seen = response.normal_handoff_ready
                if args.scenario == "handoff" and number == 2:
                    checks[name + ": fresh normal source after handoff"] = (
                        handoff_seen and no_inherited_body and evidence["read"]
                    )
                print(json.dumps({"scenario": args.scenario, "model": args.model,
                                  **{k: item[k] for k in ("name", "state", "read", "handoff", "seconds")}}), flush=True)
                if response.last_status_state == "TASK_STATE_COMPLETED" or response.normal_handoff_ready:
                    task = ""
                if response.last_status_state not in {"TASK_STATE_COMPLETED", "TASK_STATE_INPUT_REQUIRED"}:
                    raise RuntimeError("TurnDidNotComplete")
                if not proceed:
                    break
    except Exception as exc:
        checks["scenario completed"] = False
        turns.append({"error_type": type(exc).__name__})
    finally:
        server.terminate()
        records = capture.stop()
        automatic = auto_evidence(config, records) if auto else None
        if automatic is not None:
            for item in turns:
                info = turn_sources.get(item.get("name"))
                if info is None:
                    continue
                start, end = info["window"]
                observed = auto_evidence(config, [r for r in records
                                                 if start <= r.get("timestamp_unix_nano", 0) <= end])
                item["excerpt_pages"] = sorted({page for r in observed["requests"]
                                                for page in r.get("excerpt_pages", [])})
                item["hardware_excerpt"] = any(r.get("hardware_excerpt") for r in observed["requests"])
                source = item["read"] or bool(item["excerpt_pages"])
                name = item["name"]
                if info["planning"]:
                    checks[name + ": planning Wiki source available"] = source
                if info["sizing"]:
                    checks[name + ": actual ECS Wiki source available"] = any(
                        page.startswith("clouds/aliyun/products/ecs/")
                        for page in item["pages"] + item["excerpt_pages"])
                if info["gpu"]:
                    item["hardware_body_available"] = bool(
                        info["prior_hardware"] or item["hardware_body_read"] or item["hardware_excerpt"])
                    # Keep source visibility as evidence for P19's acknowledgement;
                    # explicit hardware questions still require a real Wiki source.
                    if args.scenario != "p19":
                        checks[name + ": actual B70 body available"] = item["hardware_body_available"]
                if info["handoff"]:
                    checks[name + ": fresh normal source after handoff"] = info["fresh"] and source
            checks.update(auto_checks(automatic))
            if args.scenario == "handoff":
                normal_queries = [q for q in automatic["queries"] if str(q.get("scope_id", "")).endswith(":normal")]
                if normal_queries:
                    checks["handoff normal first input is serial"] = normal_queries[0]["dispatch_mode"] == "serial"
                    checks["handoff normal followup is async"] = all(
                        q["dispatch_mode"] == "async" for q in normal_queries[1:]
                    )
        checks["server stopped"] = server.process is not None and server.process.poll() is not None
        if allow_dry_run:
            audit = [json.loads(line) for line in guard_audit.read_text().splitlines()] if guard_audit.exists() else []
            checks["wire dispatch guard installed in actual server"] = any(
                row.get("event") == "guard_installed" and row.get("pid") == server.process.pid for row in audit
            )
            checks["only reads and explicit ECS DryRun reached dispatch"] = all(
                row.get("kind") in {"read", "ecs_dry_run"} for row in audit if row.get("event") == "dispatch"
            )
        observed = sorted({str(r.get("attributes", {}).get("gen_ai.request.model")) for r in records
                           if r.get("attributes", {}).get("gen_ai.request.model")})
        checks["only requested real model ran"] = observed == [args.model]
        for filename in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
            (config / filename).unlink(missing_ok=True)
        checks["credential copies removed"] = not any((config / name).exists() for name in (
            ".credentials.yml", ".cloud-credentials.yml", "settings.yml"))
        (args.run_dir / "answers.json").write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "passed" if all(checks.values()) else "failed", "scenario": args.scenario,
            "model": args.model, "effort": "low", "fallback": False, "awareness": True,
            "scenario_retries": 0, "checks": checks, "turns": turns, "cleanup_status": "not-needed",
            **({"wiki_auto": automatic} if automatic is not None else {}),
            "manual_review_fields": ["explanatory CPU-generation claims", "global cloud availability assertions",
                                     "unparsed placement or contradictory prose",
                                     "economic/burstable CPU-credit claims"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--scenario", choices=("normal", "handoff", "p19", "step2", "boundaries", "sizing"), required=True,
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--wiki-auto-search", action="store_true")
    parser.add_argument("--allow-ecs-dry-run", action="store_true", help="P19 only; require wire-level DryRun=true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--runtime-executable", type=Path, help="Built native Skill Runtime; normal scenario only")
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--allow-real-llm", action="store_true")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    if not (args.allow_real_cloud or args.allow_real_llm) or not 1 <= args.repeat <= 5:
        parser.error("Explicit live consent and 1–5 repetitions are required")
    if args.runtime_executable and args.scenario != "normal":
        parser.error("Frozen package smoke uses the normal scenario")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    home = args.run_dir / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    os.environ.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    try:
        report = run(args)
    except Exception as exc:
        report = {"status": "failed", "error_type": type(exc).__name__, "cleanup_status": "not-needed"}
    finally:
        config = Path(os.environ["IAC_CODE_CONFIG_DIR"])
        if config.resolve().is_relative_to(args.run_dir.resolve()):
            for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
                (config / name).unlink(missing_ok=True)
    (args.run_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "turns"}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
