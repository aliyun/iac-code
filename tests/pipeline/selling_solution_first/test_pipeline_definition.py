"""Pipeline definition, discovery and loader contract for ``selling_solution_first``.

设计文档 §18.1：新 pipeline 必须被 discovery 找到、三个顶层普通 Step 串成 forward 链、
没有 sub-pipeline 也没有并行候选物化，prompt / skill / tool / hook 在安装态 loader 下可发现，
而原 `selling` 仍保持五个顶层 Step。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml

from iac_code.pipeline import create_pipeline, discover_pipelines
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool
from iac_code.pipeline.selling.tools.show_candidate_detail_tool import (
    ShowCandidateDetailTool as SellingShowCandidateDetailTool,
)
from iac_code.pipeline.selling_solution_first.tools.confirmed_ros_deploy_tool import ConfirmedRosDeployTool

PIPELINE_NAME = "selling_solution_first"
STEP_IDS = ("solution_planning_and_selection", "materialize_selected_candidate", "deploying")


@pytest.fixture(scope="module")
def pipeline_dir():
    pipelines = discover_pipelines()
    assert PIPELINE_NAME in pipelines
    return pipelines[PIPELINE_NAME]


@pytest.fixture(scope="module")
def loaded(pipeline_dir):
    return load_pipeline_dir(pipeline_dir)


@pytest.fixture(scope="module")
def raw_yaml(pipeline_dir):
    return yaml.safe_load((pipeline_dir / "pipeline.yaml").read_text(encoding="utf-8"))


class TestDiscovery:
    def test_pipeline_is_discovered_next_to_selling(self, pipeline_dir):
        pipelines = discover_pipelines()

        assert "selling" in pipelines
        assert (pipeline_dir / "pipeline.yaml").exists()

    def test_create_pipeline_builds_three_step_runner(self):
        storage = MagicMock()
        storage.session_path.return_value = MagicMock()

        runner = create_pipeline(
            PIPELINE_NAME,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="solution-first-1",
        )

        assert runner.pipeline_name == PIPELINE_NAME
        assert runner.state_machine.total_steps == 3

    def test_selling_still_has_five_top_level_steps(self):
        storage = MagicMock()
        storage.session_path.return_value = MagicMock()

        runner = create_pipeline(
            "selling",
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="selling-regression-1",
        )

        assert runner.pipeline_name == "selling"
        assert runner.state_machine.total_steps == 5


class TestTopLevelSteps:
    def test_three_plain_steps_in_a_forward_chain(self, loaded):
        assert loaded.name == PIPELINE_NAME
        assert [step.step_id for step in loaded.steps] == list(STEP_IDS)
        assert [step.step_type for step in loaded.steps] == ["normal", "normal", "normal"]
        assert [step.forward for step in loaded.steps] == [
            "materialize_selected_candidate",
            "deploying",
            None,
        ]
        assert [step.conclusion_field for step in loaded.steps] == [
            "solution_selection",
            "selected_plan",
            "deployment",
        ]

    def test_no_sub_pipeline_or_parallel_candidate_materialization(self, loaded, raw_yaml):
        assert loaded.sub_pipelines == {}
        assert "sub_pipelines" not in raw_yaml
        for step in loaded.steps:
            assert step.sub_pipeline_name is None
            assert step.step_type != "parallel_sub_pipeline"
        for raw_step in raw_yaml["steps"]:
            assert "sub_pipeline" not in raw_step
            assert raw_step.get("type", "normal") == "normal"

    def test_context_dependencies_are_acyclic_and_forward_only(self, loaded):
        # load_pipeline_dir 自身会拒绝环；这里额外锁定依赖方向与 Step 顺序一致。
        assert loaded.context_dependencies == {
            "solution_selection": [],
            "selected_plan": ["solution_selection"],
            "deployment": ["solution_selection", "selected_plan"],
        }
        seen: list[str] = []
        for field, deps in loaded.context_dependencies.items():
            assert all(dep in seen for dep in deps)
            seen.append(field)

    def test_step_one_is_the_candidate_selection_gate(self, loaded):
        step = loaded.steps[0]

        assert step.ui_mode == "candidate_selection"
        assert step.auto_advance is False
        assert step.config["accept_parameter_overrides"] is False
        assert step.config["completion_record_contract"] == "v2"
        assert set(step.inject_tools) == {"ask_user_question", "show_architecture_plan", "show_candidate_detail"}
        assert "aliyun_api" in step.tools.include
        assert step.hooks_file == "hooks/solution_planning_and_selection.py"
        assert step.completion_enricher is not None

    def test_candidate_and_deployment_options_keep_separate_schemas(self, raw_yaml):
        candidate_options = raw_yaml["steps"][0]["conclusion_schema"]["properties"]["options"]
        candidate_properties = candidate_options["items"]["properties"]
        assert set(candidate_properties) == {"name", "summary", "candidate_index"}
        assert "allOf" not in candidate_options

        confirmation_options = raw_yaml["steps"][1]["conclusion_schema"]["properties"]["options"]
        assert confirmation_options["minItems"] == 2
        assert confirmation_options["maxItems"] == 4
        required_actions = {rule["contains"]["properties"]["action"]["const"] for rule in confirmation_options["allOf"]}
        assert required_actions == {"confirm", "cancel"}
        assert set(confirmation_options["items"]["properties"]["action"]["enum"]) == {
            "confirm",
            "adjust",
            "reselect",
            "cancel",
        }

    def test_complete_step_schemas_describe_branch_identity_and_handoff_fields(self, loaded):
        planning, materialize, deploying = loaded.steps
        planning_schema = planning.conclusion_schema
        materialize_schema = materialize.conclusion_schema
        deploying_schema = deploying.conclusion_schema

        assert planning_schema is not None
        assert "awaiting_selection" in planning_schema["description"]
        assert "selected_candidate_index" in planning_schema["properties"]["status"]["description"]
        candidate = planning_schema["properties"]["candidates"]["items"]
        assert "原样取自" in candidate["description"]
        assert "Step 2 唯一允许写入" in candidate["properties"]["output_path"]["description"]
        assert (
            "0 基下标"
            in planning_schema["properties"]["options"]["items"]["properties"]["candidate_index"]["description"]
        )
        assert "原样等于" in planning_schema["properties"]["selected_candidate"]["description"]

        assert materialize_schema is not None
        assert "rollback_request" in materialize_schema["description"]
        assert "effective_deployment_parameters" in materialize_schema["properties"]["status"]["description"]
        materialized = materialize_schema["properties"]["selected_candidate_result"]["properties"]
        assert "面向最终用户" in materialized["solution_summary"]["description"]
        assert "ROS 精确询价" in materialized["cost"]["description"]
        assert "同一路径" in materialized["template"]["properties"]["file_path"]["description"]
        assert "没有覆盖时必须使用空对象" in materialize_schema["properties"]["parameter_overrides"]["description"]
        assert "真实用户确认输入" in materialize_schema["properties"]["confirmation"]["description"]

        # Step 3 继续从共享 deploying skill 继承 schema，不在新 pipeline 复制一份。
        assert deploying_schema is not None
        assert "complete_step" in deploying_schema["description"]
        assert "CREATE_COMPLETE" in deploying_schema["properties"]["status"]["description"]
        assert "真实 Stack Outputs" in deploying_schema["properties"]["outputs"]["description"]

    def test_step_two_materializes_only_the_selected_candidate(self, loaded):
        step = loaded.steps[1]

        assert step.ui_mode == "deployment_confirmation"
        assert step.auto_advance is False
        assert step.context_fields == ["solution_selection", "selected_plan"]
        assert step.config["deterministic_structured_confirmation"] is True
        assert step.config["compact_completion_schema"] is True
        assert step.config["compact_completion_errors"] is True
        assert step.config["completion_validation_error_limit"] == 5
        assert step.config["fresh_agent_context_on_resume"] is True
        assert step.config["conclusion_merge_context_field"] == "selected_plan"
        assert step.config["completion_record_contract"] == "v2"
        assert step.config["hard_constraint_evidence_contract"] == "v2"
        assert "authoritative_candidate_targets" not in step.config
        assert step.completion_input_schema is not None
        assert step.completion_enricher is not None
        assert step.hooks_file == "hooks/materialize_selected_candidate.py"
        assert set(step.inject_tools) == {
            "ask_user_question",
            "ros_validate_template",
            "ros_get_template_parameter_constraints",
            "ros_preview_template",
            "ros_estimate_template_cost",
        }
        # 物化步骤不得拿到任何 Stack 写入入口。
        assert "ros_deploy" not in step.inject_tools
        assert {"ros_stack", "ros_stack_instances"} <= set(step.tools.exclude)

    def test_step_three_deploys_behind_the_confirmed_wrapper(self, loaded):
        step = loaded.steps[2]

        assert step.context_fields == ["solution_selection", "selected_plan"]
        assert step.hooks_file == "hooks/deploying.py"
        assert step.complete_step_terminal is False
        assert "ros_deploy" in step.inject_tools
        assert {"ros_stack", "ros_stack_instances", "write_file"} <= set(step.tools.exclude)


class TestLoaderDiscovery:
    def test_prompts_skills_and_hooks_resolve_from_the_package(self, loaded, pipeline_dir):
        for step in loaded.steps:
            assert (pipeline_dir / step.prompt_file).is_file()
            assert step.skill in loaded.skills
            assert loaded.skills[step.skill].strip()
            if step.hooks_file:
                assert (pipeline_dir / step.hooks_file).is_file()

    def test_skill_roots_point_at_the_pipeline_local_skill_dirs(self, loaded, pipeline_dir):
        for skill_name, root in loaded.skill_roots.items():
            assert root == str((pipeline_dir / "skills" / skill_name).resolve())

    def test_pipeline_tools_expose_the_confirmed_deploy_wrapper_only(self, loaded):
        # loader 用 importlib 按文件加载，所以类对象与直接 import 的不是同一个身份；
        # 判据是「它是 wrapper 子类而不是原始 RosDeployTool」。
        registered = loaded.pipeline_tools["ros_deploy"]
        assert registered.__name__ == ConfirmedRosDeployTool.__name__
        assert registered is not RosDeployTool
        assert issubclass(registered, RosDeployTool)
        assert "show_architecture_plan" in loaded.pipeline_tools
        # 模块别名式复用不得把原始 RosDeployTool 暴露成第二个 ros_deploy 实现。
        deploy_impls = {
            name: cls.__name__
            for name, cls in loaded.pipeline_tools.items()
            if isinstance(cls, type) and issubclass(cls, RosDeployTool)
        }
        assert deploy_impls == {"ros_deploy": ConfirmedRosDeployTool.__name__}

    def test_reused_selling_tools_are_registered_by_their_stable_names(self, loaded):
        for tool_name in (
            "ros_validate_template",
            "ros_get_template_parameter_constraints",
            "ros_preview_template",
            "ros_estimate_template_cost",
            "show_candidate_detail",
        ):
            assert tool_name in loaded.pipeline_tools

    def test_solution_first_uses_a_local_rich_detail_tool_without_changing_selling(self, loaded):
        solution_first_schema = loaded.pipeline_tools["show_candidate_detail"]().input_schema
        selling_schema = SellingShowCandidateDetailTool().input_schema

        assert "topology_graph" in solution_first_schema["properties"]
        assert "summary" not in solution_first_schema["properties"]
        assert "summary" in selling_schema["properties"]
        assert "topology_graph" not in selling_schema["properties"]

    def test_every_injected_tool_resolves_to_a_pipeline_or_engine_tool(self, loaded):
        engine_provided = {"ask_user_question"}
        for step in loaded.steps:
            for tool_name in step.inject_tools:
                assert tool_name in loaded.pipeline_tools or tool_name in engine_provided
