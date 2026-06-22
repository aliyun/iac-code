from __future__ import annotations

import json
from pathlib import Path

import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "iac_code" / "pipeline" / "selling" / "skills" / "iac-aliyun-intent"
)
PROMPT_FILE = (
    Path(__file__).resolve().parents[4] / "src" / "iac_code" / "pipeline" / "selling" / "prompts" / "intent_parsing.md"
)


def _parse_frontmatter(text: str) -> dict:
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter"
    end = text.index("---", 3)
    return yaml.safe_load(text[3:end])


def test_intent_skill_mentions_ask_user_question_for_low_confidence():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "ask_user_question" in body
    assert "low" in body
    assert "同一个 AgentLoop" in body
    assert "complete_step" in body


def test_intent_skill_defines_non_iac_guidance_boundary():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "非部署/非基础设施" in body
    assert "纯代码" in body
    assert "纯知识" in body
    assert "不要反复询问" in body


def test_intent_prompt_requires_question_before_completion_for_ambiguous_guidable_inputs():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "ask_user_question" in body
    assert "同一个 AgentLoop" in body
    assert "不要回退" in body
    assert "必须先调用 `ask_user_question`" in body
    assert "可以先调用 `ask_user_question`" not in body
    assert 'complete_step` 的参数必须是 `{"conclusion": {...}}' in body
    assert "不要把 `is_infra_intent`" in body


def test_intent_prompt_guides_optional_memory_lookup_without_overriding_current_input():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "不要读取项目文件或记忆" not in body
    assert "read_memory({})" in body
    assert "已有资源" in body
    assert "当前用户输入为准" in body
    assert "不要因为没有相关记忆而阻塞" in body


def test_intent_prompt_pins_extremely_vague_launch_to_detail_request():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "极度模糊的上线意图" in body
    assert "请直接输入这个项目是什么，以及希望怎么上线；如果不是部署需求，可以选择暂不处理。" in body
    vague_section = body.split("极度模糊的上线意图", 1)[1].split("已有明确部署对象", 1)[0]
    assert '"id": "provide_details"' not in vague_section
    assert '"id": "economy"' not in vague_section
    assert '"id": "balanced"' not in vague_section
    assert '"id": "high_availability"' not in vague_section
    assert "不要询问用户是否要使用 IaC" in body


def test_intent_prompt_requires_dynamic_contextual_questions_for_known_deployment_objects():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "已有明确部署对象但仍缺少关键信息" in body
    assert "动态生成" in body
    assert "不要固定询问经济型/均衡/高可用" in body
    known_object_section = body.split("已有明确部署对象但仍缺少关键信息", 1)[1].split("对非部署/非云资源输入", 1)[0]
    assert '"id": "economy"' not in known_object_section
    assert '"id": "balanced"' not in known_object_section
    assert '"id": "high_availability"' not in known_object_section


def test_intent_prompt_guides_non_deployment_users_to_reenter_deployment_need():
    body = PROMPT_FILE.read_text(encoding="utf-8")

    assert "非部署/非云资源输入" in body
    assert "重新输入要部署的应用、服务或网站" in body
    assert "选项 id 必须由当前问题动态生成" in body


def test_intent_skill_makes_guidable_website_requests_mandatory_questions():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "仅描述“做网站/做应用/做小程序/上线项目”" in body
    assert "必须先调用 `ask_user_question`，不得直接调用 `complete_step`" in body
    assert "只有明确包含阿里云资源，或同时包含部署目标与足够的运维约束" in body
    assert "不要把这类输入提升为 `confidence: medium` 后直接完成" in body


def test_intent_schema_allows_clarification_result_fields():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "clarification_choice" in body
    assert "clarification_text" in body
    assert "selected_id" in body
    assert "free_text" in body
    schema = body.split("conclusion_schema:", 1)[1].split("---", 1)[0]
    assert "enum: [provide_details" not in schema


def test_intent_schema_captures_resource_lifecycle_fields():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    schema = _parse_frontmatter(body)["conclusion_schema"]
    properties = schema["properties"]

    resource_intents = properties["resource_intents"]
    assert resource_intents["type"] == "array"
    item_properties = resource_intents["items"]["properties"]
    assert set(resource_intents["items"]["required"]) == {"product", "action"}
    assert item_properties["product"]["type"] == "string"
    assert item_properties["action"]["enum"] == ["create", "use_existing", "reference", "forbid"]
    assert item_properties["role"]["type"] == "string"
    assert item_properties["source"]["type"] == "string"
    assert "forbidden_resources" not in properties


def test_intent_schema_captures_stack_name_and_network_constraints_without_e2e_controls():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    schema = _parse_frontmatter(body)["conclusion_schema"]
    non_functional = schema["properties"]["non_functional"]["properties"]

    assert non_functional["stack_name"]["type"] == "string"
    assert "资源栈名称" in non_functional["stack_name"]["description"]
    assert non_functional["network_constraints"]["type"] == "object"
    assert "deployment_hold" not in non_functional
    assert "non_functional.stack_name" in prompt
    assert "non_functional.network_constraints" in prompt
    assert "deployment_hold" not in body
    assert "部署后等待用户继续" not in body
    assert "CreateStack 的 params.StackName" not in prompt
    assert "first/second" not in body
    assert "first/second" not in prompt


def test_intent_guidance_preserves_existing_resource_lifecycle():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "resource_intents" in body
    assert 'action: "use_existing"' in body
    assert 'action: "create"' in body
    assert 'action: "forbid"' in body
    assert "已有 VPC" in body
    assert '{"product": "SecurityGroup", "action": "create"}' in body
    assert '{"product": "VPC", "action": "use_existing"}' in body
    assert "不要只把已有资源写进 core_requirements" in body
    assert "forbidden_resources" not in body


def test_intent_skill_only_supports_aliyun_and_rejects_other_clouds():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    prompt = PROMPT_FILE.read_text(encoding="utf-8")

    assert "只支持阿里云" in body
    assert "非阿里云" in body
    assert "只支持阿里云" in prompt
    assert "情况 C — 基础设施需求但指定非阿里云平台" not in body
    assert "cloud_platform: aws" not in (SKILL_DIR / "evals.json").read_text(encoding="utf-8")
    assert "未指定非阿里云平台" in body


def test_guidance_evals_have_structured_ask_user_question_assertions():
    data = json.loads((SKILL_DIR / "evals.json").read_text(encoding="utf-8"))
    evals_by_id = {ev["id"]: ev for ev in data["evals"]}

    expected_eval_ids = {2, 3, 4, 5, 11, 12}

    for eval_id in expected_eval_ids:
        assertions = evals_by_id[eval_id]["assertions"]

        assert any(assertion.get("tool") == "ask_user_question" for assertion in assertions)
    eval_text = (SKILL_DIR / "evals.json").read_text(encoding="utf-8")
    assert "provide_details" not in eval_text
    assert "economy" not in eval_text
    assert "balanced" not in eval_text
    assert "high_availability" not in eval_text


def test_guidance_evals_cover_dynamic_known_object_and_clear_aliyun_request():
    data = json.loads((SKILL_DIR / "evals.json").read_text(encoding="utf-8"))
    evals_by_id = {ev["id"]: ev for ev in data["evals"]}

    assert evals_by_id[12]["prompt"] == "nginx 网站想上线"
    assert any(assertion.get("tool") == "ask_user_question" for assertion in evals_by_id[12]["assertions"])
    assert "动态澄清" in evals_by_id[12]["expected_output"]

    assert "阿里云" in evals_by_id[13]["prompt"]
    assert not any(assertion.get("tool") == "ask_user_question" for assertion in evals_by_id[13]["assertions"])
    assert any(assertion.get("field") == "cloud_platform" for assertion in evals_by_id[13]["assertions"])
