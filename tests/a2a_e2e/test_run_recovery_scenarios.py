from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "e2e" / "run_recovery_scenarios.py"
    spec = importlib.util.spec_from_file_location("run_recovery_scenarios", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _input_required_event(kind: str) -> dict:
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "input_required",
                            "data": {"kind": kind},
                        }
                    }
                }
            }
        }
    }


def test_latest_input_required_kind_from_events_uses_latest_kind() -> None:
    runner = _load_runner()

    kind = runner._latest_input_required_kind_from_events(
        [
            _input_required_event("ask_user_question"),
            _input_required_event("candidate_selection"),
        ]
    )

    assert kind == "candidate_selection"


def test_default_recovery_prompt_targets_previous_real_user_question() -> None:
    runner = _load_runner()

    assert "我刚才问了你哪些问题" in runner.DEFAULT_RECOVERY_PROMPT
    assert "最后一条真实用户消息原文" in runner.DEFAULT_RECOVERY_PROMPT
    assert "请完成当前步骤" in runner.DEFAULT_RECOVERY_PROMPT
    assert "[Pipeline Handoff Context]" in runner.DEFAULT_RECOVERY_PROMPT
    assert "更早的方案选择消息" in runner.DEFAULT_RECOVERY_PROMPT


def test_normal_running_recovery_prompt_ignores_continue() -> None:
    runner = _load_runner()

    assert "我刚才问了你哪些问题" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "最后一条真实用户消息原文" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "内容等于“继续”" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "请完成当前步骤" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "更早的方案选择消息" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT


def test_answer_intervening_ask_inputs_reaches_selection(tmp_path: Path) -> None:
    runner = _load_runner()
    initial = runner.StreamSummary(
        name="01-initial",
        prompt="选择一个已有vpc，创建一个vswitch",
        status_states=["TASK_STATE_INPUT_REQUIRED"],
        pipeline_event_types=["input_required"],
        last_input_required_step_id="intent_parsing",
    )
    (tmp_path / "01-initial.events.jsonl").write_text(
        json.dumps(_input_required_event("ask_user_question"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    selection = runner.StreamSummary(
        name="01-initial-answer-ask-1",
        prompt=runner.INTERVENING_ASK_ANSWER,
        status_states=["TASK_STATE_INPUT_REQUIRED"],
        pipeline_event_types=["input_required"],
        last_input_required_step_id="confirm_and_select",
    )
    prompts: list[str] = []

    def stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "01-initial-answer-ask-1"
        return selection

    harness = SimpleNamespace(run_dir=tmp_path, notes=[], stream=stream)

    result = runner._answer_intervening_ask_inputs(harness, initial, name_prefix="01-initial")

    assert result is selection
    assert prompts == [runner.INTERVENING_ASK_ANSWER]
    assert result.last_input_required_step_id == "confirm_and_select"


def test_hydrated_task_checks_require_omitted_request_task_id() -> None:
    runner = _load_runner()
    harness = SimpleNamespace(checks={}, context_id="ctx-1", pipeline_task_id="task-1")
    summary = runner.StreamSummary(
        name="resume",
        prompt="继续",
        request_task_id="",
        context_id="ctx-1",
        task_id="task-1",
    )

    runner._add_hydrated_task_checks(harness, summary, "resume")

    assert harness.checks == {
        "resume omitted taskId": True,
        "resume stayed in recovered context": True,
        "resume hydrated recovered taskId": True,
    }


def test_fault_after_snapshot_continuation_uses_context_only_hydration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    task = {"id": "task-1", "contextId": "ctx-1", "status": {"state": "TASK_STATE_COMPLETED"}}
    task_list_response = {"response": {"result": {"tasks": [task]}}}
    task_get_response = {"response": {"result": task}}
    fake_harnesses = []

    class FakeBackgroundStream:
        def __init__(
            self,
            *,
            prompt: str,
            context_id: str,
            task_id: str,
            name: str,
            **_kwargs,
        ) -> None:
            self.name = name
            self.summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=task_id,
                context_id=context_id,
            )

        def start(self) -> None:
            pass

        def join(self, timeout: float) -> None:
            pass

    class FakeHarness:
        def __init__(self, args) -> None:
            self.args = args
            self.server_url = "http://127.0.0.1:1"
            self.cwd = str(tmp_path)
            self.run_dir = tmp_path
            self.server_env = {}
            self.summaries = {}
            self.snapshots = {}
            self.checks = {}
            self.notes = []
            self.context_id = ""
            self.pipeline_task_id = ""
            self.stream_request_task_ids = []

        def wait_for_server_exit(self, *, expected_returncode: int, timeout: float) -> int:
            return expected_returncode

        def disable_fault_injection(self) -> None:
            pass

        def start_server(self) -> None:
            pass

        def fetch_state(self, name: str):
            return {"snapshot": {"status": "working"}}

        def capture_task_snapshots(self, name: str):
            return {"task_get": task_get_response, "task_list": task_list_response}

        def stream(self, *, prompt: str, name: str, task_id: str | None = None):
            request_task_id = self.pipeline_task_id if task_id is None else task_id
            self.stream_request_task_ids.append(request_task_id)
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=request_task_id,
                context_id=self.context_id,
                task_id=self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                text="created vsw-123",
            )
            self.summaries[name] = summary
            return summary

    def fake_run_with_harness(args, scenario, callback):
        harness = FakeHarness(args)
        fake_harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    monkeypatch.setattr(runner, "BackgroundStream", FakeBackgroundStream)
    monkeypatch.setattr(runner, "fetch_tasks", lambda **_kwargs: task_list_response)
    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)

    args = SimpleNamespace(
        deterministic=True,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        stream_timeout=1,
        event_timeout=1,
    )

    assert runner.run_fault_after_snapshot(args, "fault-after-snapshot") == 0
    assert fake_harnesses[0].stream_request_task_ids == [""]
    assert fake_harnesses[0].checks["continue omitted taskId"] is True
    assert fake_harnesses[0].checks["continue hydrated recovered taskId"] is True


def test_fault_after_snapshot_requires_real_cloud_opt_in_even_when_deterministic() -> None:
    runner = _load_runner()
    args = SimpleNamespace(deterministic=True, allow_real_cloud=False)

    try:
        runner._validate_scenario_execution(args, "fault-after-snapshot")
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError("fault-after-snapshot should require --allow-real-cloud")


def test_fault_after_snapshot_allows_explicit_real_cloud_opt_in() -> None:
    runner = _load_runner()
    args = SimpleNamespace(deterministic=True, allow_real_cloud=True)

    runner._validate_scenario_execution(args, "fault-after-snapshot")


def test_rollback_accepts_security_group_deployment_from_handoff(monkeypatch) -> None:
    runner = _load_runner()
    handoff_summary = (
        "[Pipeline Handoff Context]\n"
        "This is injected context for the assistant, not a user request.\n"
        "Pipeline: selling\n"
        "Outcome: completed\n\n"
        "Included context:\n"
        "{\n"
        '  "deployment": {\n'
        '    "status": "success",\n'
        '    "resources_created": ["ALIYUN::ECS::SecurityGroup"],\n'
        '    "outputs": {"SecurityGroupId": "sg-test"}\n'
        "  }\n"
        "}\n\n"
        "Use this context when answering follow-up questions after the pipeline handoff."
    )
    final_state = {
        "snapshot": {
            "steps": [{"id": "deploying", "status": "completed", "runId": "step-deploying-1"}],
            "normalHandoff": {"summary": handoff_summary},
        }
    }

    class FakeHarness:
        def __init__(self) -> None:
            self.checks: dict[str, bool] = {}
            self.run_dir = Path("/tmp/fake")

        def start_stream(self, **_kwargs):
            return SimpleNamespace()

        def fetch_state(self, name: str):
            if name == "after-rollback-completion":
                return final_state
            return {"snapshot": {"taskId": "task-1"}}

        def kill9_and_restart(self) -> None:
            pass

        def stream(self, **_kwargs):
            return runner.StreamSummary(name="resume", prompt="继续")

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_wait_for_with_intervening_ask_inputs", lambda *args, **kwargs: [args[1][0]])
    monkeypatch.setattr(runner, "_wait_any", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_join_after_kill", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_completed_snapshot_or_stream", lambda *args, **kwargs: True)

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
    )

    assert runner.run_rollback(args, "rollback-step1") == 0
