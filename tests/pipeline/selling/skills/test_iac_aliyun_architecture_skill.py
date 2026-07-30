from __future__ import annotations

import json
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


def test_architecture_treats_undeclared_resources_as_create():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "未声明的资源默认是新建" in body
    assert "默认按 `action=create` 处理" in body
    assert "没有显式声明就当作本次新建" in body
    assert "不得因为某个资源“通常已经存在”" in body


def test_architecture_requires_both_candidates_when_dependency_lifecycle_unconfirmed():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "依赖资源生命周期未确认时的候选覆盖" in body
    assert "必须给出至少两个候选" in body
    assert "新建 VPC + 新建 VSwitch" in body
    assert "复用已有 VPC + 新建 VSwitch" in body
    assert "non_functional.network_constraints" in body
    assert "按确认的语义收敛为单候选" in body
    assert "不要让两个候选共用同一份继承自 intent 的 `resource_intents`" in body


def test_architecture_simple_requirement_shortcut_does_not_bypass_lifecycle_coverage():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    prompt = PROMPT_FILE.read_text(encoding="utf-8")

    assert "资源清单和生命周期都已确定" in body
    assert "不能因为资源数量少就收敛成单候选" in body
    assert "至少给出 2 个方案" in prompt
    assert "不要预设它是已有资源" in prompt


def test_architecture_evals_cover_unconfirmed_and_confirmed_vpc_lifecycle():
    data = json.loads((SKILL_DIR / "evals.json").read_text(encoding="utf-8"))
    evals_by_name = {ev["name"]: ev for ev in data["evals"]}

    unconfirmed = evals_by_name["vpc-vswitch-lifecycle-unconfirmed"]
    assert unconfirmed["intent_context"]["core_requirements"] == ["VPC", "VSwitch"]
    assert all(item["action"] == "create" for item in unconfirmed["intent_context"]["resource_intents"])
    assertion_names = {assertion["name"] for assertion in unconfirmed["assertions"]}
    assert "covers_vpc_creation" in assertion_names
    assert "no_presumed_existing_vpc" in assertion_names

    confirmed = evals_by_name["vswitch-in-confirmed-existing-vpc"]
    vpc_intent = next(item for item in confirmed["intent_context"]["resource_intents"] if item["product"] == "VPC")
    assert vpc_intent["action"] == "use_existing"
    assert "single_candidate" in {assertion["name"] for assertion in confirmed["assertions"]}
