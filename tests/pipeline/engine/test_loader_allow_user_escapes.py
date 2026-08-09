"""Regression tests for allow_user_escapes parsing (问题 5)."""

from pathlib import Path

import pytest
import yaml

from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_spec import A2AArtifactSpec, AllowUserEscapes


def _write_pipeline(tmp_path: Path, extra: dict) -> Path:
    body = {
        "name": "t",
        "context_dependencies": {"x": []},
        "steps": [
            {
                "id": "s1",
                "conclusion_field": "x",
                "forward": None,
                "prompt": "prompts/s1.md",
            }
        ],
    }
    body.update(extra)
    (tmp_path / "pipeline.yaml").write_text(yaml.dump(body), encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "s1.md").write_text("step 1", encoding="utf-8")
    return tmp_path


def test_loader_defaults_allow_user_escapes_all_false(tmp_path):
    """问题 5：缺失 allow_user_escapes 时三个开关全 false。"""
    _write_pipeline(tmp_path, {})
    loaded = load_pipeline_dir(tmp_path)
    assert loaded.allow_user_escapes == AllowUserEscapes(skill=False, command=False, shell=False)
    assert loaded.emit_stack_events is False


def test_loader_parses_allow_user_escapes_partial(tmp_path):
    """部分声明：未声明字段保持 false 默认。"""
    _write_pipeline(tmp_path, {"allow_user_escapes": {"shell": True}})
    loaded = load_pipeline_dir(tmp_path)
    assert loaded.allow_user_escapes.shell is True
    assert loaded.allow_user_escapes.skill is False
    assert loaded.allow_user_escapes.command is False


def test_loader_parses_allow_user_escapes_full(tmp_path):
    """三个开关全 true 应被原样解析。"""
    _write_pipeline(
        tmp_path,
        {"allow_user_escapes": {"skill": True, "command": True, "shell": True}},
    )
    loaded = load_pipeline_dir(tmp_path)
    assert loaded.allow_user_escapes.skill is True
    assert loaded.allow_user_escapes.command is True
    assert loaded.allow_user_escapes.shell is True


def test_loader_parses_emit_stack_events(tmp_path):
    _write_pipeline(tmp_path, {"emit_stack_events": True})
    loaded = load_pipeline_dir(tmp_path)
    assert loaded.emit_stack_events is True


def test_loader_parses_step_a2a_artifacts(tmp_path):
    _write_pipeline(
        tmp_path,
        {
            "steps": [
                {
                    "id": "s1",
                    "conclusion_field": "x",
                    "forward": None,
                    "prompt": "prompts/s1.md",
                    "config": {"mode": "strict", "max_findings": 5},
                    "a2a_artifacts": [
                        {
                            "path": "conclusion.file_path",
                            "content": "conclusion.template",
                            "media_type": "auto",
                            "role": "intermediate",
                            "supersedesPath": "conclusion.original_file_path",
                        }
                    ],
                }
            ]
        },
    )

    loaded = load_pipeline_dir(tmp_path)

    assert loaded.steps[0].config == {"mode": "strict", "max_findings": 5}
    assert loaded.steps[0].a2a_artifacts == [
        A2AArtifactSpec(
            path="conclusion.file_path",
            content="conclusion.template",
            media_type="auto",
            role="intermediate",
            supersedes_path="conclusion.original_file_path",
        )
    ]


@pytest.mark.parametrize("config", [["strict"], "strict"])
def test_loader_rejects_invalid_step_config(tmp_path, config):
    _write_pipeline(
        tmp_path,
        {
            "steps": [
                {
                    "id": "s1",
                    "conclusion_field": "x",
                    "forward": None,
                    "prompt": "prompts/s1.md",
                    "config": config,
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="Step 's1'.*config"):
        load_pipeline_dir(tmp_path)


def test_loader_resolves_a2a_artifact_roles_after_filtering(tmp_path):
    _write_pipeline(
        tmp_path,
        {
            "context_dependencies": {"template": [], "review": []},
            "feature_flags": {"review_enabled": {"default": True}},
            "steps": [
                {
                    "id": "template_generating",
                    "conclusion_field": "template",
                    "forward": "review",
                    "prompt": "prompts/s1.md",
                    "a2a_artifacts": [
                        {
                            "path": "conclusion.file_path",
                            "content": "conclusion.template",
                        }
                    ],
                },
                {
                    "id": "review",
                    "conclusion_field": "review",
                    "forward": None,
                    "prompt": "prompts/s1.md",
                    "enabled_when": "review_enabled",
                    "a2a_artifacts": [
                        {
                            "path": "conclusion.reviewed_file_path",
                            "content": "conclusion.reviewed_template",
                            "supersedes_path": "conclusion.file_path",
                        }
                    ],
                },
            ],
        },
    )

    loaded = load_pipeline_dir(tmp_path)

    assert loaded.steps[0].a2a_artifacts[0].role == "intermediate"
    assert loaded.steps[1].a2a_artifacts[0].role == "final"

    filtered = load_pipeline_dir(tmp_path, feature_flag_overrides={"review_enabled": False})

    assert [step.step_id for step in filtered.steps] == ["template_generating"]
    assert filtered.steps[0].a2a_artifacts[0].role == "final"


def test_loader_resolves_chained_a2a_artifact_replacements(tmp_path):
    _write_pipeline(
        tmp_path,
        {
            "context_dependencies": {"a": [], "b": [], "c": []},
            "steps": [
                {
                    "id": "a",
                    "conclusion_field": "a",
                    "forward": "b",
                    "prompt": "prompts/s1.md",
                    "a2a_artifacts": [
                        {
                            "path": "conclusion.a",
                            "content": "conclusion.template",
                        }
                    ],
                },
                {
                    "id": "b",
                    "conclusion_field": "b",
                    "forward": "c",
                    "prompt": "prompts/s1.md",
                    "a2a_artifacts": [
                        {
                            "path": "conclusion.b",
                            "content": "conclusion.template",
                            "supersedes_path": "conclusion.a",
                        }
                    ],
                },
                {
                    "id": "c",
                    "conclusion_field": "c",
                    "forward": None,
                    "prompt": "prompts/s1.md",
                    "a2a_artifacts": [
                        {
                            "path": "conclusion.c",
                            "content": "conclusion.template",
                            "supersedes_path": "conclusion.b",
                        }
                    ],
                },
            ],
        },
    )

    loaded = load_pipeline_dir(tmp_path)

    assert [step.a2a_artifacts[0].role for step in loaded.steps] == ["intermediate", "intermediate", "final"]


def test_selling_cost_estimating_reads_template_without_reemitting_template_artifact() -> None:
    selling_dir = Path(__file__).parents[3] / "src" / "iac_code" / "pipeline" / "selling"

    loaded = load_pipeline_dir(selling_dir)

    template_step, cost_step = loaded.sub_pipelines["evaluate_candidate"].steps
    assert template_step.step_id == "template_generating"
    assert template_step.a2a_artifacts[0].role == "final"
    assert cost_step.step_id == "cost_estimating"
    assert cost_step.context_fields == ["candidate", "template"]
    assert cost_step.a2a_artifacts == []
