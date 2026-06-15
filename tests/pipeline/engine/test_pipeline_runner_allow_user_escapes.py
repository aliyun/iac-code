"""Test PipelineRunner exposes allow_user_escapes from loaded pipeline."""

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.step_spec import AllowUserEscapes


def _make_runner(tmp_path: Path, escapes: dict | None = None, emit_stack_events: bool | None = None) -> PipelineRunner:
    body = {
        "name": "t",
        "context_dependencies": {"x": []},
        "steps": [{"id": "s1", "conclusion_field": "x", "forward": None, "prompt": "prompts/s1.md"}],
    }
    if escapes is not None:
        body["allow_user_escapes"] = escapes
    if emit_stack_events is not None:
        body["emit_stack_events"] = emit_stack_events
    (tmp_path / "pipeline.yaml").write_text(yaml.dump(body), encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "s1.md").write_text("step 1", encoding="utf-8")
    return PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=MagicMock(),
        session_id="s1",
        cwd=str(tmp_path),
    )


def test_runner_exposes_allow_user_escapes_default(tmp_path):
    runner = _make_runner(tmp_path)
    assert isinstance(runner.allow_user_escapes, AllowUserEscapes)
    assert runner.allow_user_escapes.shell is False


def test_runner_exposes_allow_user_escapes_configured(tmp_path):
    runner = _make_runner(tmp_path, escapes={"shell": True})
    assert runner.allow_user_escapes.shell is True


def test_runner_exposes_emit_stack_events(tmp_path):
    runner = _make_runner(tmp_path, emit_stack_events=True)
    assert runner.emit_stack_events is True
