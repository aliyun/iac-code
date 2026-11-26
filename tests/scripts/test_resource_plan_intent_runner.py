"""Offline acceptance contracts; no LLM, network, or user configuration."""

from pathlib import Path

import pytest
from PIL import Image

from scripts.ci import run_e2e
from scripts.ci.run_resource_plan_intent_a2a import (
    candidates_ready,
    fixture_image,
    intent_question,
    question_answer,
    turn_checks,
)


def ask() -> dict:
    return {
        "kind": "ask_user_question",
        "question": "仅分析配置还是生成部署方案？",
        "options": [{"id": "consult", "label": "仅分析，不部署"}, {"id": "plan", "label": "生成部署方案"}],
        "allowFreeText": True,
    }


def events(*, rejected: bool = False) -> list[dict]:
    result = [{"eventType": "text_delta", "data": {"text": "图片包含 ECS、负载均衡和数据库，适合多可用区架构。"}}]
    if rejected:
        result.append({"eventType": "step_completed", "data": {"normalizedConclusion": {"status": "rejected"}}})
    result.append({"eventType": "input_required", "data": ask()})
    return result


def checks(items: list[dict], *, branch: str = "clarify-deploy", first: bool = True, handoff: bool = False) -> dict:
    return turn_checks(items, state="TASK_STATE_INPUT_REQUIRED", handoff=handoff, branch=branch, first=first)


def test_ambiguous_image_requires_real_intent_question_after_analysis():
    assert all(checks(events()).values())
    assert not all(checks(events()[1:]).values())  # Asking without analysis is insufficient.
    assert not all(checks(events(rejected=True)).values())  # A later question cannot undo early exit.
    assert not all(checks(events(), handoff=True).values())


@pytest.mark.parametrize(
    "question",
    [
        {"kind": "permission", "question": "允许分析并部署？"},
        {"kind": "ask_user_question", "question": "在哪个地域部署？"},
        {"kind": "ask_user_question", "question": "是否继续？"},
    ],
)
def test_other_inputs_are_not_deployment_intent_clarification(question):
    assert not intent_question(question)
    items = events()
    items[-1]["data"] = question
    assert not all(checks(items).values())


def test_answer_selects_actual_option_id_in_either_order():
    question = ask()
    for _ in range(2):
        assert question_answer(question, deploy=True) == "plan"
        assert question_answer(question, deploy=False) == "consult"
        question["options"].reverse()


def test_answer_does_not_invent_option_for_unanswerable_question():
    with pytest.raises(ValueError):
        question_answer({"options": [], "allowFreeText": False}, deploy=True)
    assert "不部署资源" in question_answer({"allowFreeText": True}, deploy=True)


def test_cost_analysis_inside_deployment_option_is_not_consultation_only():
    question = {
        "options": [
            {"id": "consult", "label": "仅分析，不生成部署方案"},
            {"id": "plan", "label": "生成方案并分析成本，暂不部署"},
        ]
    }
    assert question_answer(question, deploy=True) == "plan"
    assert question_answer(question, deploy=False) == "consult"


def test_explicit_deployment_must_not_be_asked_intent_again_or_handed_off():
    assert all(checks([], branch="explicit-deploy").values())
    assert not all(checks(events(), branch="explicit-deploy").values())
    assert not all(checks(events(rejected=True), branch="explicit-deploy", handoff=True).values())
    assert not all(
        checks(
            [{"eventType": "step_started", "step": {"id": "materialize_selected_candidate"}}], branch="explicit-deploy"
        ).values()
    )


def test_explicit_consultation_accepts_rejection_only_with_handoff():
    assert all(checks(events(rejected=True), branch="explicit-consult", handoff=True).values())
    assert not all(checks(events(), branch="explicit-consult", handoff=True).values())
    assert not all(checks(events(rejected=True), branch="explicit-consult").values())


def test_deployment_continuation_requires_actual_candidates():
    assert candidates_ready({"status": "awaiting_selection", "candidates": [{"id": "candidate-1"}]})
    assert not candidates_ready({"status": "awaiting_selection", "candidates": []})
    assert not candidates_ready({"status": "rejected", "candidates": [{"id": "candidate-1"}]})
    assert not candidates_ready({})


def test_synthetic_image_is_repeatable_and_has_no_private_input(tmp_path: Path):
    first = fixture_image(tmp_path / "a.png")
    second = fixture_image(tmp_path / "b.png")
    assert first.read_bytes() == second.read_bytes()
    with Image.open(first) as image:
        assert image.size == (780, 300)


def test_ci_uses_one_multimodal_model_without_cloud_credentials(tmp_path: Path):
    for name in ("settings.yml", ".credentials.yml"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    args = run_e2e.parse_args(
        [
            "--case",
            "resource-plan-intent-a2a",
            "--credential-source-dir",
            str(tmp_path),
            "--case-model",
            "resource-plan-intent-a2a=qwen3.8-flash",
        ]
    )
    (case,) = run_e2e.select_cases(args)
    assert case.multimodal and case.live_runner in run_e2e.LLM_ONLY_RUNNERS
    assert case.group == "readonly" and not case.cloud_write
    assert run_e2e._live_cleanup_status(case, None) == "not-needed"
