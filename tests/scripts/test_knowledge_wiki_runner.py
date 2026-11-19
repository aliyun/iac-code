"""Offline checks of evidence extraction and live case isolation, without model/network access."""

from __future__ import annotations

import json
from argparse import Namespace
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.ci import run_e2e
from scripts.ci.run_knowledge_wiki_a2a import (
    P19,
    _normal_has_wiki_body,
    auto_evidence,
    b70_identity_confirmed,
    b70_used_correctly,
    clarification_conclusion,
    p19_plan_facts,
    p19_plan_review,
    pipeline_turn_review,
    planning_phase,
    requires_skill_free_lookup,
    wiki_evidence,
)


def test_injected_excerpts_need_matching_source_text_in_an_actual_main_request(tmp_path):
    from iac_code.services.token_counter import TokenCounter
    from iac_code.services.wiki_auto_search import reference

    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "gpu.md").touch()
    snapshot = {"query_id": "current", "status": "ready", "keywords": ["B70"], "hits": [
        {"path": "gpu.md", "title": "GPU", "matched_terms": ["B70"], "hit_lines": [7],
         "lines": [{"number": 7, "score": 0.9, "text": "**B70**: Xe2, 32 GB **GDDR6**"}]},
    ]}
    project = tmp_path / "projects/session"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text(json.dumps({"metadata": {"wiki_auto_search": snapshot}}) + "\n",
                                         encoding="utf-8")
    block = reference(snapshot, root, counter=TokenCounter(), max_tokens=1200)

    def request(content, system="main"):
        return {"kind": "span", "attributes": {"gen_ai.span.kind": "LLM", "gen_ai.system_instructions": system,
                "gen_ai.input.messages": json.dumps([{"role": "user", "content": [{"text": content}]}])}}

    assert auto_evidence(tmp_path, [])["requests"] == []
    observed = auto_evidence(tmp_path, [request(block)])["requests"][0]
    assert observed["excerpt_pages"] == ["gpu.md"] and observed["hardware_excerpt"]
    missing = block.replace("32 GB", "not the recorded source")
    assert not auto_evidence(tmp_path, [request(missing)])["requests"][0]["excerpt_pages"]
    assert not auto_evidence(tmp_path, [request(block, "Wiki keyword extraction")])["requests"][0]["excerpt_pages"]
    assert not auto_evidence(tmp_path, [request(str(root / "gpu.md"))])["requests"][0]["excerpt_pages"]


def test_wiki_read_evidence_requires_successful_actual_body():
    calls = [{"id": "read", "name": "read_file", "input": {"path": "/app/knowledge/common/b70.md"}}]
    assert not wiki_evidence(calls, {})["read"]
    assert not wiki_evidence(calls, {"read": {"is_error": True, "content": "File not found"}})["read"]
    assert not wiki_evidence(calls, {"read": {"is_error": False, "content": "No matches"}})["read"]
    result = wiki_evidence(calls, {"read": {"is_error": False, "content": (
        "File: C:\\app\\knowledge\\common\\b70.md (10 lines)\nIntel B70: Xe2, 32 GB GDDR6"
    )}})
    assert result == {"found": True, "read": True, "pages": ["common/b70.md"], "hardware_body_read": True}
    assert b70_used_correctly("Intel Arc Pro B70：Xe2 / Battlemage，32 GB GDDR6。")
    assert not b70_used_correctly("B70 不存在，是 Flex，16 GB。")
    assert not b70_used_correctly("Intel Arc Pro B70 是 Xe2 / Battlemage，配备 32 GB HBM2 显存。")


@pytest.mark.parametrize("answer, expected", [
    ("Intel B70 是一张显卡（GPU），与 TDX 无关。", True),
    ("是的，Intel Arc Pro B70 是 Battlemage 架构的显卡。", True),
    ("Intel Arc Pro B70 是 Xe2/Battlemage 架构的 GPU。", True),
    ("B70 不是显卡。", False),
    ("B70 并非 GPU。", False),
    ("B70 不存在，不是 Intel 显卡。", False),
    ("Intel Arc Pro B70 是一张显卡。但阿里云的 B70 实例不存在。", True),
    ("B70 是 Intel 显卡，但 B70 连目录都不存在，不能确认上云可买。", True),
    ("B70 是 CPU。", False),
    ("请选择 TDX 方案。", False),
    ("", False),
])
def test_p19_gpu_acknowledgement_does_not_require_repeated_vram(answer, expected):
    assert b70_identity_confirmed(answer) is expected
    if expected:
        assert not b70_used_correctly(answer)  # The explicit specification case remains strict.


def test_p19_accepts_deferred_or_missing_api_evidence_but_not_contradictory_sizes():
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    plan = {"checks": {
        "instance sizes verified by successful DescribeInstanceTypes": False,
        "declared TDX family, region and zone match verified support table": True,
    }, "specification_evidence": ["missing_api_evidence"]}
    assert all(p19_acceptance(plan).values())
    plan["specification_evidence"] = ["specification_deferred_to_step2"]
    assert all(p19_acceptance(plan).values())
    for evidence in ("mismatch", "unparseable_specification"):
        plan["specification_evidence"] = [evidence]
        assert not all(p19_acceptance(plan).values())
    plan["specification_evidence"] = ["missing_api_evidence"]
    plan["checks"]["declared TDX family, region and zone match verified support table"] = False
    assert not all(p19_acceptance(plan).values())



def test_installed_product_alias_counts_as_successful_wiki_body():
    calls = [{"id": "read", "name": "read_file", "input": {
        "path": "references/cloud-products/ecs/ecs.md",
    }}]
    outcomes = {"read": {"is_error": False, "content": (
        "File: C:\\app\\pipeline\\selling\\skills\\iac-aliyun-cost\\references\\cloud-products\\ecs\\ecs.md "
        "(64 lines)\n## 镜像选择\nUbuntu 24.04"
    )}}
    result = wiki_evidence(calls, outcomes)
    assert result["read"] and result["pages"] == ["clouds/aliyun/products/ecs/ecs.md"]
    outcomes["read"]["content"] = "File: knowledge://clouds/aliyun/products/ecs/ecs.md (64 lines)\n## 镜像选择"
    assert wiki_evidence(calls, outcomes)["pages"] == result["pages"]


@pytest.mark.parametrize("source", [
    "/app/knowledge/b70.md", "/app/skill/references/cloud-products/ecs/ecs.md", "knowledge://common/b70.md",
])
def test_handoff_inheritance_check_ignores_other_sessions_and_pipeline_transcripts(tmp_path, source):
    body = {"type": "tool_result", "content": f"File: {source} (10 lines)\nbody"}
    for name in ("prior/session.jsonl", "current/pipeline/transcripts/attempt/session.jsonl"):
        path = tmp_path / "projects" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body) + "\n", encoding="utf-8")
    assert not _normal_has_wiki_body(tmp_path, "current")
    path = tmp_path / "projects/current/session.jsonl"
    path.write_text(json.dumps(body) + "\n", encoding="utf-8")
    assert _normal_has_wiki_body(tmp_path, "current")


def test_live_wiki_catalog_and_supported_p19_prompts():
    cases = {case.name: case for case in run_e2e.LIVE_CASES if case.name.startswith("knowledge-wiki-")}
    assert set(cases) == {f"knowledge-wiki-{s}-a2a" for s in ("normal", "handoff", "p19", "step2", "boundaries")}
    assert cases["knowledge-wiki-boundaries-a2a"].live_runner in run_e2e.LLM_ONLY_RUNNERS
    assert all(case.group == "readonly" and not case.cloud_write and not case.multimodal for case in cases.values())
    normal = cases["knowledge-wiki-normal-a2a"]
    assert normal.live_runner in run_e2e.LLM_ONLY_RUNNERS
    assert ("--repeat", "1") == normal.args[-2:]
    assert ("--repeat", "1") == cases["knowledge-wiki-handoff-a2a"].args[-2:]
    assert all(run_e2e._live_cleanup_status(case, None) == "not-needed" for case in cases.values())
    assert all((run_e2e.REPO_ROOT / case.script).is_file() for case in cases.values())
    assert P19 == (
        "我要购买按量付费的tdx，可信计算服务器",
        "TDX 机密虚拟机（CVM）\tTDVM、TDX Module quote、DCAP\tg8i/c8i/r8i（+部分地域 g9i 系）\t"
        "Sapphire Rapids（四代至强）。你推荐的是sgx机型？",
        "阿里云有没有B70 intel的服务器？", "B70是一款intel显卡",
    )


def test_p19_rejects_sgx_wrong_sizes_and_missing_real_api_evidence():
    row = {"product": "ECS", "quantity": 1, "recommended_spec": "ecs.r9i.xlarge，4 vCPU / 32 GiB"}
    calls = [
        {"id": "api", "name": "aliyun_api", "input": {"action": "DescribeInstanceTypes"}},
        {"id": "detail", "name": "show_candidate_detail", "input": {"resource_inventory": [
            row, {"product": "VSwitch", "recommended_spec": "cn-hangzhou 可用区 J"},
        ]}},
    ]
    outcomes = {"api": {"is_error": False, "content": json.dumps({"InstanceTypes": [{
        "InstanceTypeId": "ecs.r9i.xlarge", "CpuCoreCount": 4, "MemorySize": 32,
    }]})}, "detail": {"is_error": False, "content": "OK"}}
    assert all(p19_plan_facts(calls, outcomes).values())
    outcomes["api"]["is_error"] = True
    missing = p19_plan_review(calls, outcomes)
    assert not missing["checks"]["instance sizes verified by successful DescribeInstanceTypes"]
    assert missing["specification_evidence"] == ["missing_api_evidence"]
    assert missing["checks"]["declared TDX family, region and zone match verified support table"]
    outcomes["api"]["is_error"] = False
    row["recommended_spec"] = "ecs.r9i.xlarge，8 vCPU / 64 GiB"
    mismatch = p19_plan_review(calls, outcomes)
    assert not mismatch["checks"]["instance sizes verified by successful DescribeInstanceTypes"]
    assert mismatch["specification_evidence"] == ["mismatch"]
    row["recommended_spec"] = "ecs.r9it.xlarge，4 vCPU / 32 GiB"
    assert not p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]
    row["recommended_spec"] = "ecs.r9i.xlarge，4 vCPU / 32 GiB，cn-hangzhou-k"
    assert not p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]
    row["recommended_spec"] = "ecs.r9i.xlarge，4 vCPU / 32 GiB，杭州可用区 K"
    assert not p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]
    row["recommended_spec"] = "ecs.r9i.xlarge，4 vCPU / 32 GiB，杭州可用区 I"
    calls[1]["input"]["resource_inventory"][1]["recommended_spec"] = "cn-beijing-i"
    assert not p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]
    outcomes["api"]["is_error"] = True
    assert not p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]
    row["recommended_spec"] = "ecs.r9i.xlarge，4 vCPU / 32 GiB"
    calls[1]["input"]["resource_inventory"][1]["recommended_spec"] = "与 ECS 同一可用区"
    calls[1]["input"]["cost_assumptions"] = ["按 cn-hangzhou-j 区域估价，部署前确认库存"]
    assert p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]
    row["recommended_spec"] += "，cn-hangzhou-k"
    assert not p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]


@pytest.fixture
def p19_evidence():
    row = {"product": "ECS", "quantity": 1, "resource_type": "ALIYUN::ECS::Instance",
           "recommended_spec": "ecs.r9i.xlarge，4 vCPU / 32 GiB"}
    calls = [
        {"id": "types", "name": "aliyun_api", "input": {"action": "DescribeInstanceTypes"}},
        {"id": "detail", "name": "show_candidate_detail", "input": {"resource_inventory": [
            row, {"product": "VSwitch", "recommended_spec": "cn-hangzhou 可用区 J"},
        ]}},
    ]
    outcomes = {"types": {"is_error": False, "content": json.dumps({"InstanceTypes": [{
        "InstanceTypeId": "ecs.r9i.xlarge", "CpuCoreCount": 4, "MemorySize": 32,
    }]})}, "detail": {"is_error": False, "content": "OK"}}
    return calls, outcomes, row


@pytest.mark.parametrize("spec", [
    "X86 架构、UEFI，如杭州 R9I 系列兼容规格；最终规格以库存和询价为准",
    "ecs.r9i，具体规格在 Step2 确认",
    "ecs.r9i.xlarge，核数和内存待确认",
    "ecs.r9i.xlarge，4 vCPU，内存待确认",
    "ecs.r9i.xlarge，32 GiB 内存，核数待确认",
])
def test_p19_step1_may_defer_sku_cpu_and_memory_to_step2(p19_evidence, spec):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, row = p19_evidence
    row["recommended_spec"] = spec
    review = p19_plan_review(calls, outcomes)
    assert review["specification_evidence"] == ["specification_deferred_to_step2"]
    assert all(p19_acceptance(review).values())
    assert not review["checks"]["instance sizes verified by successful DescribeInstanceTypes"]


@pytest.mark.parametrize("spec", [
    "ecs.r9i.xlarge，8 vCPU，内存待确认",
    "ecs.r9i.xlarge，64 GiB 内存，核数待确认",
    "ecs.r9i.large，核数和内存待确认",
    "ecs.r9it.xlarge，核数和内存待确认",
    "R9I 系列，杭州可用区 K，核数和内存待确认",
])
def test_p19_deferred_fields_do_not_hide_explicit_size_or_tdx_errors(p19_evidence, spec):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, row = p19_evidence
    row["recommended_spec"] = spec
    assert not all(p19_acceptance(p19_plan_review(calls, outcomes)).values())


@pytest.mark.parametrize("location, expected", [
    ("杭州 J", True),
    ("杭州J", True),
    ("杭州 J 可用区", True),
    ("杭州可用区J", True),
    ("华东1（杭州）可用区J", True),
    ("cn-hangzhou J 可用区", True),
    ("CN-HANGZHOU-J", True),
    ("cn-hangzhou，可用区 J", True),
    ("cn-hangzhou，J可用区交换机", True),
    ("cn-hangzhou，可用区J交换机", True),
    ("北京 I", True),
    ("杭州 K", False),
    ("杭州可用区I", False),
    ("北京 L", False),
    ("cn-shenzhen-a", False),
    ("ap-northeast-1-a", False),
    ("杭州 J，北京 I", False),
    ("杭州", True),
    ("J 可用区", False),
    ("", False),
])
def test_p19_tdx_location_spellings_preserve_supported_scope(p19_evidence, location, expected):
    calls, outcomes, _ = p19_evidence
    calls[1]["input"]["resource_inventory"][1]["recommended_spec"] = location
    facts = p19_plan_facts(calls, outcomes)
    assert facts["declared TDX family, region and zone match verified support table"] is expected
    assert facts["instance sizes verified by successful DescribeInstanceTypes"]


@pytest.mark.parametrize("location, expected, evidence", [
    ("在 cn-hangzhou 支持 TDX 的可用区", True, "zone_deferred_to_step2"),
    ("杭州", True, "zone_deferred_to_step2"),
    ("cn-hangzhou-j", True, "verified"),
    ("cn-hangzhou-k", False, "unsupported_zone"),
    ("北京 L", False, "unsupported_zone"),
    ("cn-shenzhen", False, "unconfirmed_region_or_family"),
    ("J 可用区", False, "unconfirmed_region_or_family"),
])
def test_p19_distinguishes_deferred_zones_from_explicit_wrong_placements(p19_evidence, location, expected, evidence):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, _ = p19_evidence
    calls[1]["input"]["resource_inventory"][1]["recommended_spec"] = location
    review = p19_plan_review(calls, outcomes)
    assert all(p19_acceptance(review).values()) is expected
    assert review["placement_evidence"] == [evidence]


@pytest.mark.parametrize("field", ["candidate_name", "purpose", "cost_assumptions", "decision_notes"])
@pytest.mark.parametrize("disclosure", [
    "TDX待核实", "TDX 支持待实测验证", "TDX support pending verification",
    "**TDX** 支持：尚未核实", "需进一步验证 TDX 支持情况", "TDX support is unverified",
])
def test_p19_explicit_pending_tdx_is_nonblocking_without_rewriting_raw_facts(p19_evidence, field, disclosure):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, row = p19_evidence
    detail = calls[1]["input"]
    detail["resource_inventory"][1]["recommended_spec"] = "cn-hangzhou-k"
    if field == "purpose":
        row[field] = disclosure
    else:
        detail[field] = {"risks": [disclosure]} if field == "decision_notes" else (
            [disclosure] if field == "cost_assumptions" else disclosure
        )
    plan = p19_plan_review(calls, outcomes)
    before = deepcopy(plan)
    assert not plan["checks"]["declared TDX family, region and zone match verified support table"]
    assert plan["placement_evidence"] == ["unsupported_zone"]
    assert all(p19_acceptance(plan).values())
    assert plan == before
    assert plan["model_quality_observations"][0]["blocking"] is False
    assert plan["model_quality_observations"][0]["row_index"] == 0
    assert "declared TDX family, region and zone match verified support table" not in p19_acceptance(plan)


@pytest.mark.parametrize("disclosure", [
    "", "以实际创建结果为准", "库存待核实，支持 TDX", "TDX 已支持，库存待验证", "TDX 镜像待验证",
    "TDX 模块 quote 待验证", "TDX API 参数待核实", "TDX 支持无需核实", "TDX support verified",
    "TDX待核实；TDX 已确认支持", "TDX待验证；官方已确认支持 TDX", "无需核实 TDX 支持情况",
])
def test_p19_vague_or_contradictory_caveats_do_not_exempt_unverified_zone(p19_evidence, disclosure):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, row = p19_evidence
    calls[1]["input"]["resource_inventory"][1]["recommended_spec"] = "杭州 K"
    row["purpose"] = disclosure
    plan = p19_plan_review(calls, outcomes)
    assert not all(p19_acceptance(plan).values())
    assert plan["model_quality_observations"] == []


@pytest.mark.parametrize("defect", ["quantity", "cpu", "sku", "minimum_size", "family", "region"])
def test_p19_pending_tdx_does_not_exempt_other_failures(p19_evidence, defect):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, row = p19_evidence
    detail = calls[1]["input"]
    detail["resource_inventory"][1]["recommended_spec"] = "杭州 K"
    row["purpose"] = "TDX 支持待实测验证"
    if defect == "quantity":
        row["quantity"] = 0
    elif defect == "cpu":
        row["recommended_spec"] = "ecs.r9i.xlarge，8 vCPU / 64 GiB"
    elif defect == "sku":
        row["recommended_spec"] = "ecs.r9i.large，核数和内存待实现步骤确定"
    elif defect == "minimum_size":
        row["recommended_spec"] = "ecs.r9i.xlarge，2 vCPU / 32 GiB"
    elif defect == "family":
        row["recommended_spec"] = "ecs.r9it.xlarge，4 vCPU / 32 GiB"
    else:
        detail["resource_inventory"][1]["recommended_spec"] = "cn-shenzhen-a"
    assert not all(p19_acceptance(p19_plan_review(calls, outcomes)).values())


def test_p19_pending_disclosure_cannot_be_borrowed_from_another_candidate(p19_evidence):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, row = p19_evidence
    detail = calls[1]["input"]
    detail["candidate_index"] = 0
    detail["resource_inventory"][1]["recommended_spec"] = "杭州 K"
    detail["candidate_name"] = "TDX待核实方案"
    other = deepcopy(calls[1])
    other["id"] = "another-detail"
    other["input"]["candidate_index"] = 1
    other["input"]["candidate_name"] = "TDX现货方案"
    calls.append(other)
    outcomes["another-detail"] = {"is_error": False, "content": "OK"}
    plan = p19_plan_review(calls, outcomes)
    assert plan["placement_evidence"] == ["unsupported_zone", "unsupported_zone"]
    assert [o["row_index"] for o in plan["model_quality_observations"]] == [0]
    assert not all(p19_acceptance(plan).values())


@pytest.mark.parametrize("family", ["g8i", "r9it"])
def test_p19_missing_zone_does_not_allow_an_unsupported_region_family(p19_evidence, family):
    calls, outcomes, row = p19_evidence
    row["recommended_spec"] = f"ecs.{family}.xlarge，4 vCPU / 32 GiB"
    calls[1]["input"]["resource_inventory"][1]["recommended_spec"] = "cn-hangzhou，具体可用区待实现步骤求解"
    review = p19_plan_review(calls, outcomes)
    assert not review["checks"]["declared TDX family, region and zone match verified support table"]
    assert review["placement_evidence"] == ["unconfirmed_region_or_family"]


def test_p19_step1_may_defer_both_zone_and_instance_sizes(p19_evidence):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, row = p19_evidence
    row["recommended_spec"] = "ecs.r9i.xlarge，规格待确认"
    calls[1]["input"]["resource_inventory"][1]["recommended_spec"] = "cn-hangzhou，支持 TDX 的可用区"
    review = p19_plan_review(calls, outcomes)
    assert review["placement_evidence"] == ["zone_deferred_to_step2"]
    assert review["specification_evidence"] == ["specification_deferred_to_step2"]
    assert all(p19_acceptance(review).values())


def test_p19_tdx_uses_selected_deployment_instead_of_discarded_region_in_cost_commentary(p19_evidence):
    calls, outcomes, row = p19_evidence
    row["recommended_spec"] += "，cn-beijing-i"
    detail = calls[1]["input"]
    detail["resource_inventory"][1]["recommended_spec"] = "北京 I"
    detail["cost_assumptions"] = [
        "地域 cn-beijing、可用区 cn-beijing-i（杭州可用区 J 的 ecs.r9i.xlarge 当前 ClosedWithStock，无法新购）",
        "按量付费，月按 720 小时估算",
    ]
    assert all(p19_plan_facts(calls, outcomes).values())


@pytest.mark.parametrize("node_id", ["edge-hz-j", "vpc-sg-a", "sg-hz-j"])
def test_p19_tdx_does_not_mistake_topology_node_ids_for_region_zone_ids(p19_evidence, node_id):
    calls, outcomes, _ = p19_evidence
    calls[1]["input"]["topology_graph"] = {"nodes": [{"id": node_id, "label": "VPC"}], "edges": []}
    assert p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]


@pytest.mark.parametrize("wrong_location", ["杭州 K", "杭州 I", "cn-shenzhen-a", "ap-northeast-1-a"])
@pytest.mark.parametrize("wrong_on_instance", [False, True])
def test_p19_tdx_supported_neighbor_or_cost_location_cannot_hide_wrong_placement(
    p19_evidence, wrong_location, wrong_on_instance,
):
    calls, outcomes, row = p19_evidence
    detail = calls[1]["input"]
    detail["cost_assumptions"] = ["部署 cn-hangzhou-j"]
    if wrong_on_instance:
        row["recommended_spec"] += "，" + wrong_location
    else:
        row["recommended_spec"] += "，cn-hangzhou-j"
        detail["resource_inventory"][1]["recommended_spec"] = wrong_location
    assert not p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]


def test_p19_tdx_cost_fallback_requires_unambiguous_location_when_deployment_has_none(p19_evidence):
    calls, outcomes, _ = p19_evidence
    detail = calls[1]["input"]
    detail["resource_inventory"][1]["recommended_spec"] = "与实例同一可用区"
    detail["cost_assumptions"] = ["按 cn-hangzhou-j 区域估价，部署前确认库存"]
    assert p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]
    detail["cost_assumptions"].append("也可能选北京 I")
    assert not p19_plan_facts(calls, outcomes)["declared TDX family, region and zone match verified support table"]


@pytest.mark.parametrize("resource_type", ["ALIYUN::ECS::Disk", "ALIYUN::ECS::Instance（系统盘）", ""])
def test_p19_separate_disk_cost_is_not_an_instance_but_disk_only_plan_cannot_pass(p19_evidence, resource_type):
    calls, outcomes, _ = p19_evidence
    inventory = calls[1]["input"]["resource_inventory"]
    inventory.append({"product": "ECS", "quantity": 1, "resource_type": resource_type,
                      "purpose": "系统盘", "recommended_spec": "cloud_essd，40 GiB，随实例按量计费"})
    review = p19_plan_review(calls, outcomes)
    assert all(review["checks"].values())
    assert review["specification_evidence"] == ["verified"]
    inventory.pop(0)
    review = p19_plan_review(calls, outcomes)
    assert not any(review["checks"].values())
    assert review["specification_evidence"] == ["missing_ecs_inventory"]


@pytest.mark.parametrize("spec, expected", [
    ("ecs.r9i.xlarge，40 GiB 系统盘，4 vCPU / 32 GiB 内存", "verified"),
    ("ecs.r9i.xlarge，系统盘 40GiB，4vCPU / 32GiB", "verified"),
    ("ecs.r9i.xlarge，4核，32G内存 + 40G系统盘", "verified"),
    ("ecs.r9i.xlarge，40 GB data disk，4 cores / RAM: 32 GB", "verified"),
    ("ecs.r9i.xlarge，4C32G，40G cloud_essd", "verified"),
    ("ecs.r9i.xlarge，4 vCPU / 32 GiB，系统盘 40 GiB，数据盘 100 GiB", "verified"),
    ("ecs.r9i.xlarge（4 vCPU/32 GiB），Ubuntu 24.04，40 GiB ESSD 系统盘", "verified"),
    ("ecs.r9i.xlarge，40 GiB ESSD Entry 系统盘，4 vCPU / 32 GiB", "verified"),
    ("ecs.r9i.xlarge（4 vCPU / 32 GiB），Ubuntu 24.04，ESSD PL0 40 GiB 系统盘", "verified"),
    ("ecs.r9i.xlarge，4 vCPU / 32 GiB，ESSD PL0 40 GiB", "verified"),
    ("ecs.r9i.xlarge，4 vCPU / 32 GiB RAM + ESSD PL1 40 GiB", "verified"),
    ("ecs.r9i.xlarge，SSD 40 GB，4 cores / 32 GB", "verified"),
    ("ecs.r9i.xlarge（2 vCPU/16 GiB），40 GiB ESSD 系统盘", "mismatch"),
    ("ecs.r9i.xlarge，4 vCPU / 64 GiB，ESSD PL0 40 GiB", "mismatch"),
    ("ecs.r9i.xlarge，4 vCPU，ESSD PL0 40 GiB", "specification_deferred_to_step2"),
    ("ecs.r9i.xlarge，32 GiB 系统盘，2 vCPU / 16 GiB 内存", "mismatch"),
    ("ecs.r9i.xlarge，4 vCPU / 64 GiB RAM，40 GiB disk", "mismatch"),
    ("ecs.r9i.xlarge，4 vCPU，40 GiB 系统盘", "specification_deferred_to_step2"),
    ("ecs.r9i.xlarge，4 vCPU，32 GiB / 64 GiB", "unparseable_specification"),
    ("ecs.r9i.xlarge，4 vCPU，内存 32 GiB 或 64 GiB 内存", "unparseable_specification"),
])
def test_p19_parses_ram_separately_from_disk_and_keeps_wrong_or_ambiguous_sizes_failing(p19_evidence, spec, expected):
    calls, outcomes, row = p19_evidence
    row["recommended_spec"] = spec
    review = p19_plan_review(calls, outcomes)
    assert review["specification_evidence"] == [expected]
    assert review["checks"]["instance sizes verified by successful DescribeInstanceTypes"] is (expected == "verified")


@pytest.mark.parametrize("spec, accepted", [
    ("4 vCPU / 32 GiB，40 GiB 系统盘", False),
    ("ecs.r9i.xlarge，40 GiB 系统盘", True),
    ("40 GiB 系统盘", False),
])
def test_p19_disk_does_not_supply_ram_and_deferred_sizes_still_need_a_supported_family(p19_evidence, spec, accepted):
    from scripts.ci.run_knowledge_wiki_a2a import p19_acceptance

    calls, outcomes, row = p19_evidence
    row["recommended_spec"] = spec
    row["purpose"] = "实例及系统盘"
    review = p19_plan_review(calls, outcomes)
    assert review["specification_evidence"] == ["specification_deferred_to_step2"]
    assert not review["checks"]["instance sizes verified by successful DescribeInstanceTypes"]
    assert all(p19_acceptance(review).values()) is accepted


def test_p19_reads_real_externalized_api_result_from_saved_session_without_changing_success(p19_evidence, tmp_path):
    from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY, ResultStorage
    from scripts.ci.run_step_followup_a2a import results

    calls, outcomes, row = p19_evidence
    config = tmp_path / "config"
    body = json.loads(outcomes["types"]["content"])
    body["Padding"] = "x" * 60_000
    processed = ResultStorage(str(config / "tool-results")).process("types", json.dumps(body))
    assert processed.is_externalized
    outcome = {"type": "tool_result", "tool_use_id": "types", "is_error": False, "content": processed.content,
               "metadata": {EXTERNALIZED_RESULT_PATH_METADATA_KEY: processed.file_path}}
    session = config / "projects/session/pipeline/transcripts/attempt/session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"content": [outcome]}) + "\n", encoding="utf-8")
    outcomes.update(results(config))
    assert p19_plan_review(calls, outcomes)["specification_evidence"] == ["missing_api_evidence"]
    assert p19_plan_review(calls, outcomes, config=config)["specification_evidence"] == ["verified"]
    row["recommended_spec"] = "ecs.r9i.xlarge，2 vCPU / 16 GiB"
    assert p19_plan_review(calls, outcomes, config=config)["specification_evidence"] == ["mismatch"]
    outcomes["types"]["is_error"] = True
    assert p19_plan_review(calls, outcomes, config=config)["specification_evidence"] == ["missing_api_evidence"]


@pytest.mark.parametrize("location", ["outside", "missing", "not-tool-result"])
def test_p19_cannot_borrow_external_api_evidence_outside_owned_tool_results(p19_evidence, tmp_path, location):
    from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY

    calls, outcomes, _ = p19_evidence
    config = tmp_path / "config"
    external = (tmp_path / "other/tool-results/response.txt" if location == "outside" else
                config / ("tool-results/response.txt" if location == "missing" else "workspace/response.txt"))
    external.parent.mkdir(parents=True)
    if location != "missing":
        external.write_text(outcomes["types"]["content"], encoding="utf-8")
    outcomes["types"].update(
        content="truncated preview", metadata={EXTERNALIZED_RESULT_PATH_METADATA_KEY: str(external)},
    )
    assert p19_plan_review(calls, outcomes, config=config)["specification_evidence"] == ["missing_api_evidence"]


def test_clarification_is_a_valid_purchase_path_but_failed_cancel_stops_normal_checks():
    events = [{"eventType": "input_required", "data": {"kind": "ask_user_question"}}]
    phase = planning_phase({}, events)
    assert phase == "clarification"
    first = pipeline_turn_review(
        "handoff", 0, phase=phase, had_candidate=False, handoff=False, unchanged=False,
    )
    assert all(first["checks"].values()) and first["proceed"]
    canceled = pipeline_turn_review(
        "handoff", 1, phase="selection", had_candidate=False, handoff=False, unchanged=False,
    )
    assert not canceled["proceed"]
    assert canceled["checks"] == {"explicit cancellation hands off to normal": False}
    assert "normal consultation" in canceled["observations"]["not_exercised"]
    assert planning_phase({}, [{"eventType": "input_required", "data": {"kind": "permission"}}]) == "other"


def test_challenge_preservation_requires_an_existing_candidate():
    no_candidate = pipeline_turn_review(
        "p19", 1, phase="selection", had_candidate=False, handoff=False, unchanged=False,
    )
    assert not no_candidate["checks"]
    assert no_candidate["observations"]["candidate_preservation"] == "not_exercised_no_prior_candidate"
    changed = pipeline_turn_review(
        "p19", 1, phase="selection", had_candidate=True, handoff=False, unchanged=False,
    )
    assert changed["checks"] == {"challenge preserves existing candidate": False}


def _p19_saved_selection():
    return {"status": "awaiting_selection", "candidate_set_id": "tdx-batch",
            "candidates": [{"name": "TDX ECS"}], "options": [{"id": "candidate-0"}],
            "intent": {"resource_intents": [{"product": "ECS", "action": "create"}]}}


def _p19_clarification():
    step = {"id": "solution_planning_and_selection", "runId": "planning-run"}
    event = {"eventType": "input_required", "eventId": "question-2", "step": step,
             "data": {"kind": "ask_user_question"}}
    state = {"snapshot": {
        "status": "waiting_input", "normalHandoff": None,
        "pendingInput": {"eventId": "question-2", "kind": "ask_user_question", "step": step},
        "steps": [{**step, "status": "waiting_input", "conclusion": _p19_saved_selection()}],
    }}
    return event, state


@pytest.mark.parametrize("defect", [None, "missing", "stale_question", "wrong_step", "wrong_run", "permission",
                                  "not_waiting", "handoff"])
def test_clarification_evidence_must_be_current_waiting_planning_step(defect):
    event, state = _p19_clarification()
    snapshot = state["snapshot"]
    if defect == "missing":
        snapshot["steps"][0].pop("conclusion")
    elif defect == "stale_question":
        snapshot["pendingInput"]["eventId"] = "old-question"
    elif defect == "wrong_step":
        snapshot["steps"][0]["id"] = "materialize_selected_candidate"
    elif defect == "wrong_run":
        snapshot["steps"][0]["runId"] = "old-run"
    elif defect == "permission":
        snapshot["pendingInput"]["kind"] = "permission"
    elif defect == "not_waiting":
        snapshot["status"] = "completed"
    elif defect == "handoff":
        snapshot["normalHandoff"] = {"status": "ready"}
    saved = clarification_conclusion(state, [event])
    review = pipeline_turn_review(
        "p19", 1, phase="clarification", had_candidate=True, handoff=False, unchanged=False,
        prior_conclusion=_p19_saved_selection(), clarification=saved,
    )
    assert review["checks"]["challenge preserves existing candidate"] is (defect is None)


@pytest.mark.parametrize("number", [2, 3])
def test_side_consultation_requires_unchanged_selection_instead_of_rejection(number):
    prior = _p19_saved_selection()
    kept = pipeline_turn_review(
        "p19", number, phase="selection", had_candidate=True, handoff=False, unchanged=True,
        prior_conclusion=prior, conclusion=deepcopy(prior),
    )
    assert all(kept["checks"].values())
    rejected = pipeline_turn_review(
        "p19", number, phase="other", had_candidate=True, handoff=True, unchanged=False,
        prior_conclusion=prior, conclusion={"status": "rejected", "rejection_reason": "Stock consultation"},
    )
    assert not any(rejected["checks"].values())
    no_candidate = pipeline_turn_review(
        "p19", number, phase="clarification", had_candidate=False, handoff=False, unchanged=False,
    )
    assert not no_candidate["checks"]
    assert no_candidate["observations"]["candidate_preservation"] == "not_exercised_no_prior_candidate"


@pytest.mark.parametrize("phase, handoff, unchanged", [
    ("selection", True, True), ("selection", False, False), ("other", False, True),
])
def test_saved_payload_alone_does_not_prove_purchase_is_still_waiting(phase, handoff, unchanged):
    prior = _p19_saved_selection()
    review = pipeline_turn_review(
        "p19", 2, phase=phase, had_candidate=True, handoff=handoff, unchanged=unchanged,
        prior_conclusion=prior, conclusion=deepcopy(prior),
    )
    assert not review["checks"]["side consultation keeps selection active"]
    assert review["checks"]["side consultation preserves candidate payload"]


@pytest.mark.parametrize("field", ["candidate_set_id", "candidates", "options", "intent"])
def test_unchanged_marker_cannot_hide_a_replaced_candidate_payload(field):
    prior = _p19_saved_selection()
    current = deepcopy(prior)
    current[field] = "replaced" if field == "candidate_set_id" else [] if field != "intent" else {}
    review = pipeline_turn_review(
        "p19", 3, phase="selection", had_candidate=True, handoff=False, unchanged=True,
        prior_conclusion=prior, conclusion=current,
    )
    assert review["checks"]["side consultation keeps selection active"]
    assert not review["checks"]["side consultation preserves candidate payload"]


def test_only_cloud_pricing_followup_can_load_skill_in_automatic_normal_case():
    assert requires_skill_free_lookup("normal", automatic=True, number=0)
    assert requires_skill_free_lookup("normal", automatic=True, number=1)
    assert not requires_skill_free_lookup("normal", automatic=True, number=2)
    assert requires_skill_free_lookup("normal", automatic=False, number=2)
    assert requires_skill_free_lookup("sizing", automatic=True, number=0)


@pytest.fixture
def scripted_runner(tmp_path, monkeypatch):
    """Exercise runner control flow with fixed transport records, without live services."""
    from scripts.ci import run_knowledge_wiki_a2a as runner

    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))
    state = {"calls": [], "outcomes": {}, "events": {}, "answer": "", "sent": [], "pipeline_state": {},
             "snapshots_fetched": []}

    class Server:
        def __init__(self, **kwargs):
            self.stopped = False
            self.process = SimpleNamespace(poll=lambda: 0 if self.stopped else None)

        def start(self):
            pass

        def terminate(self):
            self.stopped = True

    class Capture:
        def __init__(self, *args):
            self.env = {}

        def start(self):
            return self

        def stop(self):
            return [{"attributes": {"gen_ai.request.model": "fixture"}}]

    monkeypatch.setattr(runner, "ManagedServer", Server)
    monkeypatch.setattr(runner, "ObserveCapture", Capture)
    monkeypatch.setattr(runner, "wait_for_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "tool_inputs", lambda _: list(state["calls"]))
    monkeypatch.setattr(runner, "results", lambda _: state["outcomes"])
    monkeypatch.setattr(runner, "envelopes", lambda p: state["events"].get(p.name, []))
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda _: state["answer"])
    monkeypatch.setattr(runner, "final_text", lambda _: state["answer"])

    def fetch(**kwargs):
        state["snapshots_fetched"].append(kwargs["name"])
        return deepcopy(state["pipeline_state"])

    monkeypatch.setattr(runner, "fetch_pipeline_state", fetch)
    monkeypatch.setattr(runner, "auto_evidence", lambda *args: {"queries": [], "requests": [
        {"kind": "helper"}, {"kind": "main", "reference_blocks": 1, "system_has_results": False},
    ]})

    def run(story, *, scenario, automatic=False):
        def stream(**kwargs):
            index = len(state["sent"])
            turn = story[index]
            state["sent"].append(kwargs["prompt"])
            state["events"][kwargs["name"] + ".events.jsonl"] = turn.get("events", [])
            (kwargs["run_dir"] / (kwargs["name"] + ".events.jsonl")).write_text("", encoding="utf-8")
            state["answer"] = turn.get("answer", "fixture answer")
            state["pipeline_state"] = turn.get("pipeline_state", {})
            state["calls"].extend(turn.get("calls", []))
            state["outcomes"].update(turn.get("outcomes", {}))
            return SimpleNamespace(
                context_id="fixture-context", task_id="fixture-task", normal_handoff_ready=turn.get("handoff", False),
                last_status_state="TASK_STATE_INPUT_REQUIRED", response_content_type="text/event-stream", text="",
            )

        monkeypatch.setattr(runner, "stream_message", stream)
        args = Namespace(
            run_dir=tmp_path, scenario=scenario, wiki_auto_search=automatic, repeat=1,
            runtime_executable=None, python="unused", provider="openai", model="fixture", timeout=10,
        )
        return runner.run(args), state

    return run


def _body_call(name, path, body="body"):
    return {
        "calls": [{"id": name, "name": "read_file", "input": {"path": path}}],
        "outcomes": {name: {"is_error": False, "content": "File: " + path + " (10 lines)\n" + body}},
    }


@pytest.mark.parametrize("missed,answer,expected", [
    (True, "RVV 的 VLEN 由实现决定；AVX-512 的向量寄存器是 512 位。", True),
    (True, "无法核实具体实现的 VLEN。", True),
    (True, " \n", False),
    (False, "RVV 的 VLEN 由实现决定。", False),
])
def test_wiki_miss_allows_general_knowledge_but_requires_actual_miss_and_answer(
    scripted_runner, missed, answer, expected,
):
    first = {
        "answer": answer,
        "calls": [{"id": "search", "name": "grep", "input": {"path": "knowledge://", "pattern": "RVV"}}],
        "outcomes": {"search": {"is_error": False, "content": "No matches" if missed else "RVV result"}},
    }
    report, _ = scripted_runner([first, {"answer": "Happy birthday!"}], scenario="boundaries")
    assert report["status"] == ("passed" if expected else "failed")
    assert report["checks"]["repeat-1-turn-1: actual Wiki search miss observed"] is missed
    assert report["checks"]["repeat-1-turn-1: Wiki miss still receives an answer"] is bool(answer.strip())
    assert report["turns"][0]["fallback_or_uncertainty"] is ("无法" in answer)
    assert report["checks"]["repeat-1-turn-2: unrelated request avoids Wiki tools"]


@pytest.mark.parametrize("defect", [None, "undisclosed", "confirmed", "quantity", "candidate_lost"])
def test_runner_reports_pending_tdx_as_observation_and_keeps_other_failures_blocking(
    scripted_runner, p19_evidence, defect,
):
    first = _body_call("ecs", "/app/knowledge/clouds/aliyun/products/ecs/ecs.md")
    calls, outcomes, row = p19_evidence
    detail = calls[1]["input"]
    detail["resource_inventory"][1]["recommended_spec"] = "杭州 K"
    row["purpose"] = {"undisclosed": "TDX 实例", "confirmed": "TDX 已确认支持"}.get(
        defect, "TDX 支持待实测验证",
    )
    if defect == "quantity":
        row["quantity"] = 0
    first["calls"].extend(calls)
    first["outcomes"].update(outcomes)
    first["events"] = [{"eventType": "step_completed", "data": {"conclusion": _p19_saved_selection()}}]
    followup = {"events": [{"eventType": "step_completed", "data": {
        "conclusion": _p19_saved_selection(), "conclusion_unchanged": True,
    }}]}
    third = followup
    if defect == "candidate_lost":
        third = {"handoff": True, "events": [{"eventType": "step_completed", "data": {
            "conclusion": {"status": "rejected"},
        }}]}
    final = _body_call("b70", "/app/knowledge/common/hardware/intel-arc-pro-b70.md", "Xe2, 32 GB GDDR6")
    final.update(followup, answer="Intel Arc Pro B70 GPU: Xe2 / Battlemage, 32 GB GDDR6.")
    report, _ = scripted_runner([first, followup, third, final], scenario="p19")
    assert report["status"] == ("passed" if defect is None else "failed")
    turn = report["turns"][0]
    assert not turn["plan_facts"]["declared TDX family, region and zone match verified support table"]
    assert turn["placement_evidence"] == ["unsupported_zone"]
    observations = turn["runner_observations"].get("model_quality", [])
    assert bool(observations) is (defect not in {"undisclosed", "confirmed"})
    if observations:
        assert observations[0]["blocking"] is False


@pytest.mark.parametrize("field", [None, "candidate_set_id", "candidates", "options", "intent"])
def test_runner_checks_server_candidate_at_challenge_question_pause(scripted_runner, p19_evidence, field):
    first = _body_call("ecs", "/app/knowledge/clouds/aliyun/products/ecs/ecs.md")
    calls, outcomes, _ = p19_evidence
    first["calls"].extend(calls)
    first["outcomes"].update(outcomes)
    first["events"] = [{"eventType": "step_completed", "data": {"conclusion": _p19_saved_selection()}}]
    question, snapshot = _p19_clarification()
    if field:
        snapshot["snapshot"]["steps"][0]["conclusion"][field] = "different" if field == "candidate_set_id" else []
    second = {"events": [question], "pipeline_state": snapshot}
    followup = {"events": [{"eventType": "step_completed", "data": {
        "conclusion": _p19_saved_selection(), "conclusion_unchanged": True,
    }}]}
    final = _body_call("b70", "/app/knowledge/common/hardware/intel-arc-pro-b70.md", "Xe2, 32 GB GDDR6")
    final.update(followup, answer="Intel Arc Pro B70 GPU: Xe2 / Battlemage, 32 GB GDDR6.")
    report, state = scripted_runner([first, second, followup, final], scenario="p19")
    assert state["sent"] == list(P19)
    assert state["snapshots_fetched"] == ["repeat-1-turn-2"]
    assert report["status"] == ("failed" if field else "passed")
    assert [key for key, ok in report["checks"].items() if not ok] == (
        ["repeat-1-turn-2: challenge preserves existing candidate"] if field else []
    )


def test_runner_does_not_send_normal_prompts_after_unsuccessful_cancel(scripted_runner):
    first = _body_call("ecs", "/app/knowledge/clouds/aliyun/products/ecs/ecs.md")
    first["events"] = [{"eventType": "input_required", "data": {"kind": "ask_user_question"}}]
    report, state = scripted_runner([first, {}], scenario="handoff")
    assert state["sent"] == [P19[0], "取消这次购买，结束当前方案流程，接下来只进行普通聊天。"]
    assert report["status"] == "failed"
    assert [key for key, passed in report["checks"].items() if not passed] == [
        "repeat-1-turn-2: explicit cancellation hands off to normal",
    ]
    assert "normal consultation" in report["turns"][1]["runner_observations"]["not_exercised"]


@pytest.mark.parametrize("wrong_size", [False, True])
@pytest.mark.parametrize("reject_side_consultation", [False, True])
def test_runner_validates_plan_when_it_is_published_after_clarification(
    scripted_runner, wrong_size, reject_side_consultation,
):
    first = _body_call("ecs", "/app/knowledge/clouds/aliyun/products/ecs/ecs.md")
    first["events"] = [{"eventType": "input_required", "data": {"kind": "ask_user_question"}}]
    spec = "ecs.r9i.xlarge，8 vCPU / 64 GiB" if wrong_size else "ecs.r9i.xlarge，4 vCPU / 32 GiB"
    second = {"calls": [
        {"id": "types", "name": "aliyun_api", "input": {"action": "DescribeInstanceTypes"}},
        {"id": "plan", "name": "show_candidate_detail", "input": {"resource_inventory": [
            {"product": "ECS", "quantity": 1, "recommended_spec": spec},
            {"product": "VSwitch", "recommended_spec": "cn-hangzhou 可用区 J"},
        ]}},
    ], "outcomes": {
        "types": {"is_error": False, "content": json.dumps({"InstanceTypes": [{
            "InstanceTypeId": "ecs.r9i.xlarge", "CpuCoreCount": 4, "MemorySize": 32,
        }]})}, "plan": {"is_error": False, "content": "OK"},
    }, "events": [{"eventType": "step_completed", "data": {"conclusion": _p19_saved_selection()}}]}
    followup = {"events": [{"eventType": "step_completed", "data": {
        "conclusion": _p19_saved_selection(), "conclusion_unchanged": True,
    }}]}
    third = followup
    if reject_side_consultation:
        third = {"handoff": True, "events": [{"eventType": "step_completed", "data": {
            "conclusion": {"status": "rejected", "rejection_reason": "Independent stock consultation"},
        }}]}
    final = _body_call("b70", "/app/knowledge/common/hardware/intel-arc-pro-b70.md", "Xe2, 32 GB GDDR6")
    final["answer"] = "Intel Arc Pro B70 GPU: Xe2 / Battlemage, 32 GB GDDR6."
    if not reject_side_consultation:
        final.update(followup)
    report, state = scripted_runner([first, second, third, final], scenario="p19")
    assert state["sent"] == list(P19)
    assert not any("instance sizes" in key and key.startswith("repeat-1-turn-1:") for key in report["checks"])
    assert report["turns"][0]["runner_observations"]["plan_verification"] == "not_exercised_no_candidate"
    assert report["status"] == ("failed" if wrong_size or reject_side_consultation else "passed")
    assert report["turns"][1]["specification_evidence"] == ["mismatch" if wrong_size else "verified"]
    assert report["turns"][1]["placement_evidence"] == ["verified"]
    for check in ("side consultation keeps selection active", "side consultation preserves candidate payload"):
        assert report["checks"]["repeat-1-turn-3: " + check] is (not reject_side_consultation)


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("confirmation, expected", [
    ("Intel B70 是一张显卡（GPU），与 TDX 无关。", "passed"),
    ("B70 不是显卡。", "failed"),
])
def test_p19_confirmation_records_sources_without_requiring_another_read(
    scripted_runner, automatic, confirmation, expected,
):
    first = _body_call("ecs", "/app/knowledge/clouds/aliyun/products/ecs/ecs.md")
    first["events"] = [{"eventType": "input_required", "data": {"kind": "ask_user_question"}}]
    third = _body_call("b70", "/app/knowledge/common/hardware/intel-arc-pro-b70.md", "Xe2, 32 GB GDDR6")
    third["answer"] = "Intel Arc Pro B70: Xe2, 32 GB GDDR6."
    report, state = scripted_runner(
        [first, {}, third, {"answer": confirmation}], scenario="p19", automatic=automatic,
    )
    assert state["sent"] == list(P19)
    assert report["status"] == expected
    assert report["turns"][2]["hardware_body_read"] is True
    assert report["turns"][3]["hardware_body_available"] is False
    assert report["checks"]["repeat-1-turn-4: B70 GPU identity confirmed"] is (expected == "passed")
    assert "repeat-1-turn-4: actual B70 body available" not in report["checks"]
    assert "repeat-1-turn-4: B70 facts used correctly" not in report["checks"]


@pytest.mark.parametrize("read_body", [False, True])
def test_explicit_b70_question_still_requires_body_and_full_specification(scripted_runner, read_body):
    first = _body_call("b70", "/app/knowledge/common/hardware/intel-arc-pro-b70.md", "Xe2, 32 GB GDDR6")
    if not read_body:
        first = {}
    first["answer"] = "Intel B70 是一张显卡（GPU）。"
    report, _ = scripted_runner([first], scenario="normal")
    assert report["status"] == "failed"
    assert report["checks"]["repeat-1-turn-1: actual B70 body available"] is read_body
    assert not report["checks"]["repeat-1-turn-1: B70 facts used correctly"]


@pytest.mark.parametrize("skill_on_first_turn", [False, True])
def test_runner_allows_pricing_skill_without_removing_pure_knowledge_check(scripted_runner, skill_on_first_turn):
    first = _body_call("b70", "/app/knowledge/common/hardware/intel-arc-pro-b70.md", "Xe2, 32 GB GDDR6")
    first["answer"] = "Intel Arc Pro B70: Xe2 / Battlemage, 32 GB GDDR6."
    third = {"calls": [{"id": "skill", "name": "skill", "input": {"name": "iac_aliyun"}}]}
    if skill_on_first_turn:
        first["calls"].extend(third["calls"])
        third = {}
    report, _ = scripted_runner([first, {}, third], scenario="normal", automatic=True)
    assert report["status"] == ("failed" if skill_on_first_turn else "passed")
    assert "repeat-1-turn-1: no Skill loaded" in report["checks"]
    assert "repeat-1-turn-3: no Skill loaded" not in report["checks"]


@pytest.mark.parametrize("ecs_source", [False, True])
def test_sizing_source_check_accepts_related_ecs_pages_but_not_unrelated_body(scripted_runner, ecs_source):
    path = "/app/knowledge/" + ("clouds/aliyun/products/ecs/hardware.md" if ecs_source else "common/other.md")
    report, _ = scripted_runner([_body_call("read", path)], scenario="sizing")
    assert report["status"] == ("passed" if ecs_source else "failed")
    assert report["turns"][0]["sizing_page_read"] is False


def test_step2_cannot_use_step1_read_as_its_own_evidence(tmp_path, monkeypatch):
    from scripts.ci import run_knowledge_wiki_a2a as runner
    from scripts.ci import run_step_followup_a2a as followup

    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))
    (tmp_path / "turn-1.events.jsonl").touch()
    monkeypatch.setattr(followup, "run", lambda args: {
        "status": "passed", "checks": {"step2 real template, preview and quote": True},
    })
    monkeypatch.setattr(runner, "tool_inputs", lambda _: [{"id": "step1-read", "name": "read_file", "input": {}}])
    monkeypatch.setattr(runner, "results", lambda _: {"step1-read": {
        "is_error": False, "content": "File: /app/knowledge/clouds/aliyun/products/ecs/ecs.md (50 lines)\nbody",
    }})
    monkeypatch.setattr(runner, "envelopes", lambda _: [{
        "eventType": "tool_started", "step": {"id": "solution_planning_and_selection"},
        "data": {"toolUseId": "step1-read"},
    }])
    args = Namespace(run_dir=tmp_path, scenario="step2")
    report = runner.run(args)
    assert args.auto_approve_permissions is False
    assert report["status"] == "failed"
    assert not report["wiki"]["read"]
