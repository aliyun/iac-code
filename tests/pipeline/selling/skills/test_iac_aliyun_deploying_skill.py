import json
from pathlib import Path

import jsonschema
import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "iac_code"
    / "pipeline"
    / "selling"
    / "skills"
    / "iac-aliyun-deploying"
)
SKILL_MD = SKILL_DIR / "SKILL.md"
EVALS_JSON = SKILL_DIR / "evals.json"
DEPLOYING_PROMPT_MD = SKILL_DIR.parents[1] / "prompts" / "deploying.md"


def _parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter from SKILL.md."""
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter"
    end = text.index("---", 3)
    return yaml.safe_load(text[3:end])


class TestSkillFrontmatter:
    def test_skill_file_exists(self):
        assert SKILL_MD.exists()

    def test_has_valid_frontmatter(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert "name" in fm
        assert "description" in fm

    def test_name_is_correct(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert fm["name"] == "iac-aliyun-deploying"

    def test_not_user_invocable(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert fm.get("user_invocable") is False

    def test_description_mentions_deploy(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert "部署" in fm["description"]

    def test_description_mentions_ros(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert "ROS" in fm["description"]

    def test_conclusion_schema_requires_stack_id_for_success_and_error_for_failed(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        schema = fm["conclusion_schema"]

        jsonschema.validate({"status": "success", "stack_id": "stack-123"}, schema)
        jsonschema.validate({"status": "failed", "error": "CREATE_FAILED"}, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"status": "success"}, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"status": "failed"}, schema)


class TestSkillContentRosOnly:
    @pytest.fixture()
    def body(self) -> str:
        content = SKILL_MD.read_text(encoding="utf-8")
        end = content.index("---", 3) + 3
        return content[end:]

    def test_no_terraform_references(self, body):
        lower = body.lower()
        assert "terraform" not in lower
        assert ".tf" not in lower
        assert "tf2ros" not in lower

    def test_contains_ros_deploy_without_direct_ros_stack(self, body):
        assert "ros_deploy" in body
        assert "ros_stack" not in body

    def test_contains_availability_query(self, body):
        assert "可用性查询" in body

    def test_deploying_can_complete_parameters_without_pricing_or_user_questions(self, body):
        assert "部署参数装配" in body
        assert "selected_plan.effective_deployment_parameters" in body
        assert "ros_deploy" in body
        assert "ros_get_template_parameter_constraints" in body
        assert "预览工具" in body
        assert "ros_preview_template" not in body
        assert "ros_estimate_template_cost" not in body
        assert "ask_user_question" not in body

    def test_missing_parameters_are_not_a_direct_failure_reason(self, body):
        assert "仍缺少模板必填参数" in body
        assert "不得仅因部署参数缺失返回 `status: failed`" in body
        assert "先尽量补齐或生成参数" in body
        assert "普通密码" in body

    def test_create_stack_name_has_random_suffix(self, body):
        assert "StackName" in body
        assert "随机串后缀" in body
        assert "避免重名" in body

    def test_prefers_cost_deployment_parameters(self, body):
        assert "selected_plan.selected_candidate_result.cost.deployment_parameters" in body
        assert "按以下优先级" in body
        assert "前序成本步骤沉淀的 Default" not in body

    def test_prefers_effective_deployment_parameters(self, body):
        assert "selected_plan.effective_deployment_parameters" in body
        assert "作为当前参数基础" in body
        assert "不得因它非空就视为完整" in body

    def test_does_not_ask_user_for_missing_region_or_parameters(self, body):
        assert "请用户指定目标地域" not in body
        assert "不发起澄清问题" in body
        assert "不要向用户发起澄清问题" in body

    def test_availability_conflict_prefers_non_user_parameters_first(self, body):
        assert "优先调整非用户指定参数" in body
        assert "仍无法成功创建资源栈" in body
        assert "才可调整用户指定参数" in body

    def test_skill_omits_discussion_process_terms(self, body):
        forbidden = ["A2A", "前端", "客户端", "方案 A", "方案 B", "策略 A", "策略 B", "讨论"]
        for phrase in forbidden:
            assert phrase not in body

    def test_does_not_mention_stack_instances(self, body):
        assert "CreateStackInstances" not in body
        assert "UpdateStackInstances" not in body

    def test_contains_template_validation(self, body):
        assert "ros_validate_template" in body
        assert "模板校验" in body

    def test_dedicated_tool_boundary_covers_validation_and_deploy_lifecycle(self, body):
        assert "不要通过 `aliyun_api` 调用 ROS 模板校验或部署生命周期接口" in body
        assert "ROS `ValidateTemplate` 接口" not in body

    def test_preview_ready_path_skips_routine_validation_and_availability(self, body):
        assert body.count("selected_plan.preview_ready_for_create") == 1
        assert "`ros_deploy` 的 `create`" in body
        assert "跳过例行 `ros_validate_template`" in body
        assert "跳过例行可用性查询" in body

    def test_create_stack_failure_revalidates_after_template_change_only(self, body):
        assert "`create` 失败" in body
        assert "修改模板" in body
        assert "修改模板或部署参数" not in body
        assert "只调整部署参数" in body
        assert "重新调用 `ros_validate_template`" in body
        assert "`continue_create`" in body

    def test_template_repairs_stay_on_selected_template_path(self, body):
        assert "selected_plan.template_url" in body
        assert "不得写入新的模板文件" in body
        assert "不得改用新的模板路径" in body
        assert "edit_file" in body
        assert "write_file" not in body

    def test_no_pricing_section(self, body):
        assert "GetTemplateEstimateCost" not in body
        assert "部署前询价" not in body
        assert "ros_estimate_template_cost" not in body

    def test_deploy_tool_create_documents_disable_rollback(self, body):
        assert "`ros_deploy` 的 `create`" in body
        assert "DisableRollback" in body

    def test_template_url_source_is_not_duplicated_from_prompt(self, body):
        assert "模板来源硬约束" not in body
        assert "prompt 中已渲染" not in body
        assert "不要传 `params.TemplateBody`" not in body
        assert "<选中方案模板文件路径>" not in body
        assert "template_url=<模板文件路径>" not in body
        assert "region_id=<地域>" not in body

    def test_contains_continue_create(self, body):
        assert "ContinueCreateStackValidationFailed" in body

    def test_delete_and_create_documents_replacement_flow(self, body):
        assert "仅在 `continue_create` 返回 `ContinueCreateStackValidationFailed` 后使用 `delete_and_create`" in body
        assert "先确认替代创建参数和模板可用" in body
        assert "删除旧失败 Stack 后创建新的 Stack" in body
        assert "最终结果使用新 Stack 的 `stack_id`" in body
        assert "不要把旧 `stack_id` 当成部署成功结果" in body

    def test_does_not_present_raw_stack_lifecycle_api_as_call_target(self, body):
        body_without_error_codes = body.replace("ContinueCreateStackValidationFailed", "")
        forbidden_phrases = [
            "CreateStack 前",
            "传给 `CreateStack`",
            "CreateStack 必须传",
            "CreateStack 无法成功",
            "最终参数由 CreateStack 校验",
            "| CreateStack |",
        ]
        for phrase in forbidden_phrases:
            assert phrase not in body_without_error_codes

    def test_contains_error_handling(self, body):
        assert "部署失败" in body

    def test_no_template_generation(self, body):
        assert "模板生成流程" not in body
        assert "参数化规则" not in body

    def test_no_explanation_section(self, body):
        assert "解释/完善模板" not in body

    def test_references_exclude_terraform(self, body):
        assert "ros-template.md" in body
        assert "terraform-template.md" not in body

    def test_ros_only_doc_search(self, body):
        assert "category_id=28850" in body
        assert "category_id=95817" not in body

    def test_pipeline_confirmed_deploy_does_not_ask_again(self, body):
        assert "pipeline 已完成部署确认" in body
        assert "不要再次请求用户确认" in body
        assert "不得用 status: cancelled 表示等待用户确认" in body

    def test_delete_requires_explicit_delete_confirmation(self, body):
        assert "非本步骤创建的 Stack" in body
        assert "确认删除" in body
        assert "delete_and_create" in body
        assert "未收到明确删除确认前，不得调用 `ros_stack` 的 `DeleteStack`" not in body


class TestDeployingPrompt:
    def test_pipeline_confirmed_deploy_is_direct_execution(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert "不要再次询问是否确认部署" in body
        assert "不得用 status: cancelled 表示等待用户确认" not in body
        assert "只有用户明确取消部署时" not in body

    def test_prompt_defers_parameter_priority_to_skill(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert "selected_plan.selected_candidate_result.cost.deployment_parameters" not in body
        assert "部署参数按以下优先级装配" not in body
        assert "部署参数装配规则见技能" in body

    def test_prompt_keeps_no_repricing_without_parameter_priority_duplication(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert "部署步骤不计算费用" in body
        assert "selected_plan.effective_deployment_parameters" not in body
        assert "GetTemplateEstimateCost" not in body

    def test_prompt_does_not_repeat_parameter_adjustment_rules(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert "优先调整非用户指定参数" not in body
        assert "仍无法成功创建资源栈" not in body
        assert "才可调整用户指定参数" not in body
        assert "可用区不可用 → 自动更换可用区重试" not in body

    def test_prompt_omits_discussion_process_terms(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        forbidden = ["A2A", "前端", "客户端", "方案 A", "方案 B", "策略 A", "策略 B", "讨论"]
        for phrase in forbidden:
            assert phrase not in body

    def test_prompt_defers_delete_scope_to_skill(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert "ros_deploy" in body
        assert "delete_and_create" in body
        assert "删除约束和失败恢复策略见技能" in body
        assert "非本步骤创建的 Stack" not in body
        assert "未收到明确删除确认前，不得调用 `ros_stack` 的 `DeleteStack`" not in body

    def test_prompt_does_not_mention_uninjected_ros_tools(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert "ros_stack" not in body
        assert "ros_preview_template" not in body

    def test_prompt_names_template_url_value_for_deploy_tools(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert 'template_url = "{selected_plan.template_url}"' in body
        assert 'params.TemplateURL = "{selected_plan.template_url}"' not in body
        assert "selected_plan.template_url" in body
        assert "不得另写新模板文件" in body
        assert "不得把新文件路径传给部署工具" in body
        assert "ros_validate_template" in body
        assert "ros_deploy" in body
        assert "不要通过 `aliyun_api` 调用 ROS 模板校验或部署生命周期接口" in body
        assert "ROS `ValidateTemplate` 接口" not in body
        assert "不要传 `TemplateBody`" in body
        assert "<选中方案模板文件路径>" not in body

    def test_prompt_does_not_present_raw_stack_lifecycle_api_as_call_target(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        body_without_error_codes = body.replace("ContinueCreateStackValidationFailed", "")
        assert "CreateStack" not in body_without_error_codes

    def test_prompt_allows_preview_ready_direct_create(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert "`selected_plan.preview_ready_for_create` 为 `true`" in body
        assert "快速创建路径见技能" in body
        assert "跳过例行 `ros_validate_template`" not in body


class TestSkillDiscovery:
    def test_discovered_by_pipeline_loader(self):
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        pipeline_dir = SKILL_DIR.parents[1]
        loaded = load_pipeline_dir(pipeline_dir)
        assert "iac-aliyun-deploying" in loaded.skills

    def test_skill_content_matches_file(self):
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        pipeline_dir = SKILL_DIR.parents[1]
        loaded = load_pipeline_dir(pipeline_dir)
        expected = SKILL_MD.read_text(encoding="utf-8")
        assert loaded.skills["iac-aliyun-deploying"] == expected


class TestEvalsJson:
    def test_evals_file_exists(self):
        assert EVALS_JSON.exists()

    def test_valid_json(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_has_required_fields(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        assert data["skill_name"] == "iac-aliyun-deploying"
        assert "evals" in data
        assert len(data["evals"]) > 0

    def test_each_eval_has_structure(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        for ev in data["evals"]:
            assert "id" in ev
            assert "name" in ev
            assert "prompt" in ev
            assert "assertions" in ev
            assert len(ev["assertions"]) > 0

    def test_all_evals_are_deploy_focused(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        deploy_keywords = ["部署", "删除", "更新", "Stack", "失败", "可用区"]
        for ev in data["evals"]:
            prompt = ev["prompt"]
            behavior = ev["expected_behavior"]
            combined = prompt + behavior
            assert any(kw in combined for kw in deploy_keywords), (
                f"Eval '{ev['name']}' does not appear deployment-focused"
            )

    def test_no_terraform_in_evals(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        for ev in data["evals"]:
            prompt_lower = ev["prompt"].lower()
            assert "terraform" not in prompt_lower

    def test_evals_cover_preview_ready_fast_path(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        eval_text = json.dumps(data, ensure_ascii=False)
        assert "preview_ready_for_create" in eval_text
        assert "跳过例行" in eval_text

    def test_assertions_have_name_and_check(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        for ev in data["evals"]:
            for assertion in ev["assertions"]:
                assert "name" in assertion
                assert "check" in assertion

    def test_eval_ids_are_unique(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        ids = [ev["id"] for ev in data["evals"]]
        assert len(ids) == len(set(ids))

    def test_eval_names_are_unique(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        names = [ev["name"] for ev in data["evals"]]
        assert len(names) == len(set(names))

    def test_delete_evals_split_confirmation_and_execution(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        evals_by_name = {ev["name"]: ev for ev in data["evals"]}

        confirmation_eval = evals_by_name["delete-stack-confirmation"]
        confirmation_assertions = {assertion["name"] for assertion in confirmation_eval["assertions"]}
        assert "reports_scope" in confirmation_assertions
        assert "uses_delete_stack" not in confirmation_assertions
        assert "no_delete_stack" in confirmation_assertions
        assert "no_delete_and_create" in confirmation_assertions

        confirmed_eval = evals_by_name["delete-stack-confirmed"]
        confirmed_assertions = {assertion["name"] for assertion in confirmed_eval["assertions"]}
        assert "确认" in confirmed_eval["prompt"]
        assert "uses_delete_stack" not in confirmed_assertions
        assert "no_delete_stack" in confirmed_assertions

    def test_template_validation_eval_uses_dedicated_tool(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        evals_by_name = {ev["name"]: ev for ev in data["evals"]}
        validation_eval = evals_by_name["template-validation-fix-before-deploy"]
        eval_text = json.dumps(validation_eval, ensure_ascii=False)

        assert "ros_validate_template" in eval_text
        assert "aliyun_api ValidateTemplate" not in eval_text
