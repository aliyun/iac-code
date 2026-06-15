from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.step_spec import (
    IncludeExcludeConfig,
    LoadedPipeline,
    StepSpec,
    SubPipelineSpec,
    render_prompt,
)
from iac_code.pipeline.engine.types import RollbackRule


class TestIncludeExcludeConfig:
    def test_defaults_to_empty_lists(self):
        config = IncludeExcludeConfig()
        assert config.include == []
        assert config.exclude == []

    def test_with_values(self):
        config = IncludeExcludeConfig(include=["bash", "read_file"], exclude=["write_file"])
        assert config.include == ["bash", "read_file"]
        assert config.exclude == ["write_file"]


class TestStepSpecToolsConfig:
    def test_tools_config_default_none(self):
        spec = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
        )
        assert spec.tools is None
        assert spec.base_prompt_sections is None

    def test_tools_config_with_exclude(self):
        config = IncludeExcludeConfig(exclude=["bash"])
        spec = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
            tools=config,
        )
        assert spec.tools.exclude == ["bash"]

    def test_base_prompt_sections_on_step(self):
        config = IncludeExcludeConfig(include=["identity", "env"])
        spec = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
            base_prompt_sections=config,
        )
        assert spec.base_prompt_sections.include == ["identity", "env"]


class TestLoadedPipelineBaseSections:
    def test_base_prompt_sections_field(self):
        config = IncludeExcludeConfig(include=["identity", "system", "env"])
        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={},
            max_rollbacks=3,
            skills={},
            base_prompt_sections=config,
        )
        assert pipeline.base_prompt_sections.include == ["identity", "system", "env"]


class TestStepSpec:
    def test_create_minimal(self):
        spec = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward="architecture_planning",
            prompt_file="prompts/intent_parsing.md",
        )
        assert spec.step_id == "intent_parsing"
        assert spec.auto_advance is True
        assert spec.skill is None
        assert spec.step_type == "normal"
        assert spec.sub_pipeline_name is None
        assert spec.tools is None
        assert spec.on_enter is None
        assert spec.on_exit is None

    def test_create_full(self):
        spec = StepSpec(
            step_id="reviewing",
            conclusion_field="review",
            forward="cost_estimating",
            prompt_file="prompts/reviewing.md",
            skill="iac-aliyun-review",
            rollback_rules=[RollbackRule(target_step="template_generating", condition="template_issue")],
            context_fields=["template"],
            enabled_when="cost_estimation",
            hooks_file="hooks/deploying.py",
        )
        assert spec.skill == "iac-aliyun-review"
        assert spec.enabled_when == "cost_estimation"
        assert len(spec.rollback_rules) == 1

    def test_parallel_sub_pipeline_step(self):
        spec = StepSpec(
            step_id="parallel_deploy",
            conclusion_field="deploy_results",
            forward=None,
            prompt_file="prompts/deploy.md",
            step_type="parallel_sub_pipeline",
            sub_pipeline_name="deploy_single",
        )
        assert spec.step_type == "parallel_sub_pipeline"
        assert spec.sub_pipeline_name == "deploy_single"


class TestSubPipelineSpec:
    def test_create_basic(self):
        step = StepSpec(
            step_id="deploy_one",
            conclusion_field="deploy_result",
            forward=None,
            prompt_file="prompts/deploy_one.md",
            skill="iac-aliyun-deploy",
        )
        sub = SubPipelineSpec(
            name="deploy_single",
            steps=[step],
            max_rollbacks=2,
            iterate_over="selected_architectures",
        )
        assert sub.name == "deploy_single"
        assert len(sub.steps) == 1
        assert sub.max_rollbacks == 2
        assert sub.iterate_over == "selected_architectures"
        assert sub.context_fields_from_parent == []

    def test_with_context_fields_from_parent(self):
        sub = SubPipelineSpec(
            name="review_single",
            steps=[],
            max_rollbacks=1,
            iterate_over="templates",
            context_fields_from_parent=["intent", "architecture"],
        )
        assert sub.context_fields_from_parent == ["intent", "architecture"]


class TestLoadedPipeline:
    def test_sub_pipelines_defaults_to_empty_dict(self):
        pipeline = LoadedPipeline(
            name="selling",
            steps=[],
            context_dependencies={},
            max_rollbacks=3,
            skills={},
        )
        assert pipeline.sub_pipelines == {}

    def test_sub_pipelines_with_entries(self):
        sub = SubPipelineSpec(
            name="deploy_single",
            steps=[],
            max_rollbacks=2,
            iterate_over="selected_architectures",
        )
        pipeline = LoadedPipeline(
            name="selling",
            steps=[],
            context_dependencies={},
            max_rollbacks=3,
            skills={},
            sub_pipelines={"deploy_single": sub},
        )
        assert "deploy_single" in pipeline.sub_pipelines
        assert pipeline.sub_pipelines["deploy_single"].iterate_over == "selected_architectures"


class TestStepSpecDescription:
    def test_step_spec_description_field(self):
        step = StepSpec(
            step_id="test",
            conclusion_field="test_out",
            forward=None,
            prompt_file="test.md",
            description="解析用户意图",
        )
        assert step.description == "解析用户意图"

    def test_step_spec_description_defaults_empty(self):
        step = StepSpec(
            step_id="test",
            conclusion_field="test_out",
            forward=None,
            prompt_file="test.md",
        )
        assert step.description == ""


class TestRenderPrompt:
    def test_renders_context_fields(self):
        ctx = PipelineContext({"intent": [], "architecture": ["intent"]})
        ctx.set_conclusion("intent", {"type": "e-commerce"})
        template = "# Step\n\nIntent:\n```json\n{intent}\n```"
        result = render_prompt(template, ctx, ["intent"])
        assert '"type": "e-commerce"' in result

    def test_missing_field_renders_empty_object(self):
        ctx = PipelineContext({"intent": []})
        template = "Data: {intent}"
        result = render_prompt(template, ctx, ["intent"])
        assert result == "Data: {}"

    def test_no_context_fields_returns_template_unchanged(self):
        ctx = PipelineContext({"intent": []})
        template = "No variables here."
        result = render_prompt(template, ctx, [])
        assert result == "No variables here."

    def test_preserves_literal_braces_outside_fields(self):
        ctx = PipelineContext({"intent": []})
        ctx.set_conclusion("intent", {"k": "v"})
        template = 'JSON example: {{"literal": true}}\nReal: {intent}'
        result = render_prompt(template, ctx, ["intent"])
        assert '{"literal": true}' in result
        assert '"k": "v"' in result
