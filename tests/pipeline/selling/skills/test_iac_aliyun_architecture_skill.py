from __future__ import annotations

from pathlib import Path

import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "iac_code"
    / "pipeline"
    / "selling"
    / "skills"
    / "iac-aliyun-architecture"
)
PROMPT_FILE = SKILL_DIR.parents[1] / "prompts" / "architecture_planning.md"


def test_explicit_candidate_count_precedes_default_complexity_rules_in_both_planning_instructions():
    for path in (PROMPT_FILE, SKILL_DIR / "SKILL.md"):
        body = path.read_text(encoding="utf-8")
        assert body.index("用户明确指定候选方案数量时，优先遵守该数量") < body.index("用户未指定数量时")
        assert "intent.additional_notes" in body
        assert "先请求澄清，不要静默改变数量或虚构差异" in body


def test_architecture_consumes_intent_resource_lifecycle_contract():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "resource_intents" in body
    assert "action=create" in body
    assert "action=use_existing" in body
    assert "action=forbid" in body
    assert "use_existing/reference 必须作为已有资源引用" in body
    assert "不得生成 VSwitch" in body
    assert "forbidden_resources" not in body


def test_architecture_carries_latest_user_hard_constraints_without_product_assumptions():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "intent.hard_constraints" in body
    assert "最新用户要求" in body
    assert "用户明确删除约束时从快照中删除" in body
    assert "verification_mode" in body
    assert "推断规格或本方案推荐值" in body


def test_architecture_hard_constraint_schema_describes_every_field():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    end = body.index("---", 3)
    schema = yaml.safe_load(body[3:end])["conclusion_schema"]
    properties = schema["properties"]["candidates"]["items"]["properties"]["hard_constraints"]["items"]["properties"]

    assert properties["id"]["minLength"] == 1
    assert all(value.get("description") for value in properties.values())


def test_architecture_prompt_guides_optional_memory_lookup_for_planning_context():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "不要读取项目文件或记忆" not in body
    assert "read_memory({})" in body
    assert "架构偏好" in body
    assert "已有 VPC" in body
    assert "当前用户意图为准" in body


def test_architecture_keeps_iac_code_web_candidate_on_fixed_entry_topology():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "单 ECS + EIP" in body
    assert "安全组仅开放 8766" in body
    assert "不得增加其他入口资源" in body
    assert "`candidate.name` 固定为 `iac-code-web-single-ecs`" in body
