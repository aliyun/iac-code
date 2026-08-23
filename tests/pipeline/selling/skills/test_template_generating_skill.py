import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "iac_code"
    / "pipeline"
    / "selling"
    / "skills"
    / "iac-aliyun-template-generating"
)
SKILL_MD = SKILL_DIR / "SKILL.md"
EVALS_JSON = SKILL_DIR / "evals.json"
TEMPLATE_PROMPT_MD = SKILL_DIR.parents[1] / "prompts" / "template_generating.md"


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
        assert fm["name"] == "iac-aliyun-template-generating"

    def test_not_user_invocable(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert fm.get("user_invocable") is False

    def test_description_mentions_ros(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        assert "ROS" in fm["description"]

    def test_conclusion_schema_rejects_empty_template_and_requires_sha256(self):
        fm = _parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
        schema = fm["conclusion_schema"]

        assert schema["required"] == ["template", "template_sha256", "file_path", "region", "description"]
        assert schema["properties"]["template"]["minLength"] == 1
        assert schema["properties"]["template_sha256"]["type"] == "string"
        assert schema["additionalProperties"] is False

    def test_conclusion_schema_validates_template_emptiness(self):
        import jsonschema

        schema = _parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))["conclusion_schema"]
        valid = {
            "template": "ROSTemplateFormatVersion: 2015-09-01\n",
            "template_sha256": "a" * 64,
            "file_path": "template.yml",
            "region": "cn-hangzhou",
            "description": "demo",
        }
        jsonschema.validate(valid, schema)

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**valid, "template": ""}, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({key: value for key, value in valid.items() if key != "template_sha256"}, schema)


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

    def test_contains_ros_template_format(self, body):
        assert "ROSTemplateFormatVersion" in body or "ROS" in body

    def test_parameterization_guidance_points_to_references_without_inline_table(self, body):
        assert "库存相关属性" in body
        assert "references/cloud-products/" in body
        assert "| ECS | ZoneId, InstanceType" not in body

    def test_contains_validation_step(self, body):
        assert "ros_validate_template" in body

    def test_template_url_source_is_not_duplicated_from_prompt(self, body):
        assert "模板来源硬约束" not in body
        assert "params.TemplateURL = <候选方案输出路径>" not in body
        assert "不要传 `params.TemplateBody`" not in body
        assert "template_url=<模板文件路径>" not in body
        assert "region_id=<地域>" not in body

    def test_must_read_ros_template_reference_before_generation(self, body):
        assert "必须" in body
        assert "references/ros-template.md" in body
        assert "未阅读不得生成模板" in body

    def test_does_not_inline_common_resource_catalog(self, body):
        assert "## 常用资源类型" not in body
        assert "ALIYUN::ECS::VPC: 创建专有网络" not in body
        assert "ALIYUN::ECS::InstanceGroup: 创建 N 个 ECS 实例" not in body
        assert "references/ros-template.md" in body

    def test_run_command_details_stay_in_reference(self, body):
        assert "## 在实例中执行命令" not in body
        assert "ALIYUN::ECS::RunCommand + `CommandContent`" not in body
        assert "references/ros-template.md" in body

    def test_no_deploy_flow(self, body):
        assert "CreateStack" not in body
        assert "ros_stack" not in body

    def test_no_pricing_flow(self, body):
        assert "GetTemplateEstimateCost" not in body
        assert "询价" not in body

    def test_contains_error_handling(self, body):
        assert "校验失败" in body

    def test_states_output_contract_for_template_content(self, body):
        assert "## 产出协议" in body
        assert "template_sha256" in body
        assert "sha256" in body
        assert "逐字节一致" in body
        assert "不要在未产出模板文件的情况下提交结论" in body

    def test_honors_candidate_resource_lifecycle_contract(self, body):
        assert "resource_intents" in body
        assert "action=create" in body
        assert "action=use_existing" in body
        assert "action=forbid" in body
        assert "action=use_existing/reference 的资源必须建模为 Parameters" in body
        assert "不得在 Resources 中创建" in body
        assert "已有 VPC 中创建安全组" in body
        assert "forbidden_resources" not in body

    def test_preserves_product_neutral_user_hard_constraints(self, body):
        assert "candidate" in body
        assert "hard_constraints" in body
        assert "原样贯穿" in body
        assert "保持参数化" in body
        assert "rollback_request" in body
        assert "DescribeInstanceTypes" not in body

    def test_inventory_values_stay_parameterized_and_defaults_are_scoped(self, body):
        assert "库存相关属性**必须**定义为 Parameters" in body
        assert "以下属性**不需要**参数化，直接使用合理默认值" in body
        assert "对用户未指定的参数直接使用合理默认值" not in body

    def test_file_write_details_stay_in_step_prompt(self, body):
        assert "并写入文件" in body
        assert "生成的模板默认放在当前工作目录" in body
        assert "`./template.yml`" in body
        assert "/tmp/" not in body
        assert "write_file" not in body
        assert "无需提前创建目录" not in body
        assert "bash" not in body.lower()
        assert "mkdir" not in body.lower()


class TestSkillDiscovery:
    def test_discovered_by_pipeline_loader(self):
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        pipeline_dir = SKILL_DIR.parents[1]
        loaded = load_pipeline_dir(pipeline_dir)
        assert "iac-aliyun-template-generating" in loaded.skills

    def test_skill_content_matches_file(self):
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        pipeline_dir = SKILL_DIR.parents[1]
        loaded = load_pipeline_dir(pipeline_dir)
        expected = SKILL_MD.read_text(encoding="utf-8")
        assert loaded.skills["iac-aliyun-template-generating"] == expected


class TestSkillPromptRendering:
    def test_prompt_names_template_url_value_for_validation(self):
        body = TEMPLATE_PROMPT_MD.read_text(encoding="utf-8")
        assert 'template_url = "{candidate.output_path}"' in body
        assert "ros_validate_template" in body
        assert "ValidateTemplate" in body
        assert "TemplateBody" in body

    def test_prompt_uses_write_file_without_directory_creation(self):
        body = TEMPLATE_PROMPT_MD.read_text(encoding="utf-8")
        assert "write_file" in body
        assert "无需提前创建目录" in body
        assert "如果 `templates/` 目录不存在，先创建它" not in body
        assert "mkdir" not in body.lower()
        assert "bash" not in body.lower()

    def test_prompt_requires_template_content_and_sha256_in_conclusion(self):
        body = TEMPLATE_PROMPT_MD.read_text(encoding="utf-8")
        assert "`template_sha256`" in body
        assert "sha256" in body
        assert "逐字节一致" in body
        assert "已通过 `ros_validate_template` 校验的路径" in body

    def test_prompt_passes_full_candidate_without_repeating_skill_constraints(self):
        body = TEMPLATE_PROMPT_MD.read_text(encoding="utf-8")
        assert "{candidate}" in body
        assert "candidate.hard_constraints" not in body
        assert "不要把候选推荐值当成用户硬约束" not in body
        assert "查询可用区、实例规格" not in body
        assert "对用户未指定的参数直接使用合理默认值" not in body

    def test_full_prompt_includes_skill_base_directory(self, tmp_path):
        from iac_code.pipeline.engine.context import PipelineContext
        from iac_code.pipeline.engine.loader import load_pipeline_dir
        from iac_code.pipeline.engine.step_executor import StepExecutor
        from iac_code.tools.base import ToolRegistry

        pipeline_dir = SKILL_DIR.parents[1]
        loaded = load_pipeline_dir(pipeline_dir)
        step = next(s for s in loaded.sub_pipelines["evaluate_candidate"].steps if s.step_id == "template_generating")
        context = PipelineContext({"candidate": []})
        context.set_conclusion("candidate", {"output_path": "templates/example.yml"})

        prompt = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=loaded,
            pipeline_dir=pipeline_dir,
            cwd=str(tmp_path),
        )._build_full_system_prompt(step, context)

        assert f"Base directory for this skill: {SKILL_DIR}" in prompt

    def test_agent_loop_trusts_skill_base_directory_for_tools(self, tmp_path):
        from iac_code.pipeline.engine.context import PipelineContext
        from iac_code.pipeline.engine.loader import load_pipeline_dir
        from iac_code.pipeline.engine.step_executor import StepExecutor
        from iac_code.tools.base import ToolRegistry

        pipeline_dir = SKILL_DIR.parents[1]
        loaded = load_pipeline_dir(pipeline_dir)
        step = next(s for s in loaded.sub_pipelines["evaluate_candidate"].steps if s.step_id == "template_generating")
        context = PipelineContext({"candidate": []})
        context.set_conclusion("candidate", {"output_path": "templates/example.yml"})

        agent_context = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=loaded,
            pipeline_dir=pipeline_dir,
            cwd=str(tmp_path),
        ).build_agent_loop_context(step, context, "session-1")

        assert agent_context.agent_loop is not None
        assert str(SKILL_DIR) in agent_context.agent_loop._tool_context_trusted_read_directories
        assert str(SKILL_DIR) in agent_context.agent_loop._tool_context_relative_read_directories


class TestEvalsJson:
    def test_evals_file_exists(self):
        assert EVALS_JSON.exists()

    def test_valid_json(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_has_required_fields(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        assert data["skill_name"] == "iac-aliyun-template-generating"
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

    def test_all_evals_are_ros_focused(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        for ev in data["evals"]:
            prompt_lower = ev["prompt"].lower()
            assert "terraform" not in prompt_lower

    def test_assertions_have_name_and_check(self):
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
        for ev in data["evals"]:
            for assertion in ev["assertions"]:
                assert "name" in assertion
                assert "check" in assertion


class TestTemplateGeneratingOutputEnforcement:
    """The real pipeline guards must reject conclusions without a written, validated template."""

    TEMPLATE = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"

    @staticmethod
    def _sha256(value: str) -> str:
        import hashlib

        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @pytest.fixture()
    def step(self):
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        loaded = load_pipeline_dir(SKILL_DIR.parents[1])
        return next(s for s in loaded.sub_pipelines["evaluate_candidate"].steps if s.step_id == "template_generating")

    def _tool(self, step, tool_calls, cwd):
        from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
        from iac_code.pipeline.engine.completion_guard_state import record_completion_guard_tool_result
        from iac_code.pipeline.engine.types import StepConfig

        state: dict = {}
        for tool_name, tool_input, content in tool_calls:
            record_completion_guard_tool_result(
                state,
                tool_name=tool_name,
                tool_input=tool_input,
                content=content,
                is_error=False,
                cwd=cwd,
            )
        config = StepConfig(
            step_id=step.step_id,
            conclusion_field=step.conclusion_field,
            forward=step.forward,
            conclusion_schema=step.conclusion_schema,
        )
        return CompleteStepTool(config, completion_guards=step.completion_guards, completion_guard_state=state)

    def _write_call(self):
        return ("write_file", {"path": "template.yml", "content": self.TEMPLATE}, None)

    def _validate_call(self):
        return ("ros_validate_template", {"template_url": "template.yml"}, json.dumps({"Parameters": {}}))

    def _conclusion(self, **overrides):
        conclusion = {
            "template": self.TEMPLATE,
            "template_sha256": self._sha256(self.TEMPLATE),
            "file_path": "template.yml",
            "region": "cn-hangzhou",
            "description": "demo",
        }
        conclusion.update(overrides)
        return conclusion

    @pytest.mark.asyncio
    async def test_written_and_validated_template_completes_step(self, step, tmp_path):
        from iac_code.tools.base import ToolContext

        tool = self._tool(step, [self._write_call(), self._validate_call()], str(tmp_path))

        result = await tool.execute(tool_input={"conclusion": self._conclusion()}, context=ToolContext())

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_empty_template_is_rejected(self, step, tmp_path):
        from iac_code.tools.base import ToolContext

        tool = self._tool(step, [self._write_call(), self._validate_call()], str(tmp_path))
        conclusion = self._conclusion(template="", template_sha256=self._sha256(""))

        result = await tool.execute(tool_input={"conclusion": conclusion}, context=ToolContext())

        assert result.is_error

    @pytest.mark.asyncio
    async def test_template_without_write_file_is_rejected(self, step, tmp_path):
        from iac_code.tools.base import ToolContext

        tool = self._tool(step, [self._validate_call()], str(tmp_path))

        result = await tool.execute(tool_input={"conclusion": self._conclusion()}, context=ToolContext())

        assert result.is_error
        assert "write_file" in result.content

    @pytest.mark.asyncio
    async def test_template_without_validation_is_rejected(self, step, tmp_path):
        from iac_code.tools.base import ToolContext

        tool = self._tool(step, [self._write_call()], str(tmp_path))

        result = await tool.execute(tool_input={"conclusion": self._conclusion()}, context=ToolContext())

        assert result.is_error
        assert "ros_validate_template" in result.content

    @pytest.mark.asyncio
    async def test_rewrite_after_validation_is_rejected(self, step, tmp_path):
        from iac_code.tools.base import ToolContext

        tool_calls = [self._write_call(), self._validate_call(), self._write_call()]
        tool = self._tool(step, tool_calls, str(tmp_path))

        result = await tool.execute(tool_input={"conclusion": self._conclusion()}, context=ToolContext())

        assert result.is_error
        assert "ros_validate_template" in result.content

    @pytest.mark.asyncio
    async def test_template_hash_mismatch_is_rejected(self, step, tmp_path):
        from iac_code.tools.base import ToolContext

        tool = self._tool(step, [self._write_call(), self._validate_call()], str(tmp_path))
        conclusion = self._conclusion(template_sha256=self._sha256("stale template\n"))

        result = await tool.execute(tool_input={"conclusion": conclusion}, context=ToolContext())

        assert result.is_error
        assert "template_sha256" in result.content
