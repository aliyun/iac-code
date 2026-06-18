from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import yaml

from iac_code.pipeline.engine.handoff import build_handoff_summary, terminal_outcome_from_completed_event
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner


def _make_runner(tmp_path: Path, on_complete: dict | None = None) -> PipelineRunner:
    body = {
        "name": "test",
        "context_dependencies": {
            "intent": [],
            "architecture": ["intent"],
        },
        "steps": [
            {
                "id": "step",
                "conclusion_field": "intent",
                "forward": None,
                "prompt": "prompts/step.md",
            }
        ],
    }
    if on_complete is not None:
        body["on_complete"] = on_complete

    (tmp_path / "pipeline.yaml").write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "step.md").write_text("step", encoding="utf-8")

    return PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=MagicMock(),
        session_id="session",
        cwd=str(tmp_path),
    )


def _switch_policy(*apply_on: str, include: list[str] | None = None) -> dict:
    return {
        "action": "switch_to_normal",
        "apply_on": list(apply_on),
        "handoff_context": {"include": include or ["intent"]},
    }


def test_terminal_outcome_from_completed_event_completed():
    assert terminal_outcome_from_completed_event({"total_steps": 3}) == "completed"


def test_terminal_outcome_from_completed_event_early_exit():
    assert terminal_outcome_from_completed_event({"early_exit": True}) == "early_exit"


def test_terminal_outcome_from_completed_event_failed():
    assert terminal_outcome_from_completed_event({"failed": True}) == "failed"


def test_terminal_outcome_from_completed_event_canceled():
    assert terminal_outcome_from_completed_event({"canceled": True}) == "canceled"


def test_terminal_outcome_failed_wins_over_early_exit():
    assert terminal_outcome_from_completed_event({"failed": True, "early_exit": True}) == "failed"


def test_build_handoff_summary_includes_only_configured_fields_and_deterministic_metadata():
    summary = build_handoff_summary(
        pipeline_name="selling",
        outcome="early_exit",
        context_snapshot={
            "intent": {"summary": "部署 nginx", "region": "cn-hangzhou"},
            "architecture": {"candidates": ["ecs"]},
            "deployment": {"status": "skipped"},
        },
        include_fields=["intent", "missing_field"],
    )

    assert (
        summary
        == dedent(
            """\
        [Pipeline Handoff Context]
        This is injected context for the assistant, not a user request.
        Pipeline: selling
        Outcome: early_exit

        Included context:
        {
          "intent": {
            "summary": "部署 nginx",
            "region": "cn-hangzhou"
          }
        }

        Missing context fields:
        - missing_field

        Use this context when answering follow-up questions after the pipeline handoff.
        """
        ).strip()
    )
    assert "architecture" not in summary
    assert "deployment" not in summary


def test_runner_should_switch_to_normal_for_completed_policy(tmp_path):
    runner = _make_runner(tmp_path, _switch_policy("completed"))

    assert runner.on_complete_policy is not None
    assert runner.should_switch_to_normal({"total_steps": 1}) is True


def test_runner_should_switch_to_normal_for_configured_early_exit(tmp_path):
    runner = _make_runner(tmp_path, _switch_policy("early_exit"))

    assert runner.should_switch_to_normal({"early_exit": True}) is True


def test_runner_should_not_switch_when_policy_omitted(tmp_path):
    runner = _make_runner(tmp_path)

    assert runner.on_complete_policy is None
    assert runner.should_switch_to_normal({"total_steps": 1}) is False


def test_runner_should_not_switch_for_failed_event(tmp_path):
    runner = _make_runner(tmp_path, _switch_policy("completed", "early_exit"))

    assert runner.should_switch_to_normal({"failed": True}) is False


def test_runner_should_switch_to_normal_for_configured_failed_event(tmp_path):
    runner = _make_runner(tmp_path, _switch_policy("completed", "failed"))

    assert runner.should_switch_to_normal({"failed": True}) is True


def test_runner_should_switch_to_normal_for_configured_canceled_event(tmp_path):
    runner = _make_runner(tmp_path, _switch_policy("completed", "canceled"))

    assert runner.should_switch_to_normal({"canceled": True}) is True


def test_runner_build_normal_handoff_summary_uses_configured_context_values(tmp_path):
    runner = _make_runner(tmp_path, _switch_policy("completed", include=["intent", "architecture"]))
    runner.context.set_conclusion("intent", {"summary": "deploy nginx"})

    summary = runner.build_normal_handoff_summary({"total_steps": 1})

    assert "Pipeline: test" in summary
    assert "Outcome: completed" in summary
    assert '"summary": "deploy nginx"' in summary
    assert "Missing context fields:\n- architecture" in summary
