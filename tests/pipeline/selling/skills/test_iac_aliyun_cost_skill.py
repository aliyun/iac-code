import json
from pathlib import Path

import jsonschema
import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "iac_code" / "pipeline" / "selling" / "skills" / "iac-aliyun-cost"
)
SKILL_MD = SKILL_DIR / "SKILL.md"
EVALS_JSON = SKILL_DIR / "evals.json"
COST_PROMPT_MD = SKILL_DIR.parents[1] / "prompts" / "cost_estimating.md"


def _direct_references_dir_or_skip() -> Path:
    references = SKILL_DIR / "references"
    if not references.is_dir():
        pytest.skip("references is a Windows symlink placeholder file in this checkout")
    return references


def _is_reference_link_or_placeholder(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.is_file():
        return False
    try:
        raw_target = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    if not raw_target or "\n" in raw_target or "\r" in raw_target:
        return False
    return (path.parent / raw_target).exists()


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
        assert fm["name"] == "iac-aliyun-cost"

    def test_not_user_invocable(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert fm.get("user_invocable") is False

    def test_description_mentions_ros(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert "ROS" in fm["description"]

    def test_description_mentions_cost(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert "GetTemplateEstimateCost" in fm["description"] or "费用" in fm["description"]

    def test_conclusion_schema_carries_deployment_parameters(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        schema = fm["conclusion_schema"]
        assert "deployment_parameters" in schema["required"]
        assert schema["properties"]["deployment_parameters"]["type"] == "object"

    def test_conclusion_schema_can_report_missing_deployment_parameters(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        schema = fm["conclusion_schema"]
        assert "missing_deployment_parameters" in schema["properties"]
        assert schema["properties"]["missing_deployment_parameters"]["type"] == "array"

    def test_conclusion_schema_can_report_preview_validation_proof(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        schema = fm["conclusion_schema"]
        preview_schema = schema["properties"]["preview_validation"]

        assert preview_schema["type"] == "object"
        assert preview_schema["properties"]["succeeded"]["type"] == "boolean"
        assert preview_schema["properties"]["template_url"]["type"] == "string"
        assert preview_schema["properties"]["parameters"]["type"] == "object"

    def test_monthly_estimate_schema_describes_list_and_discounted_prices(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        description = fm["conclusion_schema"]["properties"]["monthly_estimate"]["description"]

        assert "列表价" in description
        assert "合同优惠后" in description
        assert "OriginalAmount" in description
        assert "TradeAmount" in description

    def test_conclusion_schema_requires_full_preview_validation_when_succeeded(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        schema = fm["conclusion_schema"]
        conclusion = {
            "monthly_estimate": "¥100/月",
            "currency": "CNY",
            "resources": [{"type": "ALIYUN::ECS::InstanceGroup", "cost": "¥100/月"}],
            "template_fixed": False,
            "deployment_parameters": {"ZoneId": "cn-hangzhou-k"},
            "preview_validation": {
                "succeeded": True,
                "template_url": "templates/a.yml",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
        }

        jsonschema.validate(conclusion, schema)
        missing_template_url = dict(conclusion, preview_validation={"succeeded": True, "parameters": {}})
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(missing_template_url, schema)
        missing_parameters = dict(conclusion, preview_validation={"succeeded": True, "template_url": "templates/a.yml"})
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(missing_parameters, schema)

    def test_conclusion_schema_requires_error_when_preview_validation_failed(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        schema = fm["conclusion_schema"]
        conclusion = {
            "monthly_estimate": "询价失败",
            "currency": "CNY",
            "resources": [],
            "template_fixed": False,
            "deployment_parameters": {},
            "preview_validation": {"succeeded": False, "error": "missing VpcId"},
        }

        jsonschema.validate(conclusion, schema)
        missing_error = dict(conclusion, preview_validation={"succeeded": False})
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(missing_error, schema)

    def test_preview_validation_can_record_recovery_path(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        schema = fm["conclusion_schema"]
        conclusion = {
            "monthly_estimate": "¥100/月",
            "currency": "CNY",
            "resources": [{"type": "ALIYUN::ECS::InstanceGroup", "cost": "¥100/月"}],
            "template_fixed": False,
            "deployment_parameters": {"ZoneId": "cn-hangzhou-k"},
            "preview_validation": {
                "succeeded": True,
                "template_url": "templates/a.yml",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
                "recovered": True,
                "failure_history": [
                    {
                        "error": "body_file invalid",
                        "error_code": "InvalidTemplateBody",
                        "resolution": "修复模板后重试预览",
                    }
                ],
            },
        }

        jsonschema.validate(conclusion, schema)

    def test_preview_validation_failure_history_items_require_error(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        schema = fm["conclusion_schema"]
        conclusion = {
            "monthly_estimate": "¥100/月",
            "currency": "CNY",
            "resources": [{"type": "ALIYUN::ECS::InstanceGroup", "cost": "¥100/月"}],
            "template_fixed": False,
            "deployment_parameters": {"ZoneId": "cn-hangzhou-k"},
            "preview_validation": {
                "succeeded": True,
                "template_url": "templates/a.yml",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
                "recovered": True,
                "failure_history": [{"resolution": "重试"}],
            },
        }

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(conclusion, schema)


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

    def test_uses_estimate_cost_tool_not_raw_call_instruction(self, body):
        assert "ros_estimate_template_cost" in body
        assert "则可以调用 `GetTemplateEstimateCost`" not in body

    def test_contains_validate_template(self, body):
        assert "ros_validate_template" in body

    def test_template_path_examples_use_the_working_directory(self, body):
        assert "`./template.yml`" in body
        assert 'template_url="./ros-template.yml"' in body
        assert "/tmp/" not in body

    def test_skips_validate_template_when_template_unchanged(self, body):
        assert "避免在成本预估前重复校验" in body

    def test_validate_template_only_after_template_changes(self, body):
        assert "只有在修复或改写模板后，才调用 `ros_validate_template`" in body

    def test_modified_template_flow_names_validation_and_pricing_apis(self, body):
        assert "调用 `ros_validate_template` 校验改动" in body
        assert "通过后调用 `ros_estimate_template_cost` 重新询价" in body

    def test_modified_template_retry_limit_is_seven(self, body):
        assert "最多 7 轮" in body

    def test_validate_template_policy_is_not_repeated(self, body):
        assert body.count("只有在修复或改写模板后") == 1

    def test_uses_parameters_dictionary_auto_expansion(self, body):
        assert "parameters={" in body
        assert "直接传字典格式" in body
        assert "Parameters.1.ParameterKey" not in body

    def test_outputs_pricing_parameters_for_deployment(self, body):
        assert "deployment_parameters" in body
        assert "传递给 deploying" in body
        assert "写入模板 Parameters 的 `Default`" not in body
        assert "沉淀参数默认值" not in body

    def test_contains_parameter_recommendation_flow(self, body):
        assert "Pricing Parameter Set" in body
        assert "Preview-Validated Pricing Parameter Set" in body
        assert "references/template-parameter-recommendation.md" in body
        assert "ros_get_template_parameter_constraints" in body
        assert "PreviewStack" in body
        assert "AllowedValues" in body
        assert "不得编造" in body
        assert "外部输入" in body
        assert "不执行 `PreviewStack`" not in body
        assert "写回模板的 Default 保持一致" not in body

    def test_existing_resource_parameters_can_use_api_candidates(self, body):
        assert "VpcId、VSwitchId、SecurityGroupId、KeyPairName" in body
        assert "API 返回候选不是编造" in body
        assert "先查询约束或只读资源候选" in body
        assert "不要仅因参数名是 VpcId" in body

    def test_preview_stack_uses_dedicated_tool_not_ros_stack(self, body):
        assert "ros_preview_template" in body
        assert "不要使用 `ros_stack` 执行 `PreviewStack`" in body

    def test_preview_stack_must_pass_stack_name_with_random_suffix(self, body):
        assert "PreviewStack 必须传 StackName" in body
        assert "stack_name" in body
        assert "随机串后缀" in body
        assert "避免重名" in body

    def test_parameter_recommendation_precedes_initial_pricing(self, body):
        assert "先直接询价" not in body
        assert "首次询价前" in body
        assert "形成 Preview-Validated Pricing Parameter Set" in body

    def test_parameter_recommendation_pushes_for_complete_deployment_parameters(self, body):
        assert "尽量形成完整部署参数集" in body
        assert "不要过早把可补齐参数列入 `missing_deployment_parameters`" in body
        assert "普通密码" in body
        assert "生成合规随机值" in body
        assert "仍需用户补充" not in body
        assert "需要用户在后续选择阶段补充" not in body
        assert "deploying 也可继续补齐" in body

    def test_preserves_preview_parameters_when_pricing_fails(self, body):
        assert "PreviewStack 成功但询价失败" in body
        assert "不要丢弃 Preview-Validated Pricing Parameter Set" in body
        assert "询价失败或外部输入缺失时填 `{}`" not in body

    def test_records_preview_validation_for_downstream_create_stack_fast_path(self, body):
        assert "preview_validation" in body
        assert "template_url" in body
        assert "parameters" in body
        assert "deploying" in body

    def test_body_requires_recovery_path_when_preview_recovered(self, body):
        assert "recovered: true" in body
        assert "failure_history" in body
        assert "失败-重试-恢复路径" in body

    def test_preview_stack_is_not_hard_gate_for_pricing(self, body):
        assert "PreviewStack 不是硬门禁" in body
        assert "完整部署参数" in body
        assert "ros_estimate_template_cost" in body
        assert "missing_deployment_parameters" in body
        assert "选择阶段" in body and "parameter_overrides" in body

    def test_contains_template_url(self, body):
        assert "template_url" in body

    def test_template_url_source_is_not_duplicated_from_prompt(self, body):
        assert "模板来源硬约束" not in body
        assert "params.TemplateURL = <当前模板文件路径>" not in body
        assert "不要传 `params.TemplateBody`" not in body
        assert 'template_url="<模板文件路径>"' not in body
        assert 'region_id="<地域>"' not in body

    def test_contains_fix_workflow(self, body):
        assert "修复" in body or "fix" in body.lower()

    def test_contains_output_format(self, body):
        assert "monthly_estimate" in body
        assert "complete_step" in body

    def test_no_doc_search_recommendation(self, body):
        assert "aliyun_doc_search" in body
        lower_lines = body.lower().split("\n")
        for line in lower_lines:
            if "aliyun_doc_search" in line:
                assert "不要" in line or "不" in line or "禁" in line

    def test_does_not_inline_common_resource_catalog(self, body):
        assert "### 常用资源类型" not in body
        assert "ALIYUN::ECS::VPC — 专有网络" not in body
        assert "ALIYUN::ECS::InstanceGroup — ECS 实例" not in body

    def test_parameterization_details_stay_in_references(self, body):
        assert "### 参数化规则" not in body
        assert "| ECS | ZoneId, InstanceType" not in body
        assert "references/template-parameters.md" in body

    def test_contains_error_handling(self, body):
        assert "失败" in body

    def test_emphasizes_write_back(self, body):
        assert "写回原文件路径" in body

    def test_emphasizes_downstream_dependency(self, body):
        assert "后续" in body and ("部署" in body or "步骤" in body)

    def test_must_not_skip_fix(self, body):
        assert "不要跳过修复" in body

    def test_references_cloud_products(self, body):
        assert "references/cloud-products/" in body

    def test_references_template_parameters(self, body):
        assert "references/template-parameters.md" in body

    def test_references_ros_template(self, body):
        assert "references/ros-template.md" in body

    def test_no_terraform_template_reference(self, body):
        assert "terraform-template.md" not in body


class TestReferencesExist:
    def test_references_is_symlink(self):
        ref = SKILL_DIR / "references"
        if ref.is_symlink():
            return
        assert ref.is_file(), "Windows checkouts may materialize references as a regular symlink placeholder file"

    def test_references_points_to_selling_references(self):
        ref = SKILL_DIR / "references"
        if not ref.is_symlink():
            pytest.skip("references is not a symlink in this checkout")
        target = str(ref.readlink()).replace("\\", "/")
        assert target == "../../references"

    def test_references_resolves_to_dir(self):
        assert _direct_references_dir_or_skip().resolve().is_dir()

    def test_selling_reference_overrides_only_template_parameter_recommendation(self):
        selling_refs = SKILL_DIR.parents[1] / "references"
        assert selling_refs.is_dir()
        assert (selling_refs / "template-parameter-recommendation.md").is_file()
        assert not (selling_refs / "template-parameter-recommendation.md").is_symlink()
        assert _is_reference_link_or_placeholder(selling_refs / "ros-template.md")
        assert _is_reference_link_or_placeholder(selling_refs / "template-parameters.md")
        assert _is_reference_link_or_placeholder(selling_refs / "cloud-products")

    def test_cloud_products_accessible(self):
        cloud_dir = _direct_references_dir_or_skip() / "cloud-products"
        assert cloud_dir.is_dir()
        files = list(cloud_dir.glob("*.md"))
        assert len(files) >= 3, f"expected at least 3 cloud product files, got {len(files)}"

    def test_ros_template_accessible(self):
        assert (_direct_references_dir_or_skip() / "ros-template.md").is_file()

    def test_template_parameters_accessible(self):
        assert (_direct_references_dir_or_skip() / "template-parameters.md").is_file()

    def test_template_parameter_recommendation_uses_dedicated_template_tools(self):
        reference = _direct_references_dir_or_skip() / "template-parameter-recommendation.md"
        content = reference.read_text(encoding="utf-8")

        assert "ros_get_template_parameter_constraints" in content
        assert "ros_preview_template" in content
        assert "ros_estimate_template_cost" in content
        assert 'action="GetTemplateParameterConstraints"' not in content
        assert 'action="PreviewStack"' not in content
        assert "调用 `GetTemplateEstimateCost`" not in content

    def test_template_parameter_recommendation_does_not_require_create_stack_to_reuse_preview_inputs(self):
        reference = _direct_references_dir_or_skip() / "template-parameter-recommendation.md"
        content = reference.read_text(encoding="utf-8")

        assert "`PreviewStack` 与后续 `CreateStack`" not in content
        assert "`CreateStack` 与 `PreviewStack`" not in content
        assert "同一地域" not in content
        assert "参数必须一致" not in content
        assert "最终参数由 `CreateStack` 校验" in content


class TestSkillDiscovery:
    def test_discovered_by_pipeline_loader(self):
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        pipeline_dir = SKILL_DIR.parents[1]
        loaded = load_pipeline_dir(pipeline_dir)
        assert "iac-aliyun-cost" in loaded.skills
        skill_root = Path(loaded.skill_roots["iac-aliyun-cost"])
        assert skill_root == SKILL_DIR.resolve()
        assert (skill_root / "references" / "ros-template.md").is_file()
        assert (skill_root / "references" / "template-parameters.md").is_file()

    def test_skill_content_matches_file(self):
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        pipeline_dir = SKILL_DIR.parents[1]
        loaded = load_pipeline_dir(pipeline_dir)
        expected = SKILL_MD.read_text(encoding="utf-8")
        assert loaded.skills["iac-aliyun-cost"] == expected


class TestCostPrompt:
    def test_prompt_is_not_duplicate_output_reference(self):
        body = COST_PROMPT_MD.read_text(encoding="utf-8")
        assert "Preview-Validated Pricing Parameter Set" in body
        assert "`deployment_parameters`" in body
        assert "不得写入 `***`、`[REDACTED]` 或 `<redacted>`" in body
        assert "询价失败但 PreviewStack 已成功" not in body
        assert "字段为字符串" not in body

    def test_prompt_names_preview_stack_tool_contract(self):
        body = COST_PROMPT_MD.read_text(encoding="utf-8")
        assert "ros_preview_template" in body
        assert "不要使用 `ros_stack` 执行预览" in body

    def test_prompt_treats_preview_stack_as_soft_gate(self):
        body = COST_PROMPT_MD.read_text(encoding="utf-8")
        assert "优先通过" in body
        assert "不是硬门禁" in body
        assert "参数缺口" in body

    def test_prompt_asks_model_to_complete_parameters_before_pricing(self):
        body = COST_PROMPT_MD.read_text(encoding="utf-8")
        assert "尽量形成完整部署参数集" in body
        assert "可生成参数" in body
        assert "普通密码" in body

    def test_prompt_records_preview_validation_for_deploying(self):
        body = COST_PROMPT_MD.read_text(encoding="utf-8")
        assert "preview_validation" in body
        assert "PreviewStack 成功证明" in body

    def test_prompt_requires_recovery_path_when_preview_recovered(self):
        body = COST_PROMPT_MD.read_text(encoding="utf-8")
        assert "recovered: true" in body
        assert "failure_history" in body
        assert "不得用最终成功掩盖恢复过程" in body

    def test_prompt_names_template_url_value_for_pricing_tools(self):
        body = COST_PROMPT_MD.read_text(encoding="utf-8")
        assert 'template_url = "{template.file_path}"' in body
        assert "ros_get_template_parameter_constraints" in body
        assert "ros_preview_template" in body
        assert "ros_estimate_template_cost" in body
        assert "不要传 `TemplateBody`" in body

    def test_prompt_requires_list_and_discounted_monthly_prices(self):
        body = COST_PROMPT_MD.read_text(encoding="utf-8")

        assert "OriginalAmount" in body
        assert "TradeAmount" in body
        assert "列表价" in body
        assert "合同优惠后" in body
        assert "monthly_estimate" in body


class TestEvalsJson:
    def test_evals_file_exists(self):
        assert EVALS_JSON.exists()

    def test_valid_json(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_evals_follow_parameter_dictionary_contract(self):
        text = EVALS_JSON.read_text(encoding="utf-8")
        assert "Parameters.<N>.ParameterKey" not in text
        assert "Parameters.1.ParameterKey" not in text
        assert "deployment_parameters" in text
        assert "preview_validation" in text

    def test_evals_do_not_require_validation_before_initial_pricing(self):
        text = EVALS_JSON.read_text(encoding="utf-8")
        assert "先校验" not in text

    def test_evals_keep_preview_parameters_on_pricing_failure(self):
        text = EVALS_JSON.read_text(encoding="utf-8")
        assert "PreviewStack 成功但询价失败" in text
        assert "不丢弃" in text

    def test_evals_assert_preview_stack_api_tool_contract(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        checks = "\n".join(assertion["check"] for ev in data["evals"] for assertion in ev["assertions"])
        assert "ros_preview_template" in checks
        assert "不使用 ros_stack" in checks

    def test_evals_cover_existing_vpc_parameter_recommendation(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        eval_text = json.dumps(data, ensure_ascii=False)
        assert "existing-vpc-vswitch-cost" in eval_text
        assert "ALIYUN::ECS::VPC::VPCId" in eval_text
        assert "VpcId" in eval_text
        assert "API 返回候选不是编造" in eval_text

    def test_evals_cover_preview_stack_soft_gate(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        eval_text = json.dumps(data, ensure_ascii=False)
        assert "preview-soft-gate-partial-pricing" in eval_text
        assert "PreviewStack 不是硬门禁" in eval_text
        assert "missing_deployment_parameters" in eval_text

    def test_has_required_fields(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        assert data["skill_name"] == "iac-aliyun-cost"
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

    def test_each_eval_has_template_context(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        for ev in data["evals"]:
            assert "template_context" in ev, f"eval {ev['name']} missing template_context"
            ctx = ev["template_context"]
            assert "template" in ctx, f"eval {ev['name']} template_context missing template"
            assert "file_path" in ctx, f"eval {ev['name']} template_context missing file_path"
            assert "region" in ctx, f"eval {ev['name']} template_context missing region"

    def test_all_evals_are_ros_focused(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        for ev in data["evals"]:
            prompt_lower = ev["prompt"].lower()
            assert "terraform" not in prompt_lower
            ctx = ev["template_context"]
            if "template" in ctx:
                assert "ROSTemplateFormatVersion" in ctx["template"]

    def test_assertions_have_name_and_check(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        for ev in data["evals"]:
            for assertion in ev["assertions"]:
                assert "name" in assertion
                assert "check" in assertion

    def test_eval_ids_unique(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        ids = [ev["id"] for ev in data["evals"]]
        assert len(ids) == len(set(ids))

    def test_eval_names_unique(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        names = [ev["name"] for ev in data["evals"]]
        assert len(names) == len(set(names))

    def test_covers_fix_scenario(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        fix_evals = [
            ev
            for ev in data["evals"]
            if any("fix" in a["name"] or "template_fixed" in a["name"] for a in ev["assertions"])
        ]
        assert len(fix_evals) > 0, "should have at least one eval covering template fix scenario"

    def test_covers_error_scenario(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        error_evals = [
            ev
            for ev in data["evals"]
            if any("error" in a["name"] or "failure" in a["name"] or "fail" in a["name"] for a in ev["assertions"])
        ]
        assert len(error_evals) > 0, "should have at least one eval covering error/failure scenario"
