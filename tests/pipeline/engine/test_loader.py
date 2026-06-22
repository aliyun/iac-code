from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from iac_code.pipeline.engine.loader import _parse_exit_condition, load_pipeline_dir


def _write_pipeline(tmp_path: Path, yaml_content: str, prompts: dict[str, str] | None = None):
    """Helper: write pipeline.yaml and optional prompt files."""
    (tmp_path / "pipeline.yaml").write_text(yaml_content, encoding="utf-8")
    if prompts:
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name, content in prompts.items():
            (prompts_dir / name).write_text(content, encoding="utf-8")


MINIMAL_YAML = dedent("""\
    name: test
    context_dependencies:
      intent: []
      architecture: [intent]
    max_rollbacks: 2
    steps:
      - id: step_a
        conclusion_field: intent
        forward: step_b
        prompt: prompts/step_a.md
        skill: skill-x
      - id: step_b
        conclusion_field: architecture
        forward: null
        prompt: prompts/step_b.md
        context_fields: [intent]
""")


class TestLoadPipelineDir:
    def test_loads_basic_pipeline(self, tmp_path):
        _write_pipeline(tmp_path, MINIMAL_YAML, {"step_a.md": "Do A", "step_b.md": "Do B with {intent}"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.name == "test"
        assert len(loaded.steps) == 2
        assert loaded.steps[0].step_id == "step_a"
        assert loaded.steps[0].skill == "skill-x"
        assert loaded.steps[1].context_fields == ["intent"]
        assert loaded.max_rollbacks == 2

    def test_ignores_legacy_step_rollback_section(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
              architecture: [intent]
            max_rollbacks: 2
            steps:
              - id: step_a
                conclusion_field: intent
                forward: step_b
                prompt: prompts/step_a.md
              - id: step_b
                conclusion_field: architecture
                forward: null
                prompt: prompts/step_b.md
                rollback:
                  - target: step_a
                    condition: revise_intent
        """)
        _write_pipeline(tmp_path, yaml_content, {"step_a.md": "Do A", "step_b.md": "Do B"})

        loaded = load_pipeline_dir(tmp_path)

        assert not hasattr(loaded.steps[1], "rollback_rules")

    def test_selling_iac_aliyun_skill_reference_file_uses_bundled_root_fallback(self, tmp_path):
        _write_pipeline(tmp_path, MINIMAL_YAML, {"step_a.md": "Do A", "step_b.md": "Do B with {intent}"})
        skill_dir = tmp_path / "skills" / "iac-aliyun-cost"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("Read references/ros-template.md", encoding="utf-8")
        (skill_dir / "references").write_text("../broken-on-windows-checkout", encoding="utf-8")

        loaded = load_pipeline_dir(tmp_path)

        skill_root = Path(loaded.skill_roots["iac-aliyun-cost"])
        assert skill_root != skill_dir.resolve()
        assert (skill_root / "references" / "ros-template.md").is_file()
        assert (skill_root / "references" / "cloud-products" / "ecs.md").is_file()

    def test_rejects_context_dependency_cycle(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: [architecture]
              architecture: [intent]
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "Parse"})

        with pytest.raises(ValueError, match=r"context dependency cycle: .*intent.*architecture.*intent"):
            load_pipeline_dir(tmp_path)

    def test_enabled_when_filters_step(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
              cost: [intent]
              deploy: [intent]
            feature_flags:
              cost_estimation:
                default: true
                env: IAC_CODE_COST_ESTIMATION
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: cost
                prompt: prompts/parse.md
              - id: cost
                conclusion_field: cost
                forward: deploy
                prompt: prompts/cost.md
                enabled_when: cost_estimation
              - id: deploy
                conclusion_field: deploy
                forward: null
                prompt: prompts/deploy.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P", "cost.md": "C", "deploy.md": "D"})
        with patch.dict("os.environ", {"IAC_CODE_COST_ESTIMATION": "false"}):
            loaded = load_pipeline_dir(tmp_path)
        assert len(loaded.steps) == 2
        assert loaded.steps[0].forward == "deploy"

    def test_feature_flags_default_values(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            feature_flags:
              cost_estimation:
                default: true
                env: IAC_CODE_COST_ESTIMATION
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        with patch.dict("os.environ", {}, clear=True):
            loaded = load_pipeline_dir(tmp_path)
        assert loaded.feature_flags == {"cost_estimation": True}

    def test_feature_flags_env_override(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            feature_flags:
              cost_estimation:
                default: true
                env: IAC_CODE_COST_ESTIMATION
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        with patch.dict("os.environ", {"IAC_CODE_COST_ESTIMATION": "false"}):
            loaded = load_pipeline_dir(tmp_path)
        assert loaded.feature_flags == {"cost_estimation": False}

    def test_discovers_skills(self, tmp_path):
        _write_pipeline(tmp_path, MINIMAL_YAML, {"step_a.md": "A", "step_b.md": "B"})
        skills_dir = tmp_path / "skills" / "skill-x"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# Skill X content", encoding="utf-8")
        loaded = load_pipeline_dir(tmp_path)
        assert "skill-x" in loaded.skills
        assert loaded.skills["skill-x"] == "# Skill X content"

    def test_loads_hooks(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              result: []
            max_rollbacks: 1
            steps:
              - id: hooked
                conclusion_field: result
                forward: null
                prompt: prompts/hooked.md
                hooks_file: hooks/hooked.py
        """)
        _write_pipeline(tmp_path, yaml_content, {"hooked.md": "Hooked"})
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hooked.py").write_text(
            "from iac_code.pipeline.engine.context import PipelineContext\n"
            "def on_enter(ctx: PipelineContext) -> None: ctx._test_marker = True\n",
            encoding="utf-8",
        )
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].on_enter is not None

    def test_missing_prompt_file_raises(self, tmp_path):
        _write_pipeline(tmp_path, MINIMAL_YAML)
        with pytest.raises(FileNotFoundError):
            load_pipeline_dir(tmp_path)


class TestSubPipelineParsing:
    def test_loads_sub_pipelines(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
              architecture: [intent]
              evaluated: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: architecture.candidates
                context_fields_from_parent: [intent]
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: null
                    skill: iac_aliyun
                    prompt: prompts/template.md
                    context_fields: [candidate]
            steps:
              - id: intent_parsing
                conclusion_field: intent
                forward: arch
                prompt: prompts/intent.md
                skill: iac-aliyun-intent
              - id: arch
                conclusion_field: architecture
                forward: eval
                prompt: prompts/arch.md
                skill: iac-aliyun-architecture
                context_fields: [intent]
              - id: eval
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: evaluated
                forward: null
                prompt: prompts/eval.md
        """)
        _write_pipeline(
            tmp_path,
            yaml_content,
            {
                "intent.md": "I",
                "arch.md": "A",
                "template.md": "T",
                "eval.md": "E",
            },
        )
        loaded = load_pipeline_dir(tmp_path)
        assert "evaluate_candidate" in loaded.sub_pipelines
        sub = loaded.sub_pipelines["evaluate_candidate"]
        assert sub.max_rollbacks == 2
        assert sub.iterate_over == "architecture.candidates"
        assert sub.context_fields_from_parent == ["intent"]
        assert len(sub.steps) == 1
        assert sub.steps[0].skill == "iac_aliyun"

    def test_step_type_parallel(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              result: []
            max_rollbacks: 1
            sub_pipelines:
              sub1:
                max_rollbacks: 1
                iterate_over: result.items
                steps:
                  - id: inner
                    conclusion_field: out
                    forward: null
                    prompt: prompts/inner.md
            steps:
              - id: outer
                type: parallel_sub_pipeline
                sub_pipeline: sub1
                conclusion_field: result
                forward: null
                prompt: prompts/outer.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"outer.md": "O", "inner.md": "I"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].step_type == "parallel_sub_pipeline"
        assert loaded.steps[0].sub_pipeline_name == "sub1"

    def test_skill_field_parsed(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                skill: iac-aliyun-intent
                prompt: prompts/parse.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].skill == "iac-aliyun-intent"


class TestToolsConfigParsing:
    def test_tools_object_with_exclude(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
                tools:
                  include: []
                  exclude: [bash]
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].tools is not None
        assert loaded.steps[0].tools.exclude == ["bash"]
        assert loaded.steps[0].tools.include == []

    def test_tools_object_with_include(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
                tools:
                  include: [read_file, grep]
                  exclude: []
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].tools.include == ["read_file", "grep"]

    def test_tools_not_specified_is_none(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].tools is None


class TestBasePromptSectionsParsing:
    def test_pipeline_level_sections(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            base_prompt_sections:
              include: [identity, env, tools]
              exclude: []
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.base_prompt_sections.include == ["identity", "env", "tools"]

    def test_pipeline_level_default_when_not_specified(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.base_prompt_sections.include == ["identity", "system", "env", "cloud_config", "tools"]
        assert loaded.base_prompt_sections.exclude == []

    def test_step_level_sections_override(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            base_prompt_sections:
              include: [identity, env]
              exclude: []
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
                base_prompt_sections:
                  include: [identity, env, actions]
                  exclude: []
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].base_prompt_sections.include == ["identity", "env", "actions"]


class TestInjectToolsParsing:
    def test_inject_tools_parsed(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: confirm
                conclusion_field: intent
                forward: null
                prompt: prompts/confirm.md
                inject_tools: [show_architecture_diagram]
        """)
        _write_pipeline(tmp_path, yaml_content, {"confirm.md": "C"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].inject_tools == ["show_architecture_diagram"]

    def test_inject_tools_default_empty(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].inject_tools == []


class TestUiMode:
    def test_ui_mode_parsed_from_yaml(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              result: []
            max_rollbacks: 1
            steps:
              - id: select
                conclusion_field: result
                forward: null
                prompt: prompts/select.md
                ui_mode: candidate_selection
                auto_advance: false
        """)
        _write_pipeline(tmp_path, yaml_content, {"select.md": "Select."})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].ui_mode == "candidate_selection"

    def test_ui_mode_defaults_to_none(self, tmp_path):
        _write_pipeline(tmp_path, MINIMAL_YAML, {"step_a.md": "A", "step_b.md": "B"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].ui_mode is None


class TestSkillSchemaExtraction:
    def test_extracts_conclusion_schema_from_skill_frontmatter(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
                skill: my-skill
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        skills_dir = tmp_path / "skills" / "my-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            dedent("""\
            ---
            name: my-skill
            description: test skill
            conclusion_schema:
              type: object
              required: [is_valid]
              properties:
                is_valid:
                  type: boolean
            ---
            # My Skill Content
        """),
            encoding="utf-8",
        )

        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].conclusion_schema == {
            "type": "object",
            "required": ["is_valid"],
            "properties": {"is_valid": {"type": "boolean"}},
        }

    def test_pipeline_yaml_schema_overrides_skill(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
                skill: my-skill
                conclusion_schema:
                  type: object
                  required: [override]
                  properties:
                    override:
                      type: string
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        skills_dir = tmp_path / "skills" / "my-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            dedent("""\
            ---
            name: my-skill
            description: test
            conclusion_schema:
              type: object
              required: [from_skill]
              properties:
                from_skill:
                  type: boolean
            ---
            # Content
        """),
            encoding="utf-8",
        )

        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].conclusion_schema == {
            "type": "object",
            "required": ["override"],
            "properties": {"override": {"type": "string"}},
        }

    def test_no_schema_when_skill_has_none(self, tmp_path):
        _write_pipeline(tmp_path, MINIMAL_YAML, {"step_a.md": "A", "step_b.md": "B"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].conclusion_schema is None

    def test_max_conclusion_retries_from_yaml(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              intent: []
            max_rollbacks: 1
            steps:
              - id: parse
                conclusion_field: intent
                forward: null
                prompt: prompts/parse.md
                max_conclusion_retries: 5
        """)
        _write_pipeline(tmp_path, yaml_content, {"parse.md": "P"})
        loaded = load_pipeline_dir(tmp_path)
        assert loaded.steps[0].max_conclusion_retries == 5

    def test_interrupt_judge_failure_policy_from_yaml(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              deployment: []
            max_rollbacks: 1
            steps:
              - id: deploying
                conclusion_field: deployment
                forward: null
                prompt: prompts/deploy.md
                interrupt_judge_failure: pause
        """)
        _write_pipeline(tmp_path, yaml_content, {"deploy.md": "Deploy"})

        loaded = load_pipeline_dir(tmp_path)

        assert loaded.steps[0].interrupt_judge_failure == "pause"

    def test_rejects_invalid_interrupt_judge_failure_policy(self, tmp_path):
        yaml_content = dedent("""\
            name: test
            context_dependencies:
              deployment: []
            max_rollbacks: 1
            steps:
              - id: deploying
                conclusion_field: deployment
                forward: null
                prompt: prompts/deploy.md
                interrupt_judge_failure: stop_everything
        """)
        _write_pipeline(tmp_path, yaml_content, {"deploy.md": "Deploy"})

        with pytest.raises(ValueError, match="interrupt_judge_failure"):
            load_pipeline_dir(tmp_path)


class TestParseExitCondition:
    def test_none_returns_none(self):
        assert _parse_exit_condition(None, "step_x") is None

    def test_valid_dict_returned_unchanged(self):
        raw = {"field": "done", "value": True}
        assert _parse_exit_condition(raw, "step_x") is raw

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be a dict"):
            _parse_exit_condition("wrong", "step_x")

    def test_missing_field_raises(self):
        with pytest.raises(ValueError, match="must be a dict"):
            _parse_exit_condition({"value": True}, "step_x")

    def test_missing_value_raises(self):
        with pytest.raises(ValueError, match="must be a dict"):
            _parse_exit_condition({"field": "done"}, "step_x")

    def test_error_message_includes_step_id(self):
        with pytest.raises(ValueError, match="step_x"):
            _parse_exit_condition("wrong", "step_x")
