from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import yaml

from iac_code.pipeline.engine.handoff import (
    build_handoff_summary,
    candidate_progress_from_execution,
    terminal_outcome_from_completed_event,
)
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
    resource_release_requirement = (
        "- Before performing any operation that releases, deletes, or otherwise destroys a resource, "
        "obtain a fresh, explicit confirmation from the user in normal chat. Any confirmation given "
        "during the pipeline does not count."
    )
    automatic_cleanup_exception = (
        "- Exception: pipeline-managed automatic cleanup may proceed without this additional confirmation."
    )

    assert summary == dedent(
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

        Safety requirements for normal chat:
        RESOURCE_RELEASE_REQUIREMENT
        AUTOMATIC_CLEANUP_EXCEPTION

        Use this context when answering follow-up questions after the pipeline handoff.
        """
    ).strip().replace("RESOURCE_RELEASE_REQUIREMENT", resource_release_requirement).replace(
        "AUTOMATIC_CLEANUP_EXCEPTION", automatic_cleanup_exception
    )
    assert "architecture" not in summary
    assert "deployment" not in summary


def test_build_handoff_summary_requires_new_confirmation_for_resource_release_except_automatic_cleanup():
    summary = build_handoff_summary(
        pipeline_name="selling",
        outcome="completed",
        context_snapshot={},
        include_fields=[],
    )

    assert "obtain a fresh, explicit confirmation from the user in normal chat" in summary
    assert "Any confirmation given during the pipeline does not count" in summary
    assert "pipeline-managed automatic cleanup may proceed without this additional confirmation" in summary


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


def _evaluate_candidates_execution() -> dict:
    return {
        "kind": "parallel_sub_pipeline",
        "step_id": "evaluate_candidates",
        "sub_pipeline_name": "evaluate_candidate",
        "candidates": {
            "1": {
                "status": "running",
                "candidate": {"name": "低成本单机"},
                "current_sub_step": "template_generating",
                "step_conclusions": {},
                "tool_result_cache": {"k1": {}, "k2": {}, "k3": {}},
            },
            "0": {
                "status": "running",
                "candidate": {"name": "高可用多可用区"},
                "current_sub_step": "cost_estimating",
                "step_conclusions": {"template_generating": {"file_path": "ha.yaml"}},
                "tool_result_cache": {"k1": {}},
            },
        },
    }


def test_candidate_progress_from_execution_lists_completed_and_pending_sub_steps():
    progress = candidate_progress_from_execution(
        _evaluate_candidates_execution(),
        ["template_generating", "cost_estimating"],
    )

    assert progress == [
        {
            "candidate_index": 0,
            "candidate_name": "高可用多可用区",
            "status": "running",
            "current_sub_step": "cost_estimating",
            "completed_sub_steps": ["template_generating"],
            "pending_sub_steps": ["cost_estimating"],
            "cached_tool_results": 1,
        },
        {
            "candidate_index": 1,
            "candidate_name": "低成本单机",
            "status": "running",
            "current_sub_step": "template_generating",
            "completed_sub_steps": [],
            "pending_sub_steps": ["template_generating", "cost_estimating"],
            "cached_tool_results": 3,
        },
    ]


def test_candidate_progress_from_execution_ignores_unrelated_execution_state():
    assert candidate_progress_from_execution(None, ["template_generating"]) == []
    assert candidate_progress_from_execution({}, ["template_generating"]) == []
    assert candidate_progress_from_execution({"kind": "step", "candidates": {}}, ["template_generating"]) == []
    assert (
        candidate_progress_from_execution(
            {"kind": "parallel_sub_pipeline", "candidates": "nope"},
            ["template_generating"],
        )
        == []
    )


def test_build_handoff_summary_includes_machine_readable_candidate_progress():
    progress = candidate_progress_from_execution(
        _evaluate_candidates_execution(),
        ["template_generating", "cost_estimating"],
    )

    summary = build_handoff_summary(
        pipeline_name="selling",
        outcome="canceled",
        context_snapshot={"intent": {"summary": "部署 NAS"}},
        include_fields=["intent", "evaluated_candidates"],
        candidate_progress=progress,
    )

    assert "Candidate sub-step progress:" in summary
    assert '"candidate_index": 0' in summary
    assert '"completed_sub_steps": [' in summary
    assert '"pending_sub_steps": [' in summary
    assert '"cached_tool_results": 3' in summary
    assert "reuse them instead of re-evaluating the candidate from scratch" in summary
    # Existing handoff structure is preserved.
    assert "Missing context fields:\n- evaluated_candidates" in summary


def test_build_handoff_summary_omits_progress_section_without_candidates():
    summary = build_handoff_summary(
        pipeline_name="selling",
        outcome="completed",
        context_snapshot={},
        include_fields=[],
    )

    assert "Candidate sub-step progress:" not in summary


def test_runner_build_normal_handoff_summary_includes_candidate_progress(tmp_path):
    runner = _make_runner(tmp_path, _switch_policy("canceled", include=["intent"]))
    runner.context.set_conclusion("intent", {"summary": "部署 NAS"})
    runner._execution = _evaluate_candidates_execution()

    summary = runner.build_normal_handoff_summary({"canceled": True})

    assert "Outcome: canceled" in summary
    assert "Candidate sub-step progress:" in summary
    assert '"candidate_name": "高可用多可用区"' in summary
