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


def test_architecture_requires_create_intents_to_be_fulfilled_in_candidates():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "每个 `action=create` 的资源必须在每个 candidate 中兑现为新建资源" in body
    assert "不得把它降级为 `use_existing`/`reference`" in body
    assert "不允许改写、降级或删除 intent 中已有的 `action=create` 条目" in body
    assert "不要静默降级" in body
    assert "每个 candidate 都必须同时新建 VPC 和 VSwitch" in body
    assert "让用户的 VPC 创建意图落空" in body


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
