from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script_module():
    script_path = Path("scripts/rendering/generate_ros_architecture_draft_rules.py")
    spec = importlib.util.spec_from_file_location("generate_ros_architecture_draft_rules", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _facts_payload() -> dict:
    return {
        "summary": {"api_resource_types": 2, "resource_facts": 2, "rule_signals": 2},
        "resource_facts": [
            {
                "resource_type": "ALIYUN::VPC::SnatEntry",
                "product_code": "vpc",
                "source_state": "api+local",
                "name": {"zh": "SNAT条目", "en": "SNAT entry"},
                "description": "SNAT entries configure internet access through a NAT gateway.",
                "properties": {"SnatTableId": {"description": "The SNAT table ID."}},
                "related_properties": {"SnatTableId": ["ALIYUN::VPC::NatGateway"]},
                "main_resource_type": None,
                "fixed_rule_hits": [],
            },
            {
                "resource_type": "ALIYUN::ECS::RunCommand",
                "product_code": "ecs",
                "source_state": "api+local",
                "name": {"zh": "执行命令", "en": "Run command"},
                "description": "Runs a Cloud Assistant command on ECS instances.",
                "properties": {"InstanceIds": {"description": "ECS instances."}},
                "related_properties": {"InstanceIds": ["ALIYUN::ECS::Instance"]},
                "main_resource_type": None,
                "fixed_rule_hits": [],
            },
        ],
        "rule_signals": [
            {
                "category": "attachment",
                "resource_type": "ALIYUN::VPC::SnatEntry",
                "property_name": "SnatTableId",
                "target_resource_types": ["ALIYUN::VPC::NatGateway"],
                "confidence": "medium",
                "evidence": ["SNAT entry points to NAT gateway."],
                "suggested_patch": {},
            },
            {
                "category": "orchestration_action",
                "resource_type": "ALIYUN::ECS::RunCommand",
                "property_name": None,
                "target_resource_types": ["ALIYUN::ECS::Instance"],
                "confidence": "high",
                "evidence": ["Runs command on ECS instances."],
                "suggested_patch": {},
            },
        ],
    }


def test_chunks_facts_and_reuses_cached_llm_draft_results(tmp_path: Path) -> None:
    module = _load_script_module()
    facts_path = tmp_path / "facts.json"
    cache_path = tmp_path / "draft-cache.json"
    facts_path.write_text(json.dumps(_facts_payload(), ensure_ascii=False), encoding="utf-8")
    cache_path.write_text(
        json.dumps(
            {
                "chunks": {
                    "vpc": {
                        "fingerprint": module.chunk_fingerprint(
                            module.chunk_resource_facts(_facts_payload(), max_facts_per_chunk=1)[1]
                        ),
                        "response_text": json.dumps(
                            {
                                "draft_rules": [
                                    {
                                        "resource_type": "ALIYUN::VPC::SnatEntry",
                                        "classification": "attachment",
                                        "target_resource_types": ["ALIYUN::VPC::NatGateway"],
                                        "property_names": ["SnatTableId"],
                                        "label": "SNAT entry",
                                        "edge_label": None,
                                        "confidence": 0.91,
                                        "evidence": ["cached"],
                                        "suggested_architecture_rules_patch": {"compact_child_attachments": []},
                                    }
                                ]
                            }
                        ),
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    async def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(
            {
                "draft_rules": [
                    {
                        "resource_type": "ALIYUN::ECS::RunCommand",
                        "classification": "orchestration_action",
                        "target_resource_types": ["ALIYUN::ECS::Instance"],
                        "property_names": ["InstanceIds"],
                        "label": "Cloud Assistant execution",
                        "edge_label": "runs command",
                        "confidence": 0.89,
                        "evidence": ["generated"],
                        "suggested_architecture_rules_patch": {"compact_orchestration_actions": []},
                    }
                ]
            }
        )

    result = module.run_draft_generation_from_facts_payload(
        _facts_payload(),
        cache_path=cache_path,
        max_facts_per_chunk=1,
        complete_chunk=fake_llm,
    )

    assert [chunk["chunk_id"] for chunk in module.chunk_resource_facts(_facts_payload(), max_facts_per_chunk=1)] == [
        "ecs",
        "vpc",
    ]
    assert len(result["draft_rules"]) == 2
    assert [rule["resource_type"] for rule in result["draft_rules"]] == [
        "ALIYUN::ECS::RunCommand",
        "ALIYUN::VPC::SnatEntry",
    ]
    assert len(calls) == 1
    assert "ALIYUN::ECS::RunCommand" in calls[0]
    assert "ALIYUN::VPC::SnatEntry" not in calls[0]

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(cache["chunks"]) == {"ecs", "vpc"}


def test_draft_cache_ignores_stale_chunk_fingerprints(tmp_path: Path) -> None:
    module = _load_script_module()
    cache_path = tmp_path / "draft-cache.json"
    cache_path.write_text(
        json.dumps({"chunks": {"ecs": {"fingerprint": "stale", "response_text": "not json"}}}),
        encoding="utf-8",
    )
    calls: list[str] = []

    async def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"draft_rules": []})

    result = module.run_draft_generation_from_facts_payload(
        _facts_payload(),
        cache_path=cache_path,
        max_facts_per_chunk=1,
        complete_chunk=fake_llm,
    )

    assert result["summary"]["parse_errors"] == 0
    assert len(calls) == 2
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["chunks"]["ecs"]["fingerprint"] != "stale"


def test_global_chunk_mode_packs_multiple_products_into_bounded_batches() -> None:
    module = _load_script_module()
    payload = _facts_payload()

    chunks = module.chunk_resource_facts(payload, max_facts_per_chunk=2, mode="global")

    assert len(chunks) == 1
    assert chunks[0]["chunk_id"] == "all-1"
    assert [fact["resource_type"] for fact in chunks[0]["resource_facts"]] == [
        "ALIYUN::ECS::RunCommand",
        "ALIYUN::VPC::SnatEntry",
    ]
    assert {signal["resource_type"] for signal in chunks[0]["rule_signals"]} == {
        "ALIYUN::ECS::RunCommand",
        "ALIYUN::VPC::SnatEntry",
    }


def test_actionable_only_chunking_filters_full_facts_to_selected_signal_categories() -> None:
    module = _load_script_module()
    payload = _facts_payload()
    payload["resource_facts"].append(
        {
            "resource_type": "ALIYUN::ECS::Instance",
            "product_code": "ecs",
            "source_state": "api+local",
            "name": {"zh": "ECS 实例", "en": "ECS instance"},
            "description": "Primary compute resource.",
            "properties": {},
            "related_properties": {},
            "main_resource_type": None,
            "fixed_rule_hits": [],
        }
    )
    payload["rule_signals"].append(
        {
            "category": "display",
            "resource_type": "ALIYUN::ECS::Instance",
            "property_name": None,
            "target_resource_types": [],
            "confidence": "low",
            "evidence": ["display only"],
            "suggested_patch": {},
        }
    )

    chunks = module.chunk_resource_facts(
        payload,
        max_facts_per_chunk=10,
        mode="global",
        actionable_only=True,
        signal_categories=("attachment",),
    )

    assert len(chunks) == 1
    assert [fact["resource_type"] for fact in chunks[0]["resource_facts"]] == ["ALIYUN::VPC::SnatEntry"]
    assert {signal["category"] for signal in chunks[0]["rule_signals"]} == {"attachment"}


def test_actionable_only_chunking_can_filter_by_signal_confidence() -> None:
    module = _load_script_module()

    chunks = module.chunk_resource_facts(
        _facts_payload(),
        max_facts_per_chunk=10,
        mode="global",
        actionable_only=True,
        signal_categories=("attachment", "orchestration_action"),
        signal_confidences=("high",),
    )

    assert len(chunks) == 1
    assert [fact["resource_type"] for fact in chunks[0]["resource_facts"]] == ["ALIYUN::ECS::RunCommand"]
    assert {signal["confidence"] for signal in chunks[0]["rule_signals"]} == {"high"}


def test_run_summary_records_llm_filter_scope(tmp_path: Path) -> None:
    module = _load_script_module()
    calls: list[str] = []

    async def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"draft_rules": []})

    result = module.run_draft_generation_from_facts_payload(
        _facts_payload(),
        cache_path=tmp_path / "cache.json",
        max_facts_per_chunk=10,
        chunk_mode="global",
        actionable_only=True,
        signal_categories=("orchestration_action",),
        complete_chunk=fake_llm,
    )

    assert result["summary"]["input_resource_facts"] == 2
    assert result["summary"]["llm_resource_facts"] == 1
    assert result["summary"]["signal_categories"] == ["orchestration_action"]
    assert len(calls) == 1
    assert "ALIYUN::ECS::RunCommand" in calls[0]
    assert "ALIYUN::VPC::SnatEntry" not in calls[0]


def test_prompt_compacts_long_descriptions_before_llm_call() -> None:
    module = _load_script_module()
    chunk = module.chunk_resource_facts(_facts_payload(), max_facts_per_chunk=2, mode="global")[0]
    chunk["resource_facts"][0]["properties"]["HugeProperty"] = {
        "description": "x" * 1000,
        "related_targets": [],
        "required": False,
        "type": "string",
    }

    prompt = module.build_draft_rules_prompt(chunk)

    assert "x" * 500 not in prompt
    assert "HugeProperty" in prompt
    assert '"property_names"' in prompt


def test_prompt_requests_bilingual_labels_and_fingerprint_tracks_prompt_version() -> None:
    module = _load_script_module()
    chunk = module.chunk_resource_facts(_facts_payload(), max_facts_per_chunk=2, mode="global")[0]

    prompt = module.build_draft_rules_prompt(chunk)
    first_fingerprint = module.chunk_fingerprint(chunk)
    module.PROMPT_VERSION = "unit-test-new-prompt"
    second_fingerprint = module.chunk_fingerprint(chunk)

    assert '"zh"' in prompt
    assert '"en"' in prompt
    assert first_fingerprint != second_fingerprint


def test_draft_generation_records_parse_errors_and_continues(tmp_path: Path) -> None:
    module = _load_script_module()

    async def bad_llm(_prompt: str) -> str:
        return "not json"

    result = module.run_draft_generation_from_facts_payload(
        _facts_payload(),
        cache_path=tmp_path / "cache.json",
        max_facts_per_chunk=1,
        chunk_mode="global",
        complete_chunk=bad_llm,
    )

    assert result["draft_rules"] == []
    assert result["summary"]["parse_errors"] == 2
    assert {chunk["parse_error"] for chunk in result["summary"]["chunks"]} == {
        "LLM output does not contain a JSON object"
    }


def test_load_existing_draft_result_skips_llm_generation(tmp_path: Path) -> None:
    module = _load_script_module()
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps({"summary": {"draft_rules": 0}, "draft_rules": []}), encoding="utf-8")

    result = module.load_existing_draft_result(draft_path)

    assert result == {"summary": {"draft_rules": 0}, "draft_rules": []}


def test_filter_draft_result_removes_subagent_rejected_resource_types() -> None:
    module = _load_script_module()
    result = {
        "summary": {"draft_rules": 2},
        "draft_rules": [
            {"resource_type": "ALIYUN::CEN::CenInstanceAttachment"},
            {"resource_type": "ALIYUN::ECS::RunCommand"},
        ],
    }

    filtered = module.filter_draft_result(result, reject_resource_types=("ALIYUN::CEN::CenInstanceAttachment",))

    assert filtered["draft_rules"] == [{"resource_type": "ALIYUN::ECS::RunCommand"}]
    assert filtered["summary"]["draft_rules"] == 1
    assert filtered["summary"]["subagent_rejected_resource_types"] == ["ALIYUN::CEN::CenInstanceAttachment"]


def test_default_backup_path_is_outside_source_tree() -> None:
    module = _load_script_module()

    backup_path = module.default_backup_path()

    assert backup_path.name == "architecture_rules.json.backup"
    assert "src/iac_code" not in backup_path.as_posix()


def test_summarizes_rule_diff_and_renders_final_report() -> None:
    module = _load_script_module()
    before = {"compact_child_attachments": [], "compact_resource_labels": {"A": "old"}}
    after = {
        "compact_child_attachments": [{"resource_types": ["ALIYUN::VPC::SnatEntry"]}],
        "compact_resource_labels": {"A": "old", "B": "new"},
    }
    diff = module.summarize_rules_diff(before, after)
    report = module.render_final_report_markdown(
        facts_payload=_facts_payload(),
        draft_result={"summary": {"draft_rules": 1, "parse_errors": 0}, "draft_rules": [{}]},
        review_markdown="# review\n\n| Accepted | 1 |\n| Rejected | 0 |\n",
        diff_summary=diff,
        paths={
            "facts": "/tmp/facts.json",
            "draft": "/tmp/draft.json",
            "review": "/tmp/review.md",
            "rules": "/tmp/rules.json",
        },
        verification_results=["pytest: pending"],
    )

    assert diff["compact_child_attachments"]["added"] == 1
    assert diff["compact_resource_labels"]["dict_added"] == 1
    assert "LLM 草案数量" in report
    assert "`/tmp/rules.json`" in report
    assert "compact_child_attachments" in report
