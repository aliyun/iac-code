import json
from pathlib import Path

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

    def test_contains_ros_stack(self, body):
        assert "ros_stack" in body

    def test_contains_availability_query(self, body):
        assert "可用性查询" in body

    def test_contains_template_validation(self, body):
        assert "ValidateTemplate" in body
        assert "模板校验" in body

    def test_no_pricing_section(self, body):
        assert "GetTemplateEstimateCost" not in body
        assert "部署前询价" not in body

    def test_contains_create_stack(self, body):
        assert "CreateStack" in body
        assert "DisableRollback" in body

    def test_contains_continue_create(self, body):
        assert "ContinueCreateStack" in body

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


class TestDeployingPrompt:
    def test_pipeline_confirmed_deploy_is_direct_execution(self):
        body = DEPLOYING_PROMPT_MD.read_text(encoding="utf-8")
        assert "不要再次询问是否确认部署" in body
        assert "不得用 status: cancelled 表示等待用户确认" in body
        assert "只有用户明确取消部署时" in body


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
