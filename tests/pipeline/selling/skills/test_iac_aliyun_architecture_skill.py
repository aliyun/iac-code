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


def test_architecture_requires_intent_resource_coverage_or_explicit_exclusion():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    end = body.index("---", 3)
    schema = yaml.safe_load(body[3:end])["conclusion_schema"]
    candidate_properties = schema["properties"]["candidates"]["items"]["properties"]
    exclusion = candidate_properties["excluded_resource_intents"]["items"]

    assert set(exclusion["required"]) == {"product", "reason"}
    assert exclusion["properties"]["reason"]["minLength"] == 1
    assert "不允许静默丢弃已解析的意图资源" in body
    assert "excluded_resource_intents" in body


def test_architecture_prompt_states_intent_resource_coverage_constraint():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "excluded_resource_intents" in body
    assert "不要静默丢弃已解析出的资源意图" in body


def test_architecture_keeps_iac_code_web_candidate_on_fixed_entry_topology():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "单 ECS + EIP" in body
    assert "安全组仅开放 8766" in body
    assert "不得增加其他入口资源" in body
    assert "`candidate.name` 固定为 `iac-code-web-single-ecs`" in body
