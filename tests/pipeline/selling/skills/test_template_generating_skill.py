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

    def test_contains_parameterization_rules(self, body):
        assert "参数化规则" in body

    def test_contains_validation_step(self, body):
        assert "ValidateTemplate" in body

    def test_contains_resource_types(self, body):
        assert "ALIYUN::ECS::VPC" in body
        assert "ALIYUN::ECS::InstanceGroup" in body

    def test_no_deploy_flow(self, body):
        assert "CreateStack" not in body
        assert "ros_stack" not in body

    def test_no_pricing_flow(self, body):
        assert "GetTemplateEstimateCost" not in body
        assert "询价" not in body

    def test_contains_error_handling(self, body):
        assert "校验失败" in body

    def test_honors_candidate_resource_lifecycle_contract(self, body):
        assert "resource_intents" in body
        assert "action=create" in body
        assert "action=use_existing" in body
        assert "action=forbid" in body
        assert "action=use_existing/reference 的资源必须建模为 Parameters" in body
        assert "不得在 Resources 中创建" in body
        assert "已有 VPC 中创建安全组" in body
        assert "forbidden_resources" not in body


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
