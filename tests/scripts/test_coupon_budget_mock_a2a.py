from types import SimpleNamespace

import pytest

from scripts.ci import run_coupon_budget_mock_a2a as runner


def _summary(**updates):
    return SimpleNamespace(
        **dict(context_id="context-one", task_id="task-one", normal_handoff_ready=False,
               last_status_state="TASK_STATE_INPUT_REQUIRED", response_content_type="text/event-stream") | updates,
    )


@pytest.mark.parametrize("first_text, updates, status, elapsed, expected", [
    ("", {}, "awaiting_selection", 3, True),
    ("", {"normal_handoff_ready": True}, "awaiting_selection", 3, False),
    ("", {}, "rejected", 3, False),
    ("", {}, "selected", 3, False),
    ("", {"last_status_state": "TASK_STATE_FAILED"}, "awaiting_selection", 3, False),
    ("", {}, "awaiting_selection", 10, False),
    ("Incorrect amount: 0", {}, "awaiting_selection", 3, False),
])
def test_only_missing_visible_answer_gets_one_same_task_followup(
    monkeypatch, tmp_path, first_text, updates, status, elapsed, expected,
):
    calls = []
    clock = iter([0, elapsed])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))

    def stream(**options):
        calls.append(options)
        return _summary(**updates) if len(calls) == 1 else _summary()

    monkeypatch.setattr(runner, "stream_message", stream)
    monkeypatch.setattr(runner, "pipeline_facts", lambda _: ({"status": status}, []))
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda p:
                        "Actual model answer" if "-answer." in p.name else first_text)
    _final, path, text, followed_up, transport_ok = runner._answer_turn(
        name="fixture", timeout=10, run_dir=tmp_path, server_url="http://127.0.0.1:1",
        context_id="context-one", task_id="task-one",
    )
    assert followed_up is expected and len(calls) == 1 + int(expected)
    assert text == ("Actual model answer" if expected else first_text)
    assert transport_ok is True
    if expected:
        assert calls[1]["prompt"].startswith(runner.QUESTION)
        assert calls[1]["context_id"] == "context-one" and calls[1]["task_id"] == "task-one"
        assert calls[1]["timeout"] == 7 and path.name == "fixture-answer.events.jsonl"


def test_followup_cannot_replace_a_missing_answer_or_bad_transport_with_success(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: 0)

    def stream(**options):
        calls.append(options)
        return _summary(response_content_type="application/json" if len(calls) == 1 else "text/event-stream")

    monkeypatch.setattr(runner, "stream_message", stream)
    monkeypatch.setattr(runner, "pipeline_facts", lambda _: ({"status": "awaiting_selection"}, []))
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda _: "")
    _final, _path, text, followed_up, transport_ok = runner._answer_turn(
        name="fixture", timeout=10, run_dir=tmp_path, context_id="context-one", task_id="task-one",
    )
    assert followed_up and len(calls) == 2
    assert text == "" and transport_ok is False
    assert not any(runner.evaluate_answer("applicable", text).values())


def test_followup_count_survives_safe_report_projection_without_answer_payload():
    from scripts.ci import run_e2e

    diagnostics = {"coupon_visible_answer_followup_count": 1, "answer": "private model response"}
    public = run_e2e._public_live_summary({"diagnostics": diagnostics})
    assert public["diagnostics"] == {"coupon_visible_answer_followup_count": 1}
    diagnostics["coupon_visible_answer_followup_count"] = "private response"
    assert not run_e2e._public_live_summary({"diagnostics": diagnostics}).get("diagnostics")


def test_silent_implementation_transition_is_not_recovered_as_a_waiting_answer(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: 0)
    monkeypatch.setattr(runner, "stream_message", lambda **kwargs: calls.append(kwargs) or _summary())
    monkeypatch.setattr(runner, "pipeline_facts", lambda _:
                        ({"status": "awaiting_selection"}, ["materialize_selected_candidate"]))
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda _: "")
    _final, _path, text, followed_up, _transport = runner._answer_turn(
        name="fixture", timeout=10, run_dir=tmp_path, context_id="context-one", task_id="task-one",
    )
    assert len(calls) == 1 and not followed_up and text == ""
