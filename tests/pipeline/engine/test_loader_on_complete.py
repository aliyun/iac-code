from pathlib import Path
from textwrap import dedent

import pytest

from iac_code.pipeline.engine.loader import load_pipeline_dir


def _write_pipeline(tmp_path: Path, yaml_content: str) -> None:
    (tmp_path / "pipeline.yaml").write_text(yaml_content, encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "step.md").write_text("step", encoding="utf-8")


def _minimal_yaml(extra_top_level: str = "") -> str:
    return f"name: test\n{dedent(extra_top_level)}" + dedent(
        """\
            context_dependencies:
              intent: []
            steps:
              - id: step
                conclusion_field: intent
                forward: null
                prompt: prompts/step.md
            """
    )


def test_omitted_on_complete_defaults_to_none(tmp_path):
    _write_pipeline(tmp_path, _minimal_yaml())

    loaded = load_pipeline_dir(tmp_path)

    assert loaded.on_complete is None


def test_valid_on_complete_config_parses(tmp_path):
    _write_pipeline(
        tmp_path,
        _minimal_yaml(
            dedent(
                """\
                on_complete:
                  action: switch_to_normal
                  apply_on: [completed, early_exit, failed, canceled]
                  handoff_context:
                    include: [intent]
                """
            )
        ),
    )

    loaded = load_pipeline_dir(tmp_path)

    assert loaded.on_complete is not None
    assert loaded.on_complete.action == "switch_to_normal"
    assert loaded.on_complete.apply_on == ["completed", "early_exit", "failed", "canceled"]
    assert loaded.on_complete.handoff_context.include == ["intent"]


def test_unknown_on_complete_action_raises(tmp_path):
    _write_pipeline(
        tmp_path,
        _minimal_yaml(
            dedent(
                """\
                on_complete:
                  action: keep_pipeline
                """
            )
        ),
    )

    with pytest.raises(ValueError, match="on_complete.action"):
        load_pipeline_dir(tmp_path)


def test_unsupported_on_complete_outcome_raises(tmp_path):
    _write_pipeline(
        tmp_path,
        _minimal_yaml(
            dedent(
                """\
                on_complete:
                  action: switch_to_normal
                  apply_on: [completed, aborted]
                """
            )
        ),
    )

    with pytest.raises(ValueError, match="on_complete.apply_on"):
        load_pipeline_dir(tmp_path)


def test_falsy_non_mapping_handoff_context_raises(tmp_path):
    _write_pipeline(
        tmp_path,
        _minimal_yaml(
            dedent(
                """\
                on_complete:
                  action: switch_to_normal
                  handoff_context: []
                """
            )
        ),
    )

    with pytest.raises(ValueError, match="on_complete.handoff_context"):
        load_pipeline_dir(tmp_path)
