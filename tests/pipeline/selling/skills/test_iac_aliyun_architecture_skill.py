from __future__ import annotations

from pathlib import Path

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


def test_architecture_prompt_guides_optional_memory_lookup_for_planning_context():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "不要读取项目文件或记忆" not in body
    assert "read_memory({})" in body
    assert "架构偏好" in body
    assert "已有 VPC" in body
    assert "当前用户意图为准" in body


def test_architecture_skill_requires_candidate_spec_tiering():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "候选间规格分层" in body
    assert "planned_specs" in body
    assert "禁止" in body and "核心 ECS/RDS 规格完全一致" in body
    assert "economy" in body and "balanced" in body and "performance" in body


def test_architecture_conclusion_schema_carries_planned_specs():
    import yaml

    content = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---")
    end = content.index("---", 3)
    fm = yaml.safe_load(content[3:end])
    schema = fm["conclusion_schema"]
    item_schema = schema["properties"]["candidates"]["items"]
    assert "planned_specs" in item_schema["properties"]
    planned = item_schema["properties"]["planned_specs"]
    assert planned["type"] == "array"
    assert set(planned["items"]["required"]) == {"product", "spec"}
    assert "tier" in planned["items"]["properties"]


def test_architecture_prompt_requires_spec_tiering():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "规格分层" in body
    assert "planned_specs" in body
    assert "禁止多候选使用完全相同的核心规格" in body

