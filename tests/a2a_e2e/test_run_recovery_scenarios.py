from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "e2e" / "run_recovery_scenarios.py"
    spec = importlib.util.spec_from_file_location("run_recovery_scenarios", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_recovery_ci_diagnostics_keep_only_fixed_evidence(tmp_path: Path) -> None:
    runner = _load_runner()
    control_dir = tmp_path / "a2a-persistence" / "execution-control"
    control_dir.mkdir(parents=True)
    (control_dir / "ctx-1.json").write_text(
        json.dumps({
            "taskId": "task-1", "phase": "running", "executionStatus": "working",
            "releaseReady": False, "inputHandoffReady": False, "streamAvailable": True,
            "blockers": [{"kind": "execution", "secret": "private-data"}],
        }),
        encoding="utf-8",
    )
    state = runner._control_state_diagnostic(tmp_path, "ctx-1", "task-1")
    summary = runner.StreamSummary(
        name="continue", prompt="private prompt", terminal_status_text="Active execution: private-data"
    )

    assert state == {
        "present": True, "task_matches": True, "phase": "running", "execution_status": "working",
        "release_ready": False, "input_handoff_ready": False, "stream_available": True, "blocker_count": 1,
        "subprocess_tracking": False, "active_subprocess_tools": None,
        "external_operation_count": None, "revision_settled": False, "backup_status": None,
    }
    assert runner._terminal_markers([summary]) == ["execution"]
    assert "private-data" not in json.dumps(state)
    assert runner._control_state_diagnostic(tmp_path, "../ctx-1", "task-1") == {"present": False}
    h = SimpleNamespace(run_dir=tmp_path, context_id="ctx-1", pipeline_task_id="task-1", diagnostics={})
    summary.task_id = "private-normal-task"
    summary.status_states = ["TASK_STATE_INPUT_REQUIRED"]
    runner._record_image_normal_checkpoint(h, "after_normal_followup", summary)
    checkpoint = h.diagnostics["image_normal_handoff_checkpoints"]["after_normal_followup"]
    assert checkpoint["control_state"]["task_matches"] is True
    assert checkpoint["control_state"]["stream_task_matches"] is False
    assert checkpoint["a2a_states"] == ["TASK_STATE_INPUT_REQUIRED"]
    assert "private" not in json.dumps(checkpoint)
    summary.terminal_status_text = "Current execution is active in another process: private-id"
    assert "persisted_owner_conflict" in runner._terminal_markers([summary])


def test_cleanup_failure_code_extraction_keeps_only_code_and_http_status() -> None:
    runner = _load_runner()
    assert runner._cleanup_failure_code_and_http_status(
        "Alibaba Cloud API ROS/DeleteStack returned HTTP 409 with error code StackInOperation. sk-fixture"
    ) == ("StackInOperation", 409)
    assert runner._cleanup_failure_code_and_http_status("private provider error sk-fixture") == ("", None)
    assert runner._cleanup_failure_kind("StackInOperation") == "resource_busy"
    assert runner._cleanup_failure_kind("private provider error sk-fixture") == "unknown"


def test_normal_handoff_waits_for_durable_owner_release_without_restarting_or_retrying(monkeypatch, tmp_path):
    runner = _load_runner()
    h = SimpleNamespace(run_dir=tmp_path, context_id="ctx", pipeline_task_id="pipeline", diagnostics={})
    now = [0.0]
    reads = []
    ready = {"present": True, "task_matches": True, "phase": "terminated", "release_ready": True,
             "blocker_count": 0, "active_subprocess_tools": 0, "revision_settled": True}

    def read(root, context, task):
        assert (root, context, task) == (tmp_path, "ctx", "pipeline")
        reads.append(now[0])
        return ready if len(reads) == 3 else {**ready, "phase": "running", "release_ready": False}

    def advance(delay):
        now[0] += delay

    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=advance))
    monkeypatch.setattr(runner, "_control_state_diagnostic", read)
    runner._wait_completed_execution_release(h, timeout=1)
    assert reads == [0, 0.05, 0.1]
    assert h.diagnostics["normal_handoff_wait_completed"] is True
    assert h.diagnostics["normal_handoff_wait_poll_count"] == 3


@pytest.mark.parametrize(("field", "value"), [
    ("present", False), ("task_matches", False), ("phase", "running"), ("release_ready", False),
    ("blocker_count", 1), ("active_subprocess_tools", 1), ("revision_settled", False),
])
def test_normal_handoff_does_not_guess_release_from_completed_pipeline_snapshot(monkeypatch, tmp_path, field, value):
    runner = _load_runner()
    h = SimpleNamespace(run_dir=tmp_path, context_id="ctx", pipeline_task_id="pipeline", diagnostics={})
    control = {"present": True, "task_matches": True, "phase": "terminated", "release_ready": True,
               "blocker_count": 0, "active_subprocess_tools": 0, "revision_settled": True, field: value}
    monkeypatch.setattr(runner, "_control_state_diagnostic", lambda *a: control)
    with pytest.raises(TimeoutError, match="did not release ownership"):
        runner._wait_completed_execution_release(h, timeout=0)
    assert h.diagnostics["normal_handoff_wait_completed"] is False


def test_normal_image_task_frame_without_execution_cannot_reach_restart(monkeypatch):
    runner = _load_runner()
    summary = runner.StreamSummary(name="normal", prompt="image", task_id="new", context_id="ctx",
                                   status_states=["TASK_STATE_INPUT_REQUIRED"], text="response")
    h = SimpleNamespace(checks={}, diagnostics={}, context_id="ctx", pipeline_task_id="pipeline",
                        stream_image_text=lambda **kw: summary,
                        kill9_and_restart=lambda: pytest.fail("unowned response must not reach restart"))
    monkeypatch.setattr(runner, "_complete_pipeline", lambda *a: None)
    monkeypatch.setattr(runner, "_wait_completed_execution_release", lambda *a, **k: None)

    def checkpoint(_h, stage, _summary=None):
        h.diagnostics.setdefault("image_normal_handoff_checkpoints", {})[stage] = {
            "control_state": {"stream_task_matches": False}}

    monkeypatch.setattr(runner, "_record_image_normal_checkpoint", checkpoint)

    def execute(_args, _case, callback):
        with pytest.raises(RuntimeError, match="its own execution before restart"):
            callback(h)
        return 1

    monkeypatch.setattr(runner, "_run_with_harness", execute)
    args = SimpleNamespace(event_timeout=120, normal_followup_prompt="question")
    assert runner.run_image_normal_handoff(args, "image-normal-handoff") == 1
    assert h.checks["normal image follow-up used a new task"] is False
    assert h.checks["normal image follow-up finished turn"] is True


def test_recovery_harness_records_failure_location_without_relying_on_error_text(monkeypatch) -> None:
    runner = _load_runner()
    result = {}

    class FakeHarness:
        def __init__(self, _args, *, scenario):
            self.scenario = scenario
            self.failure_stage = "post_rollback_confirmation"
            self.run_dir = Path("unused")
            self.context_id = self.pipeline_task_id = ""
            self.diagnostics = {}
            self.notes = []
            self.checks = {}

        def preflight(self):
            pass

        def start_server(self):
            pass

        def terminate(self):
            pass

        def finish(self, **kwargs):
            result.update(kwargs)
            return 1

    monkeypatch.setattr(runner, "ScenarioHarness", FakeHarness)

    def fail(_harness):
        raise TimeoutError("private token sk-fixture")

    assert runner._run_with_harness(SimpleNamespace(ci_teardown=False), "rollback-step5", fail) == 1
    assert result["passed"] is False
    assert result["error_type"] == "TimeoutError"
    assert result["error_site"].startswith("scripts/a2a/e2e/run_recovery_scenarios.py:")


def _input_required_event(kind: str = "", *, step_id: str = "") -> dict:
    data = {}
    if kind:
        data["kind"] = kind
    if step_id:
        data["stepId"] = step_id
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "input_required",
                            "step": {"id": step_id} if step_id else {},
                            "data": data,
                        }
                    }
                }
            }
        }
    }


def _pipeline_batch(*envelopes: dict) -> dict:
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipelineBatch": {
                            "events": list(envelopes),
                        }
                    }
                }
            }
        }
    }


def test_top_level_task_status_message_is_preserved() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="recovered", prompt="continue")
    runner._apply_event(summary, {
        "task": {
            "id": "task-fixture",
            "contextId": "context-fixture",
            "status": {
                "state": "TASK_STATE_FAILED",
                "message": {"parts": [{"text": "recovery failure fixture"}]},
            },
        },
    })

    assert summary.last_status_state == "TASK_STATE_FAILED"
    assert summary.text == "recovery failure fixture"
    assert summary.terminal_status_text == "recovery failure fixture"


def test_latest_input_required_kind_from_events_uses_latest_kind() -> None:
    runner = _load_runner()

    kind = runner._latest_input_required_kind_from_events(
        [
            _input_required_event("ask_user_question"),
            _input_required_event("candidate_selection"),
        ]
    )

    assert kind == "candidate_selection"


def test_waiting_for_followup_ask_distinguishes_candidate_selection(tmp_path: Path) -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(
        name="02-answer-first-ask",
        prompt=runner.ASK_FIRST_ANSWER,
        pipeline_event_types=["input_required"],
        last_input_required_step_id="confirm_and_select",
    )
    events_path = tmp_path / "02-answer-first-ask.events.jsonl"
    events_path.write_text(
        json.dumps(_input_required_event("candidate_selection", step_id="confirm_and_select")) + "\n",
        encoding="utf-8",
    )
    harness = SimpleNamespace(run_dir=tmp_path)

    assert runner._waiting_for_followup_ask(harness, summary) is False

    events_path.write_text(
        json.dumps(_input_required_event("ask_user_question", step_id="intent_parsing")) + "\n",
        encoding="utf-8",
    )
    assert runner._waiting_for_followup_ask(harness, summary) is True


def test_deployment_success_requires_latest_success_and_stack_id(tmp_path: Path) -> None:
    runner = _load_runner()
    events_path = tmp_path / "02-answer.events.jsonl"

    def completed(sequence: int, conclusion: dict) -> dict:
        return _pipeline_batch(
            {
                "eventType": "step_completed",
                "sequence": sequence,
                "step": {"id": "deploying"},
                "data": {"conclusion": conclusion},
            }
        )

    events_path.write_text(
        "\n".join(
            [
                json.dumps(completed(1, {"status": "success", "stack_id": "stack-1"})),
                json.dumps(completed(2, {"status": "failed", "resources_created": []})),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert runner._deployment_succeeded_with_stack_id(tmp_path) is False

    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(completed(3, {"status": "success", "outputs": {"StackId": "stack-2"}})) + "\n")
    assert runner._deployment_succeeded_with_stack_id(tmp_path) is True


def test_recovery_predicates_and_evidence_inspect_every_batched_event() -> None:
    runner = _load_runner()
    event = _pipeline_batch(
        {"eventType": "text_delta", "data": {"text": "before"}},
        {
            "eventType": "input_required",
            "step": {"id": "confirm_and_select"},
            "data": {"kind": "candidate_selection", "stepId": "confirm_and_select"},
        },
        {"eventType": "rollback_completed", "data": {}},
    )

    assert runner._input_required_step("confirm_and_select")(event, None) is True
    assert runner._event_type("rollback_completed")(event, None) is True
    assert runner._latest_input_required_kind_from_events([event]) == "candidate_selection"
    assert runner._latest_input_required_step_id_from_events([event]) == "confirm_and_select"


def test_default_recovery_prompt_targets_previous_real_user_question() -> None:
    runner = _load_runner()

    assert "我刚才问了你哪些问题" in runner.DEFAULT_RECOVERY_PROMPT
    assert "最后一条真实用户消息原文" in runner.DEFAULT_RECOVERY_PROMPT
    assert "请完成当前步骤" in runner.DEFAULT_RECOVERY_PROMPT
    assert "[Pipeline Handoff Context]" in runner.DEFAULT_RECOVERY_PROMPT
    assert "更早的方案选择消息" in runner.DEFAULT_RECOVERY_PROMPT


def test_ci_recovery_records_private_case_session_without_constraining_stack_name(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--scenario", "scenario1", "--run-dir", str(tmp_path), "--ci-teardown"])
    harness = runner.ScenarioHarness(args, scenario="scenario1")
    manifest = json.loads((tmp_path / "owned-stacks.json").read_text(encoding="utf-8"))

    assert manifest["cwd"] == harness.cwd
    assert manifest["configDir"]
    assert harness._ci_owned_prompt(args.initial_prompt) == args.initial_prompt
    assert harness._ci_owned_prompt(runner.IMAGE_INTERRUPT_PROMPT) == runner.IMAGE_INTERRUPT_PROMPT
    assert harness._ci_owned_prompt(args.recovery_prompt) == args.recovery_prompt


@pytest.mark.parametrize("method", ["stream_image_text", "start_stream_image_text"])
@pytest.mark.parametrize("ci_teardown", [False, True])
def test_image_intents_carry_the_same_exact_stack_constraint_as_text(
    tmp_path: Path, method: str, ci_teardown: bool,
) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--scenario", "image-interrupt", "--run-dir", str(tmp_path)] +
                             (["--ci-teardown"] if ci_teardown else []))
    h = runner.ScenarioHarness(args, scenario="image-interrupt")
    h.stream = MagicMock()
    h.start_stream = MagicMock()
    h.image_fixtures.part = MagicMock(return_value={"mediaType": "image/png", "bytes": "fixture"})
    getattr(h, method)(text=runner.ROLLBACK_PROMPT, image_key="rollback-interrupt", name="rollback")
    assert h.image_fixtures.part.call_args.args[1] == runner.ROLLBACK_PROMPT
    caption = h.image_fixtures.part.call_args.kwargs["caption"]
    assert caption == ""


@pytest.mark.parametrize("ci_teardown", [False, True])
def test_explicit_image_rollback_target_is_in_image_not_plain_prompt(tmp_path, ci_teardown):
    runner = _load_runner()
    args = runner.parse_args(["--scenario", "image-interrupt", "--run-dir", str(tmp_path)] +
                             (["--ci-teardown"] if ci_teardown else []))
    h = runner.ScenarioHarness(args, scenario="image-interrupt")
    h.start_stream = MagicMock()
    h.image_fixtures.part = MagicMock(return_value={"mediaType": "image/png", "bytes": "fixture"})
    h.start_stream_image_text(text=runner.ROLLBACK_PROMPT, image_key="rollback-interrupt", name="rollback",
                             caption=runner.IMAGE_ROLLBACK_TARGET_CAPTION, prompt=runner.IMAGE_INTERRUPT_PROMPT)
    fixture = h.image_fixtures.part.call_args
    assert fixture.args == ("rollback-interrupt", runner.ROLLBACK_PROMPT)
    assert fixture.kwargs["caption"].startswith(runner.IMAGE_ROLLBACK_TARGET_CAPTION)
    assert "StackName" not in fixture.kwargs["caption"]
    assert h.start_stream.call_args.kwargs["prompt"] == runner.IMAGE_INTERRUPT_PROMPT
    assert "intent_parsing" not in h.start_stream.call_args.kwargs["prompt"]


def test_image_interrupt_scenario_requests_its_documented_fault_target(monkeypatch):
    runner = _load_runner()
    captured = {}

    def image_request(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("request captured")

    h = SimpleNamespace(start_stream=lambda **_: object(), start_stream_image_text=image_request)
    monkeypatch.setattr(runner, "_run_with_harness", lambda args, scenario, callback: callback(h))
    monkeypatch.setattr(runner, "_wait_for_with_intervening_ask_inputs", lambda *a, **kw: [])
    with pytest.raises(RuntimeError, match="request captured"):
        runner.run_image_interrupt(SimpleNamespace(initial_prompt="initial", event_timeout=1), "image-interrupt")
    assert captured["text"] == runner.ROLLBACK_PROMPT
    assert captured["caption"] == runner.IMAGE_ROLLBACK_TARGET_CAPTION
    assert "intent_parsing" in captured["caption"]
    assert h.failure_stage == "rollback_completion"


def test_ci_rollback_cleanup_tracks_both_stack_names(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args([
        "--scenario", "rollback-step5-cleanup", "--run-dir", str(tmp_path), "--ci-teardown",
    ])
    harness = runner.ScenarioHarness(args, scenario="rollback-step5-cleanup")

    assert len(harness.owned_stack_names) == 2
    assert harness.owned_stack_names[0].endswith("-first")
    assert harness.owned_stack_names[1].endswith("-second")


def test_iac_code_web_2c4g_evidence_requires_structured_cpu_and_memory() -> None:
    runner = _load_runner()

    assert runner.IAC_CODE_WEB_2C4G_PROMPT == "我要部署一台2核4G的ECS同时部署 iac-code Agent"
    conclusion = {
        "deployment_parameters": {"InstanceType": "ecs.c7.large"},
        "hard_constraint_checks": [
            {
                "constraint": {"property": "vcpu", "value": 2, "unit": "count"},
                "status": "satisfied",
                "actual_value": "2",
                "actual_unit": "count",
                "parameter_values": {"InstanceType": "ecs.c7.large"},
                "evidence": [
                    {
                        "type": "tool",
                        "tool_name": "aliyun_api",
                        "action": "DescribeInstanceTypes",
                        "result_path": "InstanceTypes.InstanceType.0.CpuCoreCount",
                        "actual_value": 2,
                    }
                ],
            },
            {
                "constraint": {"property": "memory", "value": 4, "unit": "GiB"},
                "status": "satisfied",
                "actual_value": "4",
                "actual_unit": "GiB",
                "parameter_values": {"InstanceType": "ecs.c7.large"},
                "evidence": [
                    {
                        "type": "tool",
                        "tool_name": "aliyun_api",
                        "action": "DescribeInstanceTypes",
                        "result_path": "InstanceTypes.InstanceType.0.MemorySize",
                        "actual_value": 4,
                    }
                ],
            },
        ],
    }
    snapshot = {
        "display": {
            "toolResults": [
                {
                    "sequence": 1,
                    "toolName": "aliyun_api",
                    "isError": False,
                    "input": {"product": "ecs", "action": "DescribeInstanceTypes"},
                    "result": {
                        "InstanceTypes": {
                            "InstanceType": [{"InstanceTypeId": "ecs.c7.large", "CpuCoreCount": 2, "MemorySize": 4}]
                        }
                    },
                },
                {
                    "sequence": 2,
                    "toolName": "complete_step",
                    "isError": False,
                    "input": {"conclusion": conclusion},
                },
            ]
        }
    }

    assert runner._has_2c4g_structured_evidence(snapshot) is True
    snapshot["display"]["toolResults"][0]["result"]["InstanceTypes"]["InstanceType"][0]["MemorySize"] = 8
    assert runner._has_2c4g_structured_evidence(snapshot) is False
    snapshot["display"]["toolResults"][0]["result"]["InstanceTypes"]["InstanceType"][0]["MemorySize"] = 4
    conclusion["hard_constraint_checks"][1]["actual_unit"] = "MiB"
    assert runner._has_2c4g_structured_evidence(snapshot) is False
    conclusion["hard_constraint_checks"][1]["actual_unit"] = "GiB"
    snapshot["display"]["toolResults"][0]["result"] = "private truncated preview"
    assert runner._has_2c4g_structured_evidence(snapshot) is False


def test_tool_results_use_sequence_and_ignore_other_display_buckets() -> None:
    runner = _load_runner()

    snapshot = {
        "display": {
            "permissions": [{"sequence": 1, "toolName": "ros_estimate_template_cost"}],
            "toolResults": [
                {"sequence": 20, "toolName": "ros_estimate_template_cost"},
                {"sequence": 10, "toolName": "ros_preview_template"},
            ],
        }
    }

    assert [item["toolName"] for item in runner._ordered_tool_results(snapshot)] == [
        "ros_preview_template",
        "ros_estimate_template_cost",
    ]


def test_all_pipeline_event_types_keeps_events_before_intervening_answers() -> None:
    runner = _load_runner()
    original = runner.StreamSummary(
        name="01-initial-2c4g",
        prompt=runner.IAC_CODE_WEB_2C4G_PROMPT,
        pipeline_event_types=["deployment_started", "input_required"],
    )
    answer = runner.StreamSummary(
        name="01-initial-2c4g-answer-ask-1",
        prompt=runner.INTERVENING_ASK_ANSWER,
        pipeline_event_types=["input_required"],
    )

    assert original.prompt == runner.IAC_CODE_WEB_2C4G_PROMPT
    assert runner._all_pipeline_event_types([original, answer]) == {"deployment_started", "input_required"}


def test_golden_solution_requires_read_and_tagged_write() -> None:
    runner = _load_runner()
    snapshot = {
        "display": {
            "toolResults": [
                {
                    "sequence": 1,
                    "toolName": "read_file",
                    "isError": False,
                    "input": {"path": "references/solutions/iac-code-web.ros.yml"},
                },
                {
                    "sequence": 2,
                    "toolName": "write_file",
                    "isError": False,
                    "input": {"path": "templates/web.yml", "content": "acs:solution:iac-code:iac-code-web"},
                },
            ]
        }
    }

    assert runner._golden_solution_evidenced(snapshot) is True
    snapshot["display"]["toolResults"][1]["input"]["content"] = "generic template"
    assert runner._golden_solution_evidenced(snapshot) is False
    snapshot["display"]["toolResults"][1] = {
        "sequence": 2,
        "toolName": "complete_step",
        "isError": False,
        "input": {"conclusion": {"template": "acs:solution:iac-code:iac-code-web"}},
    }
    assert runner._golden_solution_evidenced(snapshot) is True


def test_normal_running_recovery_prompt_ignores_continue() -> None:
    runner = _load_runner()

    assert "我刚才问了你哪些问题" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "最后一条真实用户消息原文" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "内容等于“继续”" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "请完成当前步骤" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "更早的方案选择消息" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT


def test_a2a_session_contains_user_message_reads_persisted_context(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    from iac_code.agent.message import Message
    from iac_code.services.session_storage import SessionStorage

    cwd = str(tmp_path / "workspace")
    Path(cwd).mkdir()
    run_dir = tmp_path / "run"
    context_dir = run_dir / "a2a-persistence" / "contexts"
    context_dir.mkdir(parents=True)
    (context_dir / "ctx-1.json").write_text(
        json.dumps({"context_id": "ctx-1", "cwd": cwd, "session_id": "session-1"}),
        encoding="utf-8",
    )
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    storage.save(
        cwd,
        "session-1",
        [
            Message(role="user", content="[Pipeline Handoff Context]\n..."),
            Message(role="user", content="你刚才创建了什么"),
            Message(role="assistant", content="没有实际部署任何云资源。"),
        ],
    )
    monkeypatch.setattr(runner, "SessionStorage", lambda: storage)
    harness = SimpleNamespace(run_dir=run_dir, context_id="ctx-1", cwd=cwd, notes=[])

    assert runner._a2a_session_contains_user_message(harness, "你刚才创建了什么") is True
    assert runner._a2a_session_contains_user_message(harness, "不存在的问题") is False
    assert harness.notes == []


def test_text_image_fixture_store_writes_png_and_manifest(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    # This test covers generation/storage, independently of installed CJK fonts.
    # Chinese glyph rejection and pre-rendered Chinese fixtures are tested below.
    monkeypatch.setattr(runner, "_load_text_image_font", lambda **_: runner.ImageFont.load_default())
    text = "Offline text image fixture"
    store = runner.TextImageFixtureStore(tmp_path / "image-fixtures")

    part = store.part("runtime-only", text)

    assert part["filename"] == "runtime-only.png"
    assert part["mediaType"] == "image/png"
    assert base64.b64decode(part["bytes"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert (tmp_path / "image-fixtures" / "runtime-only.png").is_file()
    manifest = json.loads((tmp_path / "image-fixtures" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["runtime-only"]["text"] == text
    assert manifest["runtime-only"]["mediaType"] == "image/png"
    assert manifest["runtime-only"]["source"] == "generated"


def test_static_text_image_fixtures_cover_fixed_image_prompts() -> None:
    runner = _load_runner()
    manifest = json.loads((runner.STATIC_TEXT_IMAGE_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert set(manifest) == set(runner.STATIC_TEXT_IMAGE_FIXTURES)
    for key, text in runner.STATIC_TEXT_IMAGE_FIXTURES.items():
        entry = manifest[key]
        fixture_path = runner.STATIC_TEXT_IMAGE_FIXTURE_ROOT / entry["filename"]
        assert entry["text"] == text
        assert entry["mediaType"] == "image/png"
        assert fixture_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_text_image_fixture_store_prefers_static_fixture(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    store = runner.TextImageFixtureStore(tmp_path / "image-fixtures")
    static_manifest = json.loads((runner.STATIC_TEXT_IMAGE_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    def fail_render(_text: str) -> bytes:
        raise AssertionError("static fixtures should avoid runtime image rendering")

    monkeypatch.setattr(runner, "_render_text_png", fail_render)

    part = store.part("initial", runner.STATIC_TEXT_IMAGE_FIXTURES["initial"])

    static_path = runner.STATIC_TEXT_IMAGE_FIXTURE_ROOT / static_manifest["initial"]["filename"]
    assert part["filename"] == static_path.name
    assert part["mediaType"] == "image/png"
    assert base64.b64decode(part["bytes"]) == static_path.read_bytes()
    manifest = json.loads((tmp_path / "image-fixtures" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["initial"]["source"] == "static"
    assert manifest["initial"]["path"] == str(static_path)


@pytest.mark.parametrize("key", ["selection", "rollback-interrupt"])
def test_owned_image_preserves_chinese_pixels_without_cjk_font(monkeypatch, tmp_path: Path, key: str) -> None:
    runner = _load_runner()
    store = runner.TextImageFixtureStore(tmp_path / "images")
    text = runner.STATIC_TEXT_IMAGE_FIXTURES[key]
    original_path = store._static_fixture_path(key, text)
    assert original_path is not None
    monkeypatch.setattr(runner, "_load_text_image_font", lambda **_: runner.ImageFont.load_default())
    monkeypatch.setattr(runner, "_render_text_png", lambda _: pytest.fail("must not rerender Chinese"))
    name = "iac-e2e-" + "f" * 32 + "-main"
    caption = "If creating a ROS Stack, StackName must be exactly:\n" + name + "\nDo not reuse any existing Stack."
    part = store.part(key, text, caption=caption)
    image_bytes = io.BytesIO(base64.b64decode(part["bytes"]))
    with runner.Image.open(original_path) as original, runner.Image.open(image_bytes) as result:
        assert result.height > original.height
        assert result.crop((0, 0, original.width, original.height)).tobytes() == original.convert("RGB").tobytes()
        footer = result.crop((0, original.height, result.width, result.height))
        assert footer.tobytes() != b"\xff" * (footer.width * footer.height * 3)
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert manifest[key]["text"] == text
    assert manifest[key]["source"] == "static-captioned"


def test_dynamic_image_caption_rejects_unsupported_non_ascii_text() -> None:
    runner = _load_runner()
    with pytest.raises(ValueError, match="ASCII"):
        runner._append_text_image_caption(b"not-read-before-validation", "中文")


def test_scenario_harness_stream_passes_image_parts(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    captured: dict[str, object] = {}

    args = SimpleNamespace(
        server_cwd=str(tmp_path),
        cwd="",
        port=0,
        host="127.0.0.1",
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=False,
        fault_at="",
        stream_timeout=1,
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path / "runs"),
        python=sys.executable,
        leave_server_running=False,
    )
    harness = runner.ScenarioHarness(args, scenario="image-initial")
    assert harness.server_env["IAC_CODE_MODEL"] == "qwen3.8-max"
    image = {"filename": "initial.png", "mediaType": "image/png", "bytes": "iVBORw0KGgo="}

    def fake_stream_message(**kwargs):
        captured.update(kwargs)
        return runner.StreamSummary(
            name=kwargs["name"],
            prompt=kwargs["prompt"],
            request_task_id=kwargs["task_id"],
            task_id="task-1",
            context_id="ctx-1",
        )

    monkeypatch.setattr(runner, "stream_message", fake_stream_message)

    harness.stream(prompt=runner.IMAGE_TEXT_PROMPT, name="01-image", context_id="", task_id="", images=[image])

    assert captured["images"] == [image]


def test_image_recovery_scenarios_are_registered() -> None:
    runner = _load_runner()

    for scenario in [
        "image-initial",
        "image-ask-waiting",
        "image-selection-waiting",
        "image-normal-handoff",
        "image-interrupt",
    ]:
        assert scenario in runner._SCENARIOS
        assert scenario in runner._REAL_CLOUD_SCENARIOS


def test_default_models_are_selected_per_scenario() -> None:
    runner = _load_runner()
    args = runner.parse_args([])

    assert runner._model_for_scenario(args, "scenario1") == "deepseek-v4-flash-0731"
    assert runner._model_for_scenario(args, "image-initial") == "qwen3.8-max"


def test_explicit_model_overrides_every_scenario() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--model", "custom-model"])

    assert runner._model_for_scenario(args, "scenario1") == "custom-model"
    assert runner._model_for_scenario(args, "image-initial") == "custom-model"


def test_scenario1_performance_backup_is_registered_and_requires_real_cloud() -> None:
    runner = _load_runner()

    assert runner._SCENARIOS["scenario1-performance-backup"] is runner.run_scenario1_performance_backup
    args = SimpleNamespace(allow_real_cloud=False, deterministic=False)
    try:
        runner._validate_scenario_execution(args, "scenario1-performance-backup")
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError("scenario1-performance-backup should require --allow-real-cloud")


def test_selection_during_backup_is_registered_and_requires_real_cloud() -> None:
    runner = _load_runner()

    scenario = runner.SELECTION_DURING_BACKUP_SCENARIO
    assert runner._SCENARIOS[scenario] is runner.run_selection_during_backup
    args = SimpleNamespace(allow_real_cloud=False, deterministic=False)
    try:
        runner._validate_scenario_execution(args, scenario)
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError(f"{scenario} should require --allow-real-cloud")


def test_redaction_step4_is_registered_requires_real_cloud_and_forces_safe_mode(tmp_path: Path) -> None:
    runner = _load_runner()

    assert runner._SCENARIOS[runner.REDACTION_STEP4_SCENARIO] is runner.run_redaction_step4
    args = SimpleNamespace(allow_real_cloud=False, deterministic=False)
    try:
        runner._validate_scenario_execution(args, runner.REDACTION_STEP4_SCENARIO)
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError("redaction-step4 should require --allow-real-cloud")

    harness_args = SimpleNamespace(
        server_cwd=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path),
        cwd="",
        host="127.0.0.1",
        port=0,
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=False,
        fault_at="",
    )
    harness = runner.ScenarioHarness(harness_args, scenario=runner.REDACTION_STEP4_SCENARIO)

    assert harness.server_env["IAC_CODE_A2A_SAFE_MODE"] == "true"


def test_step4_redaction_audit_preserves_credentials_tokens_and_only_hides_paths() -> None:
    runner = _load_runner()
    server_root = "/srv/iac-code-e2e"
    canonical = {
        "status": "waiting_input",
        "pendingInput": {
            "step": {"id": "confirm_and_select"},
            "options": [{"name": "economy"}, {"name": "balanced"}],
        },
        "steps": [
            {
                "conclusion": {
                    "deployment_parameters": {"RdsMasterUserPassword": "real-generated-value"},
                    "preview_validation": {"parameters": {"RdsMasterUserPassword": "real-generated-value"}},
                    "template_url": f"{server_root}/workspace/backend.yml",
                }
            }
        ],
        "display": {"usage": {"totalTokens": 1234}},
        "failedToolCall": {
            "path": f"{server_root}-rerun/src/iac_code/a2a/app.py",
            "result": f"File not found: {server_root}-rerun/src/iac_code/a2a/app.py",
        },
    }
    public = json.loads(json.dumps(canonical))
    public["steps"][0]["conclusion"]["template_url"] = "[PATH]"

    audit = runner._build_step4_redaction_audit(
        canonical,
        public,
        known_server_paths=(server_root,),
        safe_mode="true",
    )

    assert all(runner._step4_redaction_checks(audit).values())
    assert "real-generated-value" not in json.dumps(audit)
    assert audit["canonicalKnownServerPathOccurrences"] == 1
    assert audit["publicKnownServerPathOccurrences"] == 0

    leaked_public = json.loads(json.dumps(public))
    leaked_public["steps"][0]["conclusion"]["template_url"] = f"{server_root}/workspace/backend.yml"
    leaked_audit = runner._build_step4_redaction_audit(
        canonical,
        leaked_public,
        known_server_paths=(server_root,),
        safe_mode="true",
    )

    assert leaked_audit["publicKnownServerPathOccurrences"] == 1
    assert runner._step4_redaction_checks(leaked_audit)["safe mode hides known server paths from public state"] is False

    public["steps"][0]["conclusion"]["deployment_parameters"]["RdsMasterUserPassword"] = "***"
    broken_audit = runner._build_step4_redaction_audit(
        canonical,
        public,
        known_server_paths=(server_root,),
        safe_mode="true",
    )
    broken_checks = runner._step4_redaction_checks(broken_audit)

    assert broken_checks["public functional parameters contain no redaction placeholders"] is False
    assert broken_checks["public credential fields match canonical values"] is False

    broken_canonical = json.loads(json.dumps(canonical))
    broken_canonical["steps"][0]["conclusion"]["deployment_parameters"]["RdsMasterUserPassword"] = "***"
    broken_canonical["display"]["usage"]["totalTokens"] = "***"
    canonical_audit = runner._build_step4_redaction_audit(
        broken_canonical,
        public,
        known_server_paths=(server_root,),
        safe_mode="true",
    )
    canonical_checks = runner._step4_redaction_checks(canonical_audit)

    assert canonical_checks["canonical functional parameters contain no redaction placeholders"] is False
    assert canonical_checks["canonical token counters are numeric when present"] is False


def test_redaction_counters_exclude_cloud_parameters_and_schemas_but_validate_real_usage():
    runner = _load_runner()
    snapshot = {"display": {"usage": {"totalTokens": 123}, "toolResults": [
        {"toolName": "aliyun_api", "input": {"params": {"MaxTokens": "128"}},
         "result": {"totalTokens": "field schema", "usage": {"inputTokens": "fake tool content"}}},
    ]}}
    old = {path: value for path, key, value in runner._iter_scalar_values(snapshot)
           if key.casefold().endswith("tokens")}
    assert any(not runner._is_number(value) for value in old.values())
    assert runner._token_counter_values(snapshot) == {"display.usage.totalTokens": 123}
    audit = runner._build_step4_redaction_audit(snapshot, snapshot, known_server_paths=(), safe_mode="true")
    checks = runner._step4_redaction_checks(audit)
    assert checks["canonical token counters are numeric when present"] is True
    assert checks["public token counters remain numeric and unchanged when present"] is True
    snapshot["display"]["usage"]["totalTokens"] = "redacted"
    broken = runner._build_step4_redaction_audit(snapshot, snapshot, known_server_paths=(), safe_mode="true")
    assert runner._step4_redaction_checks(broken)["canonical token counters are numeric when present"] is False


@pytest.mark.parametrize("bad_public", ["redacted", True, 124, None, "missing"])
def test_redaction_actual_usage_events_reject_changed_missing_and_non_numeric_counters(bad_public):
    runner = _load_runner()
    event = {"eventType": "usage", "eventId": "usage-1", "data": {"totalTokens": 123}}
    canonical = runner._usage_event_token_counters(event)
    public = {} if bad_public == "missing" else runner._usage_event_token_counters({
        **event, "data": {"totalTokens": bad_public},
    })
    audit = runner._build_step4_redaction_audit(
        {}, {}, known_server_paths=(), safe_mode="true",
        canonical_token_events=canonical, public_token_events=public,
    )
    checks = runner._step4_redaction_checks(audit)
    assert checks["canonical token counters are numeric when present"] is True
    assert checks["public token counters remain numeric and unchanged when present"] is False


def test_redaction_usage_reads_native_journal_and_correlates_public_batch_not_tool_text(tmp_path, monkeypatch):
    runner = _load_runner()
    from iac_code.a2a.pipeline_events import _provider_usage_data
    from iac_code.a2a.pipeline_journal import A2APipelineJournal

    payload = _provider_usage_data(SimpleNamespace(
        input_tokens=10, output_tokens=3, total_tokens=13, cache_read_input_tokens=2,
        provider="fake", model="fake",
    ))
    event = {"eventType": "usage", "eventId": "usage-1", "data": payload}
    context = {"eventType": "context_usage", "eventId": "context-1", "data": {"totalTokens": 100}}
    journal = A2APipelineJournal(tmp_path)
    journal.append_many([event, context])
    monkeypatch.setattr(runner, "_pipeline_session_identity", lambda _h: (str(tmp_path), "session"))
    monkeypatch.setattr(runner, "existing_a2a_pipeline_dir_for_session", lambda **_kwargs: tmp_path)
    canonical = runner._load_canonical_usage_token_counters(SimpleNamespace())
    public = runner._usage_event_token_counters({"metadata": {"iac_code": {"pipelineBatch": {"events": [
        {**event, "data": {**payload, "totalTokens": 13.0}}, context,
        {"eventType": "tool_result", "data": {"eventType": "usage", "eventId": "fake",
                                                    "data": {"totalTokens": "private"}}},
    ]}}}})
    assert canonical == public and len(public) == 5
    audit = runner._build_step4_redaction_audit(
        {}, {}, known_server_paths=(), safe_mode="true",
        canonical_token_events=canonical, public_token_events=public,
    )
    assert runner._step4_redaction_checks(audit)["public token counters remain numeric and unchanged when present"]
    assert "private" not in json.dumps(audit)
    runner._merge_usage_token_counters(public, {"events.usage-1.totalTokens": 99})
    inconsistent = runner._build_step4_redaction_audit(
        {}, {}, known_server_paths=(), safe_mode="true",
        canonical_token_events=canonical, public_token_events=public,
    )
    assert not runner._step4_redaction_checks(inconsistent)[
        "public token counters remain numeric and unchanged when present"]


@pytest.mark.parametrize("option_count", [2, 3, 1])
def test_redaction_step4_stops_before_selection_and_writes_only_audit(
    monkeypatch, tmp_path: Path, option_count: int,
) -> None:
    runner = _load_runner()
    prompts: list[str] = []
    server_root = str(tmp_path / "server")
    canonical = {
        "status": "waiting_input",
        "pendingInput": {
            "step": {"id": "confirm_and_select"},
            "options": [{"name": f"candidate-{index}"} for index in range(option_count)],
        },
        "steps": [
            {
                "conclusion": {
                    "deployment_parameters": {"RdsMasterUserPassword": "real-generated-value"},
                    "template_url": f"{server_root}/backend.yml",
                }
            }
        ],
        "display": {"usage": {"totalTokens": 1234}},
    }
    public = {"snapshot": json.loads(json.dumps(canonical))}
    public["snapshot"]["steps"][0]["conclusion"]["template_url"] = "[PATH]"

    class FakeHarness:
        def __init__(self) -> None:
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.cwd = server_root
            self.server_cwd = server_root
            self.server_url = "http://127.0.0.1:1"
            self.run_dir = tmp_path
            self.server_env = {"IAC_CODE_A2A_SAFE_MODE": "true"}
            self.checks = {}
            self.snapshots = {}
            self.notes = []

        def stream(self, *, prompt: str, name: str, context_id: str, task_id: str):
            prompts.append(prompt)
            assert name == "01-redaction-step4"
            assert context_id == ""
            assert task_id == ""
            return runner.StreamSummary(
                name=name,
                prompt=prompt,
                task_id=self.pipeline_task_id,
                context_id=self.context_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"],
                pipeline_event_types=["input_required"],
                last_input_required_step_id="confirm_and_select",
            )

    harness = FakeHarness()

    def fake_run_with_harness(_args, _scenario, callback):
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_load_canonical_pipeline_snapshot", lambda _h: canonical)
    monkeypatch.setattr(runner, "_load_canonical_usage_token_counters", lambda _h: {})
    monkeypatch.setattr(runner, "_fetch_pipeline_state_for_redaction_audit", lambda _h: public)
    args = SimpleNamespace(redaction_step4_prompt=runner.REDACTION_STEP4_PROMPT)

    assert runner.run_redaction_step4(args, runner.REDACTION_STEP4_SCENARIO) == (0 if option_count == 2 else 1)
    assert harness.checks["step4 exposes two candidate options"] is (option_count == 2)
    assert prompts == [runner.REDACTION_STEP4_PROMPT]
    audit = json.loads((tmp_path / "redaction-audit.json").read_text(encoding="utf-8"))
    assert "real-generated-value" not in json.dumps(audit)
    assert any("no selection input was sent" in note for note in harness.notes)


@pytest.mark.parametrize(("notes", "requested"), [
    ("请准备 2 个方案", 2), ("提供两个方案", 2), ("10个候选", 10),
    ("12个方案", 0), ("先2个方案或3个方案", 0), ("双方案", 0),
])
def test_redaction_candidate_diagnostics_export_only_counts_without_changing_acceptance(notes, requested):
    runner = _load_runner()
    secret = "FAKE_PRIVATE_CREDENTIAL"
    snapshot = {"steps": [
        {"id": "intent_parsing", "conclusion": {"additional_notes": notes + " " + secret}},
        {"id": "architecture_planning", "conclusion": {"candidates": [{"password": secret}] * 3}},
    ]}
    original = json.dumps(snapshot)
    h = SimpleNamespace(diagnostics={}, checks={"step4 exposes two candidate options": False})
    runner._record_redaction_candidate_diagnostics(h, snapshot)
    assert h.diagnostics == {"redaction_intent_requested_candidate_count": requested,
                             "redaction_intent_structured_candidate_count": 0,
                             "redaction_architecture_candidate_count": 3}
    assert h.checks == {"step4 exposes two candidate options": False}
    assert secret not in json.dumps(h.diagnostics) and notes not in json.dumps(h.diagnostics)
    assert json.dumps(snapshot) == original


@pytest.mark.parametrize('value,expected', [(3, 3), (None, 0), (True, 0), ('private', 0), (-1, 0)])
def test_redaction_structured_candidate_count_diagnostic_is_numeric_only(value, expected):
    runner = _load_runner()
    h = SimpleNamespace(diagnostics={})
    runner._record_redaction_candidate_diagnostics(h, {'steps': [{
        'id': 'intent_parsing', 'conclusion': {'requested_candidate_count': value},
    }]})
    assert h.diagnostics['redaction_intent_structured_candidate_count'] == expected
    assert 'private' not in json.dumps(h.diagnostics)


def test_answer_intervening_ask_inputs_reaches_selection(tmp_path: Path, monkeypatch) -> None:
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

    monkeypatch.setattr(runner, "_answer_pending_legacy_question", lambda *_args: runner.INTERVENING_ASK_ANSWER)
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


def test_all_evidence_includes_workspace_text_files(tmp_path: Path) -> None:
    runner = _load_runner()
    workspace = tmp_path / "workspace"
    template_dir = workspace / "templates"
    template_dir.mkdir(parents=True)
    (template_dir / "main.yml").write_text(
        "Resources:\n  VSwitch:\n    Type: ALIYUN::ECS::VSwitch\n",
        encoding="utf-8",
    )
    (workspace / "ignored.bin").write_bytes(b"ALIYUN::ECS::VSwitch")
    harness = SimpleNamespace(
        summaries={},
        snapshots={},
        workspace_dir=workspace,
    )

    evidence = runner._all_evidence(harness)

    assert "templates/main.yml" in evidence
    assert "ALIYUN::ECS::VSwitch" in evidence
    assert "ignored.bin" not in evidence


def test_finish_pipeline_after_possible_input_uses_custom_prompt_for_pending_input(tmp_path) -> None:
    runner = _load_runner()
    prompts: list[str] = []
    initial = runner.StreamSummary(
        name="resume",
        prompt="继续",
        status_states=["TASK_STATE_INPUT_REQUIRED"],
        pipeline_event_types=["input_required"],
        last_input_required_step_id="intent_parsing",
    )

    def stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "continue-after-input-1"
        return runner.StreamSummary(
            name=name,
            prompt=prompt,
            status_states=["TASK_STATE_COMPLETED"],
            pipeline_event_types=["pipeline_completed"],
        )

    harness = SimpleNamespace(run_dir=tmp_path, stream=stream)
    args = SimpleNamespace(selection_prompt="选择第一个方案")

    runner._finish_pipeline_after_possible_input(
        harness,
        initial,
        args,
        input_prompt=runner.ROLLBACK_PROMPT,
    )

    assert prompts == [runner.ROLLBACK_PROMPT]


@pytest.mark.parametrize(('total_budget', 'idle_budget', 'reaches_target'), [(2, None, False), (10, 2, True)])
def test_step4_preparation_uses_stream_budget_while_native_milestones_advance(
    monkeypatch, total_budget, idle_budget, reaches_target,
):
    runner = _load_runner()
    clock = [0.0]
    stream = SimpleNamespace(events=[], summary=SimpleNamespace())
    def wait_for(predicate, **_kwargs):
        clock[0] += 0.5
        step = int(clock[0] // 1.5)
        event = {'eventType': 'step_completed', 'sequence': step + 1, 'step': {'id': f'preparation-{step}'}}
        if step >= 3:
            event = {'eventType': 'step_started', 'sequence': 10, 'step': {'id': 'confirm_and_select'}}
        stream.events.append(event)
        if predicate(event, stream.summary):
            return object()
        raise TimeoutError('still preparing')
    stream.wait_for = wait_for
    monkeypatch.setattr(runner, '_extract_pipeline_envelopes', lambda event: [event])
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(runner.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    kwargs = dict(description='step_started(confirm_and_select)', timeout=total_budget,
                  name_prefix='initial', progress_idle_timeout=idle_budget)
    if reaches_target:
        assert runner._wait_for_with_intervening_ask_inputs(
            SimpleNamespace(), [stream], runner._step_started('confirm_and_select'), **kwargs) == [stream]
        assert clock[0] > 2 and clock[0] < 10
    else:
        with pytest.raises(TimeoutError):
            runner._wait_for_with_intervening_ask_inputs(
                SimpleNamespace(), [stream], runner._step_started('confirm_and_select'), **kwargs)


def test_step4_idle_budget_rejects_heartbeats_and_repeated_milestones(monkeypatch):
    runner = _load_runner()
    clock = [0.0]
    stream = SimpleNamespace(events=[], summary=SimpleNamespace())
    def wait_for(*_args, **_kwargs):
        clock[0] += 0.5
        stream.events.extend([{'eventType': 'step_started', 'sequence': 1, 'step': {'id': 'evaluate_candidates'}},
                              {'eventType': 'working', 'sequence': len(stream.events) + 2}])
        raise TimeoutError('no actual progress')
    stream.wait_for = wait_for
    monkeypatch.setattr(runner, '_extract_pipeline_envelopes', lambda event: [event])
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(runner.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(TimeoutError, match='no pipeline milestone progress'):
        runner._wait_for_with_intervening_ask_inputs(
            SimpleNamespace(), [stream], lambda *_: False, description='Step 4', timeout=10,
            name_prefix='initial', progress_idle_timeout=2)
    assert clock[0] < 4


def test_wait_for_with_intervening_ask_inputs_uses_custom_answer_prompt(monkeypatch) -> None:
    runner = _load_runner()
    prompts: list[str] = []
    initial_summary = runner.StreamSummary(
        name="initial",
        prompt="",
        pipeline_event_types=["input_required"],
    )

    class InitialStream:
        name = "initial"
        events = [_input_required_event("ask_user_question")]

        def wait_for(self, *_args, **_kwargs):
            raise RuntimeError("initial ended")

    class AnswerStream:
        name = "answer"
        events: list[dict] = []

        def wait_for(self, predicate, *, description: str, timeout: float):
            event = {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "input_required",
                                    "step": {"id": "confirm_and_select"},
                                    "data": {},
                                }
                            }
                        }
                    }
                }
            }
            if predicate(event, initial_summary):
                return runner.EventMatch(description=description, event=event, summary=initial_summary)
            raise TimeoutError(description)

    def start_stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "rollback-answer-ask-1"
        return AnswerStream()

    harness = SimpleNamespace(notes=[], start_stream=start_stream)

    InitialStream.summary = initial_summary
    monkeypatch.setattr(runner, "_answer_pending_legacy_question", lambda _h, _s, goal: goal)
    streams = runner._wait_for_with_intervening_ask_inputs(
        harness,
        [InitialStream()],
        runner._input_required_step("confirm_and_select"),
        description="selection",
        timeout=1,
        name_prefix="rollback",
        answer_prompt=runner.ROLLBACK_PROMPT,
    )

    assert prompts == [runner.ROLLBACK_PROMPT]
    assert len(streams) == 2


def test_wait_for_with_intervening_inputs_answers_allowed_step_with_custom_prompt() -> None:
    runner = _load_runner()
    prompts: list[str] = []
    initial_summary = runner.StreamSummary(name="initial", prompt="", pipeline_event_types=["input_required"])

    class InitialStream:
        name = "initial"
        events = [_input_required_event(step_id="intent_parsing")]

        def wait_for(self, *_args, **_kwargs):
            raise RuntimeError("initial ended")

    class AnswerStream:
        name = "answer"
        events: list[dict] = []

        def wait_for(self, predicate, *, description: str, timeout: float):
            event = {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "step_started",
                                    "step": {"id": "confirm_and_select"},
                                    "data": {},
                                }
                            }
                        }
                    }
                }
            }
            if predicate(event, initial_summary):
                return runner.EventMatch(description=description, event=event, summary=initial_summary)
            raise TimeoutError(description)

    def start_stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "rollback-answer-intent_parsing-1"
        return AnswerStream()

    harness = SimpleNamespace(notes=[], start_stream=start_stream)

    streams = runner._wait_for_with_intervening_ask_inputs(
        harness,
        [InitialStream()],
        runner._step_started("confirm_and_select"),
        description="confirm step",
        timeout=1,
        name_prefix="rollback",
        answer_prompt=runner.ROLLBACK_PROMPT,
        answer_input_steps={"intent_parsing"},
    )

    assert prompts == [runner.ROLLBACK_PROMPT]
    assert len(streams) == 2


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

        def _ci_owned_prompt(self, prompt):
            return prompt + "\nStackName=iac-e2e-123456789abc-main"

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
    assert "StackName=iac-e2e-123456789abc-main" in fake_harnesses[0].summaries["01-initial-fault"].prompt


def test_fault_after_snapshot_defaults_crash_point(tmp_path: Path) -> None:
    runner = _load_runner()
    args = SimpleNamespace(
        server_cwd=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path),
        cwd="",
        host="127.0.0.1",
        port=0,
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=True,
        fault_at="",
    )

    harness = runner.ScenarioHarness(args, scenario="fault-after-snapshot")

    assert harness.server_env["IAC_CODE_TEST_CRASH_AT"] == runner.FAULT_AFTER_SNAPSHOT_POINT


def test_scenario1_performance_backup_configures_server_env(tmp_path: Path) -> None:
    runner = _load_runner()
    args = SimpleNamespace(
        server_cwd=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path),
        cwd="",
        host="127.0.0.1",
        port=0,
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=False,
        fault_at="",
    )

    harness = runner.ScenarioHarness(args, scenario="scenario1-performance-backup")

    assert harness.server_env["IAC_CODE_MODEL"] == "deepseek-v4-flash-0731"
    assert harness.server_env["IAC_CODE_A2A_EXTREME_PERFORMANCE"] == "true"
    assert harness.server_env["IAC_CODE_CONFIG_BACKUP_DIR"] == str((tmp_path / "run" / "session-backup").resolve())
    assert harness.backup_root == (tmp_path / "run" / "session-backup").resolve()


def test_selection_during_backup_configures_e2e_only_delay(tmp_path: Path) -> None:
    runner = _load_runner()
    args = SimpleNamespace(
        server_cwd=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path),
        cwd="",
        host="127.0.0.1",
        port=0,
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=False,
        fault_at="",
    )

    harness = runner.ScenarioHarness(args, scenario=runner.SELECTION_DURING_BACKUP_SCENARIO)

    assert harness.server_env["IAC_CODE_A2A_EXTREME_PERFORMANCE"] == "true"
    assert harness.server_env["IAC_CODE_E2E_BACKUP_DELAY_SECONDS"] == "10.0"
    assert harness.server_env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] == str(
        (tmp_path / "run" / "selection-backup-delay").resolve()
    )
    assert str(runner.BACKUP_DELAY_FIXTURE_ROOT.resolve()) == harness.server_env["PYTHONPATH"].split(os.pathsep)[0]
    arm = json.loads(
        runner._backup_delay_marker_path(tmp_path / "run" / "selection-backup-delay", "arm").read_text(encoding="utf-8")
    )
    assert arm["scenario"] == runner.SELECTION_DURING_BACKUP_SCENARIO
    assert arm["delaySeconds"] == 10.0


def test_selection_during_backup_allows_real_step1_planning_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    observed: dict[str, object] = {}

    class StopAfterTimeoutProbeError(Exception):
        pass

    class Harness:
        def start_stream(self, **_kwargs):
            return object()

    def wait_for_backup(_h, _control, _stream, *, timeout):
        observed["timeout"] = timeout
        raise StopAfterTimeoutProbeError

    monkeypatch.setattr(runner, "_run_with_harness", lambda _args, _scenario, callback: callback(Harness()))
    monkeypatch.setattr(runner, "_backup_delay_control_path", lambda _h: tmp_path)
    monkeypatch.setattr(runner, "_wait_for_backup_start_with_intervening_asks", wait_for_backup)
    args = SimpleNamespace(initial_prompt="test", event_timeout=240.0, stream_timeout=1800.0)

    with pytest.raises(StopAfterTimeoutProbeError):
        runner.run_selection_during_backup(args, runner.SELECTION_DURING_BACKUP_SCENARIO)

    assert observed["timeout"] == 600.0


def test_backup_delay_sitecustomize_delays_armed_input_required_backup(tmp_path: Path) -> None:
    runner = _load_runner()
    control = tmp_path / "backup-delay"
    runner._write_json(runner._backup_delay_marker_path(control, "arm"), {"armed": True})
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(runner.BACKUP_DELAY_FIXTURE_ROOT.resolve()), env.get("PYTHONPATH", "")) if value
    )
    env["IAC_CODE_E2E_BACKUP_DELAY_SECONDS"] = "0.05"
    env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] = str(control)
    script = "\n".join(
        [
            "from iac_code.services.session_backup import BackupReason, SessionBackupService",
            "service = SessionBackupService()",
            "service.backup_session('', 'session-1', reason=BackupReason.INPUT_REQUIRED, critical=False)",
        ]
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    finished = runner._wait_for_backup_delay_marker(control, "finished", timeout=1)
    assert finished["elapsedSeconds"] >= 0.05
    assert finished["succeeded"] is True


def test_backup_delay_wait_answers_step1_question_before_marker(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    control = tmp_path / "backup-delay"
    initial = SimpleNamespace(
        name="initial", done=True,
        events=[{"eventType": "input_required", "data": {"kind": "ask_user_question"}}],
        summary=SimpleNamespace(name="initial"),
    )
    answer = SimpleNamespace(name="answer", done=False, events=[])

    class Harness:
        notes: list[str] = []
        current_goal = '已有 VPC 创建 VSwitch'

        def start_stream(self, *, prompt: str, name: str):
            assert prompt == 'grounded question answer'
            assert name == "01-initial-answer-ask-1"
            runner._write_json(runner._backup_delay_marker_path(control, "started"), {"delaySeconds": 10.0})
            return answer

    calls = []
    monkeypatch.setattr(runner, '_answer_pending_legacy_question',
                        lambda h, s, goal: calls.append((s.name, goal)) or 'grounded question answer')
    marker, streams = runner._wait_for_backup_start_with_intervening_asks(
        Harness(), control, initial, timeout=1.0
    )

    assert marker["delaySeconds"] == 10.0
    assert streams == [initial, answer]
    assert calls == [('initial', Harness.current_goal)]


def test_scenario1_performance_backup_omits_selection_task_id_and_checks_backup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    harnesses = []

    class FakeHarness:
        def __init__(self, args) -> None:
            self.args = args
            self.run_dir = tmp_path
            self.workspace_dir = tmp_path / "workspace"
            self.workspace_dir.mkdir()
            self.server_env = {
                "IAC_CODE_A2A_EXTREME_PERFORMANCE": "true",
                "IAC_CODE_CONFIG_BACKUP_DIR": str(tmp_path / "backup"),
            }
            self.context_id = ""
            self.pipeline_task_id = ""
            self.checks = {}
            self.notes = []
            self.summaries = {}
            self.snapshots = {}
            self.stream_request_task_ids = {}
            self.kill9_count = 0
            self.start_server_count = 0

        def stream(
            self,
            *,
            prompt: str,
            name: str,
            context_id: str | None = None,
            task_id: str | None = None,
            **_kwargs,
        ):
            if context_id == "":
                self.context_id = "ctx-1"
            if not self.context_id:
                self.context_id = "ctx-1"
            if not self.pipeline_task_id:
                self.pipeline_task_id = "task-1"
            request_task_id = self.pipeline_task_id if task_id is None else task_id
            self.stream_request_task_ids[name] = request_task_id
            if name == "01-initial":
                summary = runner.StreamSummary(
                    name=name,
                    prompt=prompt,
                    request_task_id=request_task_id,
                    context_id=self.context_id,
                    task_id=self.pipeline_task_id,
                    status_states=["TASK_STATE_INPUT_REQUIRED"],
                    pipeline_event_types=["input_required"],
                    last_input_required_step_id="confirm_and_select",
                )
            elif name == "02-select-candidate":
                restored_dir = tmp_path / "primary-session"
                restored_dir.mkdir(exist_ok=True)
                (restored_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
                summary = runner.StreamSummary(
                    name=name,
                    prompt=prompt,
                    request_task_id=request_task_id,
                    context_id=self.context_id,
                    task_id=self.pipeline_task_id,
                    status_states=["TASK_STATE_COMPLETED"],
                    pipeline_event_types=["input_received", "step_completed", "pipeline_completed"],
                    normal_handoff_ready=True,
                    text="created ALIYUN::ECS::VSwitch",
                )
            else:
                summary = runner.StreamSummary(
                    name=name,
                    prompt=prompt,
                    request_task_id=request_task_id,
                    context_id=self.context_id,
                    task_id=f"{name}-task",
                    status_states=["TASK_STATE_COMPLETED"],
                    text="created ALIYUN::ECS::VSwitch",
                )
            self.summaries[name] = summary
            return summary

        def fetch_state(self, name: str):
            return {
                "snapshot": {
                    "contextId": self.context_id,
                    "taskId": self.pipeline_task_id,
                    "status": "completed",
                    "normalHandoff": {"action": "switch_to_normal", "targetMode": "normal"},
                }
            }

        def kill9_and_restart(self) -> None:
            pass

        def kill9(self) -> None:
            self.kill9_count += 1

        def start_server(self) -> None:
            self.start_server_count += 1

    def fake_run_with_harness(args, _scenario, callback):
        harness = FakeHarness(args)
        harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(
        runner,
        "_waiting_input_backup_snapshots",
        lambda _h: {"task": {"state": "input-required"}, "context": {"active_task_id": None}},
    )
    monkeypatch.setattr(
        runner,
        "_remove_primary_session_for_backup_restore",
        lambda _h: {
            "primarySessionDir": str(tmp_path / "primary-session"),
            "primarySessionFile": str(tmp_path / "primary-session" / "session.jsonl"),
            "backupSessionDir": str(tmp_path / "backup-session"),
        },
    )
    (tmp_path / "backup-session").mkdir()
    monkeypatch.setattr(runner, "_a2a_session_contains_user_message", lambda _h, _text: True)
    monkeypatch.setattr(runner, "_all_evidence", lambda _h: "ALIYUN::ECS::VSwitch")
    monkeypatch.setattr(runner, "_run_dir_has_cleanup_events", lambda _run_dir: False)
    monkeypatch.setattr(runner, "_session_has_cleanup_prompt", lambda _h: False)
    monkeypatch.setattr(runner, "_cleanup_ledger_has_required_resources", lambda _h: False)
    args = SimpleNamespace(
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
        recovery_prompt=runner.DEFAULT_RECOVERY_PROMPT,
    )

    assert runner.run_scenario1_performance_backup(args, "scenario1-performance-backup") == 0
    assert harnesses[0].stream_request_task_ids["02-select-candidate"] == ""
    assert harnesses[0].checks["selection omitted taskId"] is True
    assert harnesses[0].checks["selection hydrated recovered taskId"] is True
    assert harnesses[0].checks["step4 backup context has no active task"] is True
    assert harnesses[0].checks["primary session stayed absent after restart"] is True
    assert harnesses[0].checks["selection restored primary session from backup"] is True
    assert harnesses[0].kill9_count == 1
    assert harnesses[0].start_server_count == 1


def test_remove_primary_session_for_backup_restore_keeps_backup(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    from iac_code.agent.message import Message
    from iac_code.services.session_storage import SessionStorage

    config_dir = tmp_path / "config"
    backup_root = tmp_path / "backup"
    run_dir = tmp_path / "run"
    workspace = tmp_path / "workspace"
    context_id = "ctx-1"
    session_id = "session-1"
    workspace.mkdir()
    context_dir = run_dir / "a2a-persistence" / "contexts"
    context_dir.mkdir(parents=True)
    (context_dir / f"{context_id}.json").write_text(
        json.dumps({"context_id": context_id, "session_id": session_id, "cwd": str(workspace)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    primary_storage = SessionStorage(projects_dir=config_dir / "projects")
    backup_storage = SessionStorage(projects_dir=backup_root / "projects")
    primary_storage.save(str(workspace), session_id, [Message(role="user", content="primary")])
    backup_storage.save(str(workspace), session_id, [Message(role="user", content="backup")])
    primary_session_dir = primary_storage.v2_session_dir(str(workspace), session_id)
    backup_session_dir = backup_storage.v2_session_dir(str(workspace), session_id)
    assert primary_session_dir is not None
    assert backup_session_dir is not None

    harness = SimpleNamespace(
        backup_root=backup_root,
        context_id=context_id,
        cwd=str(workspace),
        run_dir=run_dir,
    )
    evidence = runner._remove_primary_session_for_backup_restore(harness)

    assert evidence["primaryRemovedBeforeRestart"] is True
    assert evidence["backupPresentAfterRemoval"] is True
    assert not primary_session_dir.exists()
    assert backup_session_dir.is_dir()
    assert (run_dir / "step4.backup-only-restore.json").is_file()


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


def test_rollback_step5_cleanup_scenarios_are_registered_and_require_real_cloud() -> None:
    runner = _load_runner()

    assert runner._SCENARIOS["rollback-step5-cleanup"] is runner.run_rollback_step5_cleanup
    assert runner._SCENARIOS["rollback-step5-cleanup-recovery"] is runner.run_rollback_step5_cleanup_recovery

    for scenario in ("rollback-step5-cleanup", "rollback-step5-cleanup-recovery"):
        args = SimpleNamespace(allow_real_cloud=False, deterministic=False)
        try:
            runner._validate_scenario_execution(args, scenario)
        except SystemExit as exc:
            assert "--allow-real-cloud" in str(exc)
        else:
            raise AssertionError(f"{scenario} should require --allow-real-cloud")


def test_stack_cleanup_snapshot_helpers_distinguish_deleted_and_retained_stacks() -> None:
    runner = _load_runner()
    snapshot = {
        "snapshot": {
            "cleanup": {
                "resources": [
                    {
                        "provider": "ros",
                        "resourceType": "stack",
                        "resourceId": "stack-1",
                        "regionId": "cn-hangzhou",
                        "cleanupStatus": "completed",
                        "stackStatus": "DELETE_COMPLETE",
                    }
                ]
            },
            "stacks": {
                "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                "byId": {
                    "stack-1": {"stackId": "stack-1", "current": False, "cleared": True},
                    "stack-2": {"stackId": "stack-2", "current": True},
                    "stack-3": {"stackId": "stack-3", "isSuccess": False, "stackStatus": "CREATE_FAILED"},
                },
            },
        }
    }

    cleanup_resource = runner._cleanup_resource_for_stack(snapshot, "stack-1")
    assert cleanup_resource["cleanupStatus"] == "completed"
    assert runner._cleanup_resource_completed(cleanup_resource) is True
    assert runner._cleanup_resource_completed({"cleanupStatus": "completed"}) is False
    assert runner._snapshot_current_stack_id(snapshot, exclude={"stack-1"}) == "stack-2"
    assert runner._snapshot_current_stack_id(snapshot, exclude={"stack-2"}) is None
    assert runner._ros_stack_deleted({"status": "DELETE_COMPLETE"}) is True
    assert runner._ros_stack_deleted({"not_found": True}) is True
    assert runner._ros_stack_retained({"status": "CREATE_COMPLETE"}) is True
    assert runner._ros_stack_retained({"status": "DELETE_COMPLETE"}) is False
    assert runner._ros_stack_retained({"status": "DELETE_ROLLBACK_COMPLETE"}) is False


def _stack_current_changed_event(
    *,
    action: str,
    stack_id: str,
    status: str,
    is_success: bool,
    stack_name: str = "",
    cleared: bool = False,
) -> dict:
    data = {
        "provider": "ros",
        "action": action,
        "stackId": stack_id,
        "stackStatus": status,
        "isSuccess": is_success,
        "cleared": cleared,
    }
    if stack_name:
        data["stackName"] = stack_name
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "stack_current_changed",
                            "data": data,
                        }
                    }
                }
            }
        }
    }


def _ros_deploy_tool_result_event(
    *,
    stack_id: str,
    stack_name: str = "",
    status: str = "CREATE_COMPLETE",
    is_success: bool = True,
    is_error: bool = False,
) -> dict:
    result = {
        "stack_id": stack_id,
        "status": status,
        "is_success": is_success,
    }
    if stack_name:
        result["stack_name"] = stack_name
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "tool_result",
                            "data": {
                                "toolName": "ros_deploy",
                                "isError": is_error,
                                "result": json.dumps(result),
                            },
                        }
                    }
                }
            }
        }
    }


def test_wait_for_created_stack_uses_successful_stack_event() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="02-create-first-stack", prompt="deploy")
    events = [
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="failed-stack",
            status="CREATE_FAILED",
            is_success=False,
        ),
        _stack_current_changed_event(
            action="DeleteStack",
            stack_id="failed-stack",
            status="DELETE_COMPLETE",
            is_success=True,
            cleared=True,
        ),
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="created-stack",
            status="CREATE_COMPLETE",
            is_success=True,
        ),
    ]

    class FakeStream:
        name = "02-create-first-stack"

        def wait_for(self, predicate, *, description: str, timeout: float):
            for event in events:
                if predicate(event, summary):
                    return runner.EventMatch(description=description, event=event, summary=summary)
            raise TimeoutError(description)

    assert runner._wait_for_created_stack(FakeStream(), exclude=set(), timeout=1) == "created-stack"


def test_wait_for_created_stack_accepts_successful_continue_create_stack() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="02-create-first-stack", prompt="deploy")
    events = [
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="created-stack",
            status="CREATE_FAILED",
            is_success=False,
        ),
        _stack_current_changed_event(
            action="ContinueCreateStack",
            stack_id="created-stack",
            status="CREATE_COMPLETE",
            is_success=True,
        ),
    ]

    class FakeStream:
        name = "02-create-first-stack"

        def wait_for(self, predicate, *, description: str, timeout: float):
            for event in events:
                if predicate(event, summary):
                    return runner.EventMatch(description=description, event=event, summary=summary)
            raise TimeoutError(description)

    assert runner._wait_for_created_stack(FakeStream(), exclude=set(), timeout=1) == "created-stack"


def test_wait_for_created_stack_accepts_successful_ros_deploy_tool_result() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="02-create-first-stack", prompt="deploy")
    events = [
        _ros_deploy_tool_result_event(stack_id="failed-stack", is_success=False),
        _ros_deploy_tool_result_event(stack_id="created-stack"),
    ]

    class FakeStream:
        name = "02-create-first-stack"

        def wait_for(self, predicate, *, description: str, timeout: float):
            for event in events:
                if predicate(event, summary):
                    return runner.EventMatch(description=description, event=event, summary=summary)
            raise TimeoutError(description)

    assert runner._wait_for_created_stack(FakeStream(), exclude=set(), timeout=1) == "created-stack"


def test_wait_for_created_stack_ignores_unexpected_stack_name() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="02-create-first-stack", prompt="deploy")
    events = [
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="wrong-stack",
            stack_name="wrong-name",
            status="CREATE_COMPLETE",
            is_success=True,
        ),
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="created-stack",
            stack_name="expected-name",
            status="CREATE_COMPLETE",
            is_success=True,
        ),
    ]

    class FakeStream:
        name = "02-create-first-stack"

        def wait_for(self, predicate, *, description: str, timeout: float):
            for event in events:
                if predicate(event, summary):
                    return runner.EventMatch(description=description, event=event, summary=summary)
            raise TimeoutError(description)

    assert (
        runner._wait_for_created_stack(
            FakeStream(),
            exclude=set(),
            timeout=1,
            expected_stack_name="expected-name",
        )
        == "created-stack"
    )


def test_created_stack_id_from_stream_uses_only_that_stream_successes() -> None:
    runner = _load_runner()

    stream = SimpleNamespace(
        events=[
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="failed-stack",
                status="CREATE_FAILED",
                is_success=False,
            ),
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="rollback-stack",
                status="CREATE_COMPLETE",
                is_success=True,
            ),
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="second-stack",
                status="CREATE_COMPLETE",
                is_success=True,
            ),
        ]
    )

    assert runner._created_stack_id_from_stream(stream, exclude={"rollback-stack"}) == "second-stack"


def test_created_stack_id_from_stream_accepts_continue_create_stack_success() -> None:
    runner = _load_runner()

    stream = SimpleNamespace(
        events=[
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="created-stack",
                status="CREATE_FAILED",
                is_success=False,
            ),
            _stack_current_changed_event(
                action="ContinueCreateStack",
                stack_id="created-stack",
                status="CREATE_COMPLETE",
                is_success=True,
            ),
        ]
    )

    assert runner._created_stack_id_from_stream(stream, exclude=set()) == "created-stack"


def test_created_stack_id_from_stream_accepts_ros_deploy_tool_result_success() -> None:
    runner = _load_runner()

    stream = SimpleNamespace(
        events=[
            _ros_deploy_tool_result_event(stack_id="failed-stack", is_error=True),
            _ros_deploy_tool_result_event(stack_id="created-stack"),
        ]
    )

    assert runner._created_stack_id_from_stream(stream, exclude=set()) == "created-stack"


def test_created_stack_id_from_stream_ignores_ros_deploy_tool_result_with_wrong_stack_name() -> None:
    runner = _load_runner()

    stream = SimpleNamespace(
        events=[
            _ros_deploy_tool_result_event(stack_id="wrong-stack", stack_name="wrong-name"),
            _ros_deploy_tool_result_event(stack_id="created-stack", stack_name="expected-name"),
        ]
    )

    assert (
        runner._created_stack_id_from_stream(stream, exclude=set(), expected_stack_name="expected-name")
        == "created-stack"
    )


def test_post_rollback_timeout_allows_step_regeneration_time() -> None:
    runner = _load_runner()

    args = SimpleNamespace(event_timeout=300, stream_timeout=2400)

    assert runner._post_rollback_timeout(args) == 900


def test_deterministic_fault_mode_still_runs_real_provider_preflight(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(
        [
            "--allow-real-cloud",
            "--deterministic",
            "--provider",
            "dashscope",
            "--run-dir",
            str(tmp_path),
            "--scenario",
            "fault-after-snapshot",
        ]
    )
    preflight = MagicMock(return_value={"ok": True, "summary": "ok"})
    monkeypatch.setattr(runner, "run_llm_preflight", preflight)
    harness = runner.ScenarioHarness(args, scenario="fault-after-snapshot")

    harness.preflight()

    preflight.assert_called_once()
    assert harness.checks["LLM preflight succeeded"] is True


def test_wait_any_ignores_finished_stream_when_another_stream_matches() -> None:
    runner = _load_runner()
    match = runner.EventMatch(
        description="target",
        event={"ok": True},
        summary=runner.StreamSummary(name="active", prompt=""),
    )

    class FinishedStream:
        name = "finished"

        def wait_for(self, *_args, **_kwargs):
            raise RuntimeError("finished ended before target")

    class ActiveStream:
        name = "active"

        def wait_for(self, *_args, **_kwargs):
            return match

    assert (
        runner._wait_any([FinishedStream(), ActiveStream()], lambda *_args: True, description="target", timeout=1)
        is match
    )


def test_cleanup_ledger_items_use_a2a_context_session_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = _load_runner()

    cwd = str((tmp_path / "workspace").resolve())
    Path(cwd).mkdir()
    run_dir = tmp_path / "run"
    contexts_dir = run_dir / "a2a-persistence" / "contexts"
    contexts_dir.mkdir(parents=True)
    (contexts_dir / "ctx-1.json").write_text(
        json.dumps({"context_id": "ctx-1", "session_id": "session-1", "cwd": cwd}),
        encoding="utf-8",
    )

    from iac_code.services.session_storage import SessionStorage

    ledger_dir = SessionStorage().session_dir(cwd, "session-1") / "pipeline"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "cleanup.yaml").write_text(
        "\n".join(
            [
                "schema_version: 1",
                "observed_resources:",
                "- provider: ros",
                "  resource_type: stack",
                "  resource_id: stack-1",
                "  observed_action: CreateStack",
                "cleanup_resources: []",
                "history: []",
            ]
        ),
        encoding="utf-8",
    )

    harness = SimpleNamespace(context_id="ctx-1", cwd=cwd, run_dir=run_dir)

    items = runner._cleanup_ledger_items(harness, "observed_resources")

    assert [item["resource_id"] for item in items] == ["stack-1"]


def test_cleanup_activity_snapshot_helper_ignores_empty_default_cleanup() -> None:
    runner = _load_runner()

    assert (
        runner._snapshot_has_cleanup_activity(
            {"snapshot": {"cleanup": {"status": "none", "resourceCount": 0, "resources": [], "history": []}}}
        )
        is False
    )
    assert runner._snapshot_has_cleanup_activity({"snapshot": {"cleanup": {"resourceCount": "1"}}}) is True
    assert runner._snapshot_has_cleanup_activity({"snapshot": {"cleanup": {"status": "pending"}}}) is True
    assert (
        runner._snapshot_has_cleanup_activity({"snapshot": {"cleanup": {"resources": [{"resourceId": "stack-1"}]}}})
        is True
    )
    cleanup_started_snapshot = {"snapshot": {"cleanup": {"history": [{"eventType": "cleanup_started"}]}}}
    assert runner._snapshot_has_cleanup_activity(cleanup_started_snapshot) is True
    assert (
        runner._snapshot_has_cleanup_activity(
            {
                "snapshot": {
                    "cleanup": {
                        "status": "unavailable",
                        "resourceCount": 0,
                        "resources": [],
                        "history": [
                            {
                                "eventType": "pipeline_handoff_ready",
                                "status": "unavailable",
                                "data": {"status": "unavailable"},
                            }
                        ],
                    }
                }
            }
        )
        is False
    )


def test_cleanup_activity_event_helper_detects_cleanup_events_and_handoff_data(tmp_path: Path) -> None:
    runner = _load_runner()
    normal_path = tmp_path / "normal.events.jsonl"
    cleanup_path = tmp_path / "cleanup.events.jsonl"
    handoff_path = tmp_path / "handoff.events.jsonl"
    normal_path.write_text(
        json.dumps(
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="stack-1",
                status="CREATE_COMPLETE",
                is_success=True,
            )
        ),
        encoding="utf-8",
    )
    cleanup_path.write_text(
        json.dumps(
            {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "cleanup_started",
                                    "scope": "cleanup",
                                    "data": {"resourceId": "stack-1"},
                                }
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    handoff_path.write_text(
        json.dumps(
            {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "pipeline_handoff_ready",
                                    "data": {"cleanup": {"resourceCount": 1}},
                                }
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert runner._events_file_has_cleanup_activity(normal_path) is False
    assert runner._events_file_has_cleanup_activity(cleanup_path) is True
    assert runner._events_file_has_cleanup_activity(handoff_path) is True
    assert runner._run_dir_has_cleanup_events(tmp_path) is True


def test_session_file_has_cleanup_prompt_uses_metadata_type(tmp_path: Path) -> None:
    runner = _load_runner()
    session_path = tmp_path / "session.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "visible"}),
                json.dumps(
                    {
                        "role": "user",
                        "content": "hidden cleanup prompt",
                        "metadata": {"type": "pipeline_cleanup_prompt"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    assert runner._session_file_has_cleanup_prompt(session_path) is True


def test_cleanup_ledger_required_resources_helper_ignores_observed_only() -> None:
    runner = _load_runner()
    harness = SimpleNamespace()

    assert runner._cleanup_ledger_has_required_resources(harness) is False

    original = runner._cleanup_ledger_items
    try:
        runner._cleanup_ledger_items = lambda _h, key: (
            [{"resource_id": "stack-1", "cleanup_required": False}]
            if key == "cleanup_resources"
            else [{"resource_id": "stack-observed"}]
        )
        assert runner._cleanup_ledger_has_required_resources(harness) is False
        runner._cleanup_ledger_items = lambda _h, key: (
            [{"resource_id": "stack-2", "cleanup_required": True}] if key == "cleanup_resources" else []
        )
        assert runner._cleanup_ledger_has_required_resources(harness) is True
    finally:
        runner._cleanup_ledger_items = original


def test_cleanup_prompts_require_fresh_creation_and_fault_window_without_fixed_names(tmp_path: Path) -> None:
    runner = _load_runner()
    harness = SimpleNamespace(run_dir=tmp_path)
    first = runner._cleanup_deployment_prompt("选择方案。", harness, "first")
    second = runner._cleanup_deployment_prompt("选择方案。", harness, "second")
    assert "必须新建一个 ROS stack" in first
    assert "已有 stack" in first
    assert "等待用户下一条指令" in first
    assert "不要调用 complete_step" in first
    assert "complete_step 前必须" in second
    assert "StackName" not in first + second
    assert first != second


def test_rollback_step5_cleanup_flow_cleans_first_stack_and_keeps_second(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    fake_harnesses = []

    class FakeStream:
        def __init__(self, summary: object, events: list[dict] | None = None) -> None:
            self.summary = summary
            self.name = summary.name
            self.events = events or []

        def wait_for(self, *_args, **_kwargs):
            return None

        def join(self, timeout: float):
            return self.summary

    class FakeHarness:
        def __init__(self) -> None:
            self.args = SimpleNamespace(stream_timeout=1, event_timeout=1)
            self.run_dir = tmp_path
            self.server_env = {}
            self.cwd = str(tmp_path)
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.checks: dict[str, bool] = {}
            self.notes: list[str] = []
            self.summaries = {}
            self.snapshots = {}
            self.stream_calls: list[dict] = []
            self.started_streams: list[str] = []
            self.cleanup_reads = 0

        def stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.stream_calls.append({"prompt": prompt, "name": name, "task_id": task_id})
            is_initial = name == "01-initial"
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"] if is_initial else ["TASK_STATE_COMPLETED"],
                pipeline_event_types=["input_required"] if is_initial else ["pipeline_completed"],
                last_input_required_step_id="confirm_and_select" if is_initial else "",
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            return summary

        def start_stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.started_streams.append(name)
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                pipeline_event_types=["pipeline_completed"],
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            events = []
            if name == "04-select-second-stack":
                events.append(
                    _stack_current_changed_event(
                        action="CreateStack",
                        stack_id="stack-2",
                        stack_name=runner._cleanup_stack_name(self, "second"),
                        status="CREATE_COMPLETE",
                        is_success=True,
                    )
                )
            return FakeStream(summary, events=events)

        def fetch_state(self, name: str):
            if name == "after-cleanup":
                self.cleanup_reads += 1
            cleanup_done = self.cleanup_reads > 1
            snapshot = {
                "snapshot": {
                    "status": "completed",
                    "cleanup": {
                        "status": "completed",
                        "resources": [
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-1",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "completed" if cleanup_done else "running",
                                "stackStatus": "DELETE_COMPLETE" if cleanup_done else "DELETE_IN_PROGRESS",
                            }
                        ],
                    },
                    "stacks": {
                        "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                        "byId": {"stack-2": {"stackId": "stack-2", "current": True}},
                    },
                }
            }
            self.snapshots[name] = snapshot
            return snapshot

        def kill9_and_restart(self) -> None:
            self.notes.append("restarted")

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        fake_harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    cleanup_ledger_items = [
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-1",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        }
    ]

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_answer_intervening_ask_inputs", lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, "_wait_for_created_stack", lambda *_args, **_kwargs: "stack-1")
    monkeypatch.setattr(runner, "_wait_any", lambda *_args, **_kwargs: None)
    native_wait = runner._wait_for_with_intervening_ask_inputs
    waited_inputs = []

    def wait_with_questions(*args, **kwargs):
        waited_inputs.append(kwargs["name_prefix"])
        return native_wait(*args, **kwargs)

    monkeypatch.setattr(runner, "_wait_for_with_intervening_ask_inputs", wait_with_questions)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *_args, **_kwargs: _args[1])
    monkeypatch.setattr(
        runner,
        "_cleanup_ledger_items",
        lambda _h, key: cleanup_ledger_items if key == "cleanup_resources" else [],
    )
    monkeypatch.setattr(
        runner,
        "_capture_ros_stack_states",
        lambda h, stack_ids, name: {
            "stack-1": {"status": "DELETE_COMPLETE" if h.cleanup_reads > 1 else "DELETE_IN_PROGRESS"},
            "stack-2": {"status": "CREATE_COMPLETE"},
        },
    )
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    )

    assert runner.run_rollback_step5_cleanup(args, "rollback-step5-cleanup") == 0
    harness = fake_harnesses[0]
    assert "03-post-rollback" in waited_inputs
    assert "StackName" not in harness.stream_calls[0]["prompt"]
    assert "StackName" not in harness.summaries["03-rollback-after-first-stack"].prompt
    assert harness.stream_calls[-1]["task_id"] == ""
    assert harness.checks["first rollback stack cleanup completed in snapshot"] is True
    assert harness.checks["rollback cleanup stacks completed in snapshot"] is True
    assert harness.checks["ROS first rollback stack deleted"] is True
    assert harness.checks["ROS rollback cleanup stacks deleted"] is True
    assert harness.checks["ROS second stack retained"] is True
    assert harness.snapshots["cleanup_verify_attempts"] == 2


def test_rollback_step5_cleanup_recovery_uses_tool_safe_recovery_prompt(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    fake_harnesses = []

    class FakeStream:
        def __init__(self, summary: object, events: list[dict] | None = None) -> None:
            self.summary = summary
            self.name = summary.name
            self.events = events or []

        def wait_for(self, *_args, **_kwargs):
            return None

        def join(self, timeout: float):
            return self.summary

    class FakeHarness:
        def __init__(self) -> None:
            self.args = SimpleNamespace(stream_timeout=1, event_timeout=1)
            self.run_dir = tmp_path
            self.server_env = {}
            self.cwd = str(tmp_path)
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.checks: dict[str, bool] = {}
            self.notes: list[str] = []
            self.summaries = {}
            self.snapshots = {}
            self.stream_calls: list[dict] = []

        def stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.stream_calls.append({"prompt": prompt, "name": name, "task_id": task_id})
            is_initial = name == "01-initial"
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"] if is_initial else ["TASK_STATE_COMPLETED"],
                pipeline_event_types=["input_required"] if is_initial else ["pipeline_completed"],
                last_input_required_step_id="confirm_and_select" if is_initial else "",
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            return summary

        def start_stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                pipeline_event_types=["pipeline_completed"],
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            events = []
            if name == "04-select-second-stack":
                events.append(
                    _stack_current_changed_event(
                        action="CreateStack",
                        stack_id="stack-2",
                        stack_name=runner._cleanup_stack_name(self, "second"),
                        status="CREATE_COMPLETE",
                        is_success=True,
                    )
                )
            return FakeStream(summary, events=events)

        def fetch_state(self, name: str):
            snapshot = {
                "snapshot": {
                    "status": "completed",
                    "cleanup": {
                        "status": "completed",
                        "resources": [
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-1",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "completed",
                                "stackStatus": "DELETE_COMPLETE",
                            }
                        ],
                    },
                    "stacks": {
                        "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                        "byId": {"stack-2": {"stackId": "stack-2", "current": True}},
                    },
                }
            }
            self.snapshots[name] = snapshot
            return snapshot

        def kill9_and_restart(self) -> None:
            self.notes.append("restarted")

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        fake_harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    cleanup_ledger_items = [
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-1",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        }
    ]

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_answer_intervening_ask_inputs", lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, "_wait_for_created_stack", lambda *_args, **_kwargs: "stack-1")
    monkeypatch.setattr(runner, "_wait_any", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *_args, **_kwargs: _args[1])
    monkeypatch.setattr(runner, "_wait_for_cleanup_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_join_after_kill", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_events_file_has_cleanup_event",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        runner,
        "_cleanup_ledger_items",
        lambda _h, key: cleanup_ledger_items if key == "cleanup_resources" else [],
    )
    monkeypatch.setattr(
        runner,
        "_capture_ros_stack_states",
        lambda _h, stack_ids, name: {
            "stack-1": {"status": "DELETE_COMPLETE"},
            "stack-2": {"status": "CREATE_COMPLETE"},
        },
    )

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    )

    assert runner.run_rollback_step5_cleanup_recovery(args, "rollback-step5-cleanup-recovery") == 0
    recovery_prompt = next(
        call["prompt"] for call in fake_harnesses[0].stream_calls if call["name"] == "06-cleanup-after-restart"
    )
    assert recovery_prompt != runner.CONTINUE_PROMPT
    assert "不要调用任何工具" in recovery_prompt
    assert "不要查询" in recovery_prompt
    assert "不要删除" in recovery_prompt


def test_rollback_step5_cleanup_flow_fails_when_any_cleanup_stack_is_left(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()

    class FakeStream:
        def __init__(self, summary: object, events: list[dict] | None = None) -> None:
            self.summary = summary
            self.name = summary.name
            self.events = events or []

        def wait_for(self, *_args, **_kwargs):
            return None

        def join(self, timeout: float):
            return self.summary

    class FakeHarness:
        def __init__(self) -> None:
            self.args = SimpleNamespace(stream_timeout=1, event_timeout=1)
            self.run_dir = tmp_path
            self.server_env = {}
            self.cwd = str(tmp_path)
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.checks: dict[str, bool] = {}
            self.notes: list[str] = []
            self.summaries = {}
            self.snapshots = {}

        def stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            is_initial = name == "01-initial"
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"] if is_initial else ["TASK_STATE_COMPLETED"],
                pipeline_event_types=["input_required"] if is_initial else ["pipeline_completed"],
                last_input_required_step_id="confirm_and_select" if is_initial else "",
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            return summary

        def start_stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                pipeline_event_types=["pipeline_completed"],
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            events = []
            if name == "04-select-second-stack":
                events.append(
                    _stack_current_changed_event(
                        action="CreateStack",
                        stack_id="stack-2",
                        stack_name=runner._cleanup_stack_name(self, "second"),
                        status="CREATE_COMPLETE",
                        is_success=True,
                    )
                )
            return FakeStream(summary, events=events)

        def fetch_state(self, name: str):
            snapshot = {
                "snapshot": {
                    "status": "completed",
                    "cleanup": {
                        "status": "pending",
                        "resources": [
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-1",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "completed",
                                "stackStatus": "DELETE_COMPLETE",
                            },
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-left",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "pending",
                                "stackStatus": "CREATE_COMPLETE",
                            },
                        ],
                    },
                    "stacks": {
                        "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                        "byId": {"stack-2": {"stackId": "stack-2", "current": True}},
                    },
                }
            }
            self.snapshots[name] = snapshot
            return snapshot

        def kill9_and_restart(self) -> None:
            raise AssertionError("non-recovery scenario should not restart")

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    cleanup_ledger_items = [
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-1",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        },
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-left",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        },
    ]

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_answer_intervening_ask_inputs", lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, "_wait_for_created_stack", lambda *_args, **_kwargs: "stack-1")
    monkeypatch.setattr(runner, "_wait_any", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *_args, **_kwargs: _args[1])
    monkeypatch.setattr(
        runner,
        "_cleanup_ledger_items",
        lambda _h, key: cleanup_ledger_items if key == "cleanup_resources" else [],
    )
    monkeypatch.setattr(
        runner,
        "_capture_ros_stack_states",
        lambda _h, stack_ids, name: {
            "stack-1": {"status": "DELETE_COMPLETE"},
            "stack-left": {"status": "CREATE_COMPLETE"},
            "stack-2": {"status": "CREATE_COMPLETE"},
        },
    )

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    )

    assert runner.run_rollback_step5_cleanup(args, "rollback-step5-cleanup") == 1


def test_rollback_step5_cleanup_recovery_kills_and_retriggers_cleanup(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    fake_harnesses = []

    class FakeStream:
        def __init__(self, summary: object, events: list[dict] | None = None) -> None:
            self.summary = summary
            self.name = summary.name
            self.events = events or []

        def wait_for(self, *_args, **_kwargs):
            return None

        def join(self, timeout: float):
            return self.summary

    class FakeHarness:
        def __init__(self) -> None:
            self.args = SimpleNamespace(stream_timeout=1, event_timeout=1)
            self.run_dir = tmp_path
            self.server_env = {}
            self.cwd = str(tmp_path)
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.checks: dict[str, bool] = {}
            self.notes: list[str] = []
            self.summaries = {}
            self.snapshots = {}
            self.stream_calls: list[dict] = []
            self.started_streams: list[dict] = []
            self.kill_count = 0

        def stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.stream_calls.append({"prompt": prompt, "name": name, "task_id": task_id})
            is_initial = name == "01-initial"
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"] if is_initial else ["TASK_STATE_COMPLETED"],
                pipeline_event_types=["input_required"] if is_initial else ["pipeline_completed"],
                last_input_required_step_id="confirm_and_select" if is_initial else "",
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            return summary

        def start_stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.started_streams.append({"prompt": prompt, "name": name, "task_id": task_id})
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                pipeline_event_types=["pipeline_completed"],
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            events = []
            if name == "04-select-second-stack":
                events.append(
                    _stack_current_changed_event(
                        action="CreateStack",
                        stack_id="stack-2",
                        stack_name=runner._cleanup_stack_name(self, "second"),
                        status="CREATE_COMPLETE",
                        is_success=True,
                    )
                )
            return FakeStream(summary, events=events)

        def fetch_state(self, name: str):
            snapshot = {
                "snapshot": {
                    "status": "completed",
                    "cleanup": {
                        "status": "completed",
                        "resources": [
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-1",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "completed",
                                "stackStatus": "DELETE_COMPLETE",
                            }
                        ],
                    },
                    "stacks": {
                        "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                        "byId": {"stack-2": {"stackId": "stack-2", "current": True}},
                    },
                }
            }
            self.snapshots[name] = snapshot
            return snapshot

        def kill9_and_restart(self) -> None:
            self.kill_count += 1

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        fake_harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    cleanup_ledger_items = [
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-1",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        }
    ]

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_answer_intervening_ask_inputs", lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, "_wait_for_created_stack", lambda *_args, **_kwargs: "stack-1")
    monkeypatch.setattr(runner, "_wait_any", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *_args, **_kwargs: _args[1])
    monkeypatch.setattr(runner, "_wait_for_cleanup_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_events_file_has_cleanup_event", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        runner,
        "_cleanup_ledger_items",
        lambda _h, key: cleanup_ledger_items if key == "cleanup_resources" else [],
    )
    monkeypatch.setattr(
        runner,
        "_capture_ros_stack_states",
        lambda _h, stack_ids, name: {
            "stack-1": {"status": "DELETE_COMPLETE"},
            "stack-2": {"status": "CREATE_COMPLETE"},
        },
    )

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    )

    assert runner.run_rollback_step5_cleanup_recovery(args, "rollback-step5-cleanup-recovery") == 0
    harness = fake_harnesses[0]
    assert harness.kill_count == 1
    assert harness.started_streams[-1] == {
        "prompt": runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
        "name": "05-cleanup-running",
        "task_id": "",
    }
    assert harness.stream_calls[-1] == {
        "prompt": runner.CLEANUP_RECOVERY_PROMPT,
        "name": "06-cleanup-after-restart",
        "task_id": "",
    }
    assert harness.checks["cleanup retriggered after restart"] is True


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
        '  },\n  "selected_plan": {"selected_candidate_result": {"template": {"template": '
        + json.dumps(json.dumps({'Resources': {'Group': {'Type': 'ALIYUN::ECS::SecurityGroup'}}}))
        + '}}}\n'
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
            self.diagnostics = {}

        def _ci_owned_prompt(self, text):
            return text

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

    finish_kwargs: list[dict] = []

    def fake_finish_pipeline_after_possible_input(*_args, **kwargs):
        assert _args[0].current_goal == "请先回退到 intent_parsing 步骤。" + runner.ROLLBACK_PROMPT
        finish_kwargs.append(kwargs)

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_wait_for_with_intervening_ask_inputs", lambda *args, **kwargs: [args[1][0]])
    monkeypatch.setattr(runner, "_wait_any", lambda *args, **kwargs: SimpleNamespace(event=_pipeline_batch(
        {"eventType": "rollback_completed", "sequence": 10}
    )))
    monkeypatch.setattr(runner, "_join_after_kill", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", fake_finish_pipeline_after_possible_input)
    monkeypatch.setattr(runner, "_completed_snapshot_or_stream", lambda *args, **kwargs: True)

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
    )

    assert runner.run_rollback(args, "rollback-step1") == 0
    assert finish_kwargs == [{"input_prompt": "请先回退到 intent_parsing 步骤。" + runner.ROLLBACK_PROMPT}]


def test_final_deployment_evidence_uses_handoff_target_when_deploy_failed() -> None:
    runner = _load_runner()
    handoff_context = {
        "intent": {
            "core_requirements": ["VPC", "VSwitch"],
            "resource_intents": [
                {"product": "VPC", "action": "use_existing"},
                {"product": "VSwitch", "action": "create"},
            ],
        },
        "architecture": {
            "candidates": [
                {
                    "name": "已有VPC创建安全组",
                    "products": ["VPC", "SecurityGroup"],
                    "resource_intents": [
                        {
                            "resource_type": "ALIYUN::ECS::SecurityGroup",
                            "action": "create",
                        }
                    ],
                    "cons": ["不提供 VSwitch 等网络基础设施"],
                }
            ]
        },
        "evaluated_candidates": [{"template_path": "templates/1-existing-vpc-security-group.yml"}],
        "selected_plan": {
            "selected_candidate": {
                "products": ["SecurityGroup"],
                "resource_intents": [
                    {"product": "VPC", "action": "use_existing"},
                    {"product": "SecurityGroup", "action": "create"},
                    {"product": "VSwitch", "action": "forbid"},
                ],
            },
            "resource_types": ["ALIYUN::ECS::SecurityGroup"],
        },
        "deployment": {"status": "failed", "error": "STS token exchange denied"},
    }
    handoff_summary = (
        "[Pipeline Handoff Context]\n"
        "This is injected context for the assistant, not a user request.\n"
        "Pipeline: selling\n"
        "Outcome: completed\n\n"
        "Included context:\n"
        f"{json.dumps(handoff_context, ensure_ascii=False)}\n\n"
        "Use this context when answering follow-up questions after the pipeline handoff."
    )
    final_state = {
        "snapshot": {
            "steps": [
                {
                    "id": "deploying",
                    "status": "completed",
                    "conclusion": {"status": "failed", "error": "STS token exchange denied"},
                }
            ],
            "normalHandoff": {"summary": handoff_summary},
        }
    }

    evidence = runner._final_deployment_evidence(final_state)

    assert "SecurityGroup" in evidence
    assert "VSwitch" not in evidence


def test_final_deployment_evidence_prefers_realized_target_over_stale_candidate() -> None:
    runner = _load_runner()
    stale_candidate = {
        "name": "已有 VPC 中创建 VSwitch",
        "output_path": "templates/1-existing-vpc-create-vswitch.yml",
        "products": ["VPC", "VSwitch"],
        "resource_intents": [
            {"product": "VPC", "action": "use_existing"},
            {"product": "VSwitch", "action": "create"},
        ],
        "topology": "在已有 VPC 中创建一个 VSwitch。",
    }
    handoff_context = {
        "selected_plan": {
            "selected_candidate_name": stale_candidate["name"],
            "selected_candidate": stale_candidate,
            "selected_candidate_result": {
                "candidate": stale_candidate,
                "failed": False,
                "template": {
                    "template": (
                        "ROSTemplateFormatVersion: '2015-09-01'\n"
                        "Resources:\n"
                        "  SecurityGroup:\n"
                        "    Type: ALIYUN::ECS::SecurityGroup\n"
                    ),
                    "file_path": "templates/1-existing-vpc-create-security-group.yml",
                    "region": "cn-hangzhou",
                    "description": "在已有 VPC 中创建安全组",
                },
                "cost": {
                    "resources": [{"type": "ALIYUN::ECS::SecurityGroup", "cost": "¥0"}],
                    "deployment_parameters": {
                        "RegionId": "cn-hangzhou",
                        "VpcId": "vpc-test",
                        "SecurityGroupName": "sg-test",
                    },
                    "preview_validation": {
                        "succeeded": True,
                        "template_url": "templates/1-existing-vpc-create-security-group.yml",
                    },
                },
            },
        },
        "deployment": {
            "resources_created": ["ALIYUN::ECS::SecurityGroup"],
            "stack_id": "stack-test",
            "status": "success",
            "outputs": {"SecurityGroupId": "sg-test"},
        },
    }
    handoff_summary = (
        "[Pipeline Handoff Context]\n"
        "This is injected context for the assistant, not a user request.\n"
        "Pipeline: selling\n"
        "Outcome: completed\n\n"
        "Included context:\n"
        f"{json.dumps(handoff_context, ensure_ascii=False)}\n\n"
        "Use this context when answering follow-up questions after the pipeline handoff."
    )
    final_state = {
        "snapshot": {
            "steps": [{"id": "deploying", "status": "completed", "conclusion": {"status": "success"}}],
            "normalHandoff": {"summary": handoff_summary},
        }
    }

    evidence = runner._final_deployment_evidence(final_state)

    assert "SecurityGroup" in evidence
    assert "VSwitch" not in evidence


def test_finish_pipeline_answers_clarification_inside_selection_step_before_followup(tmp_path, monkeypatch):
    runner = _load_runner()
    pending = runner.StreamSummary(name='selection', prompt='select', status_states=['TASK_STATE_INPUT_REQUIRED'],
        pipeline_event_types=['input_required'], last_input_required_step_id='confirm_and_select')
    done = runner.StreamSummary(name='done', prompt='answer', status_states=['TASK_STATE_COMPLETED'],
                               pipeline_event_types=['pipeline_completed'], normal_handoff_ready=True)
    (tmp_path / 'selection.events.jsonl').write_text(
        json.dumps({'pipeline': {'eventType': 'input_required',
                                'data': {'kind': 'ask_user_question', 'question': '用途?'}}}) + '\n', encoding="utf-8")
    calls = []
    def stream(**kwargs):
        calls.append(kwargs)
        return done
    h = SimpleNamespace(run_dir=tmp_path, current_goal='只创建安全组，不创建VSwitch', stream=stream)
    monkeypatch.setattr(runner, '_answer_pending_legacy_question', lambda _h, _s, goal: goal)
    final = runner._finish_pipeline_after_possible_input(h, pending, SimpleNamespace(selection_prompt='选择方案'))
    assert final is done
    assert final.normal_handoff_ready
    assert calls == [{'prompt': '只创建安全组，不创建VSwitch', 'name': 'answer-after-resume-1'}]


@pytest.mark.parametrize("event_type", ["step_started", "input_required"])
def test_post_rollback_wait_ignores_buffered_old_step_and_unrelated_new_event(event_type):
    runner = _load_runner()
    summary = runner.StreamSummary(name="fixture", prompt="fixture")
    old = {"eventType": event_type, "sequence": 8, "step": {"id": "confirm_and_select"}}
    predicate = (runner._step_started if event_type == "step_started" else runner._input_required_step)
    assert predicate("confirm_and_select")(_pipeline_batch(old), summary)
    bounded = predicate("confirm_and_select", after_sequence=10)
    assert not bounded(_pipeline_batch(old, {
        "eventType": event_type, "sequence": 20, "step": {"id": "intent_parsing"},
    }), summary)
    assert bounded(_pipeline_batch({**old, "sequence": 11}), summary)
    for sequence in (None, True, 10, 9):
        assert not bounded(_pipeline_batch({**old, "sequence": sequence}), summary)


def test_rollback_boundary_uses_matching_durable_event_and_fails_closed_without_sequence():
    runner = _load_runner()
    match = SimpleNamespace(event=_pipeline_batch(
        {"eventType": "rollback_completed", "sequence": 10},
        {"eventType": "step_started", "sequence": 20},
    ))
    assert runner._matched_pipeline_sequence(match, "rollback_completed") == 10
    with pytest.raises(RuntimeError, match="durable pipeline event sequence"):
        runner._matched_pipeline_sequence(SimpleNamespace(event=_pipeline_batch(
            {"eventType": "rollback_completed"},
        )), "rollback_completed")


def test_rollback_boundary_accepts_real_protobuf_struct_wire_numbers():
    from google.protobuf.json_format import MessageToDict, ParseDict
    from google.protobuf.struct_pb2 import Struct

    runner = _load_runner()
    summary = runner.StreamSummary(name="wire", prompt="fixture")

    def wire(event_type, sequence):
        metadata = ParseDict({"iac_code": {"pipeline": {
            "eventType": event_type, "sequence": sequence, "step": {"id": "confirm_and_select"},
        }}}, Struct())
        return {"metadata": MessageToDict(metadata)}

    rollback = wire("rollback_completed", 10)
    assert type(runner._extract_pipeline_envelopes(rollback)[0]["sequence"]) is float
    boundary = runner._matched_pipeline_sequence(SimpleNamespace(event=rollback), "rollback_completed")
    assert boundary == 10
    factories = [("step_started", runner._step_started), ("input_required", runner._input_required_step)]
    for event_type, factory in factories:
        predicate = factory("confirm_and_select", after_sequence=boundary)
        assert not predicate(wire(event_type, 9), summary)
        assert not predicate(wire(event_type, 10), summary)
        assert predicate(wire(event_type, 11), summary)


@pytest.mark.parametrize("value", [True, False, None, "11", 10.5, float("nan"), float("inf"), -1.0, 0.0, float(2**53)])
def test_invalid_or_inexact_wire_sequences_cannot_cross_rollback_boundary(value):
    runner = _load_runner()
    assert runner._pipeline_event_sequence({"sequence": value}) is None
    event = {"eventType": "step_started", "sequence": value, "step": {"id": "confirm_and_select"}}
    assert not runner._step_started("confirm_and_select", after_sequence=10)(event, runner.StreamSummary("x", "x"))


def test_target_diagnostics_distinguish_deploying_step_from_handoff_without_raw_values():
    runner = _load_runner()
    context = {'deployment': {'resources_created': ['ALIYUN::ECS::SecurityGroup'], 'stack_id': 'private-stack'}}
    state = {'snapshot': {
        'steps': [{'id': 'deploying', 'status': 'completed', 'conclusion': {'resource_type': 'VSwitch'}},
                  {'id': 'intent_parsing', 'status': 'completed',
                   'conclusion': {'resource_type': 'ALIYUN::ECS::SecurityGroup'}},
                  {'id': 'architecture_planning', 'status': 'completed',
                   'conclusion': {'resource_type': 'ALIYUN::ECS::SecurityGroup'}}],
        'normalHandoff': {'summary': 'Included context:\n' + json.dumps(context) + '\n\nUse this context'},
    }}
    h = SimpleNamespace(diagnostics={})
    runner._record_final_target_diagnostics(h, state)
    assert h.diagnostics == {'final_target_handoff_present': True, 'final_target_context_present': True,
                             'final_target_selected_plan_present': False,
                             'final_target_step_security_group': False, 'final_target_step_vswitch': True,
                             'final_target_handoff_security_group': True, 'final_target_handoff_vswitch': False,
                             'final_target_intent_security_group': True, 'final_target_intent_vswitch': False,
                             'final_target_architecture_security_group': True,
                             'final_target_architecture_vswitch': False}
    assert 'private' not in json.dumps(h.diagnostics)


def test_complete_pipeline_answers_selection_clarification_before_acceptance(tmp_path, monkeypatch):
    runner = _load_runner()
    initial = runner.StreamSummary(name='initial', prompt='goal', status_states=['TASK_STATE_INPUT_REQUIRED'],
        pipeline_event_types=['input_required'], last_input_required_step_id='confirm_and_select')
    pending = runner.StreamSummary(name='selection', prompt='select', status_states=['TASK_STATE_INPUT_REQUIRED'],
        pipeline_event_types=['input_required'], last_input_required_step_id='confirm_and_select')
    done = runner.StreamSummary(name='done', prompt='answer', status_states=['TASK_STATE_COMPLETED'],
        pipeline_event_types=['pipeline_completed'], normal_handoff_ready=True)
    (tmp_path / 'selection.events.jsonl').write_text(json.dumps(_input_required_event('ask_user_question')) + '\n',
                                                   encoding='utf-8')
    responses = iter([initial, pending, done])
    calls = []

    def stream(**kwargs):
        calls.append(kwargs['prompt'])
        return next(responses)

    h = SimpleNamespace(run_dir=tmp_path, current_goal='只创建测试 VSwitch', checks={}, snapshots={},
                        stream=stream, fetch_state=lambda _name: {'status': 'completed'})
    monkeypatch.setattr(runner, '_answer_intervening_ask_inputs', lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, '_answer_pending_legacy_question', lambda _h, _s, goal: goal)
    runner._complete_pipeline(h, SimpleNamespace(initial_prompt='goal', selection_prompt='select'))
    assert calls == ['goal', 'select', '只创建测试 VSwitch']
    assert h.checks == {'initial reached step4 selection': True, 'selection completed pipeline': True,
                        'selection produced normal handoff': True}


def test_intervening_question_inside_selection_step_is_answered(tmp_path, monkeypatch):
    runner = _load_runner()
    pending = runner.StreamSummary(name='pending', prompt='goal', status_states=['TASK_STATE_INPUT_REQUIRED'],
        pipeline_event_types=['input_required'], last_input_required_step_id='confirm_and_select')
    selection = runner.StreamSummary(name='ready', prompt='answer', status_states=['TASK_STATE_INPUT_REQUIRED'],
        pipeline_event_types=['input_required'], last_input_required_step_id='confirm_and_select')
    (tmp_path / 'pending.events.jsonl').write_text(json.dumps(_input_required_event('ask_user_question')) + '\n',
                                                 encoding='utf-8')
    calls = []

    def stream(**kwargs):
        calls.append(kwargs['prompt'])
        return selection

    h = SimpleNamespace(run_dir=tmp_path, current_goal='fixture goal', notes=[], stream=stream)
    monkeypatch.setattr(runner, '_answer_pending_legacy_question', lambda _h, _s, goal: goal)
    assert runner._answer_intervening_ask_inputs(h, pending, name_prefix='initial') is selection
    assert calls == ['fixture goal']


def test_ci_preflight_pins_stable_fixture_without_changing_initial_case_goal(monkeypatch, tmp_path):
    runner = _load_runner()
    config = tmp_path / 'config'
    config.mkdir()
    harness = SimpleNamespace(args=SimpleNamespace(ci_teardown=True, allow_real_cloud=True,
        skip_preflight=True, python='python'), server_env={'IAC_CODE_CONFIG_DIR': str(config)},
        server_cwd=str(tmp_path), notes=[], owned_stack_names=['iac-e2e-owned-main'])
    monkeypatch.setattr(runner, 'network_facts', lambda *_: {
        'vpc_id': 'vpc-stable-fixture', 'zone_id': 'cn-hangzhou-i', 'cidr': '10.250.1.0/24'})
    runner.ScenarioHarness.preflight(harness)
    assert harness.network_fixture_facts['vpc_id'] == 'vpc-stable-fixture'
    assert '不得复用其它 E2E Stack 创建的临时 VPC' in (config / 'IAC-CODE-E2E.md').read_text(encoding='utf-8')
    assert harness.server_env['IAC_CODE_INSTRUCTION_MEMORY_FILE'] == 'IAC-CODE-E2E.md'


def test_image_initial_drives_refreshed_selection_but_still_requires_completion(monkeypatch, tmp_path):
    runner = _load_runner()
    initial = runner.StreamSummary(name='initial', prompt='', status_states=['TASK_STATE_INPUT_REQUIRED'],
        last_input_required_step_id='confirm_and_select')
    selection = runner.StreamSummary(name='selected', prompt='', status_states=['TASK_STATE_INPUT_REQUIRED'],
        last_input_required_step_id='confirm_and_select')
    completed = runner.StreamSummary(name='completed', prompt='', status_states=['TASK_STATE_COMPLETED'],
        pipeline_event_types=['pipeline_completed'])
    h = SimpleNamespace(checks={}, stream_image_text=lambda **_: initial, stream=lambda **_: selection)
    monkeypatch.setattr(runner, '_answer_intervening_ask_inputs', lambda _h, s, **_: s)
    monkeypatch.setattr(runner, '_all_evidence', lambda _: 'ALIYUN::ECS::VSwitch')
    monkeypatch.setattr(runner, '_run_with_harness', lambda _a, _s, cb: cb(h))
    seen = []
    monkeypatch.setattr(runner, '_finish_pipeline_after_possible_input',
        lambda _h, s, _a: seen.append(s) or completed)
    args = SimpleNamespace(initial_prompt='fixture', selection_prompt='select')
    runner.run_image_initial(args, 'image-initial')
    assert seen == [selection]
    assert h.checks['image initial selection completed pipeline'] is True
    monkeypatch.setattr(runner, '_finish_pipeline_after_possible_input', lambda _h, s, _a: s)
    runner.run_image_initial(args, 'image-initial')
    assert h.checks['image initial selection completed pipeline'] is False


@pytest.mark.parametrize(('cpu', 'memory', 'passed'), [(2, 4, True), (2, 8, False), (4, 4, False)])
def test_independent_2c4g_oracle_rejects_wrong_real_sku_despite_satisfied_model_claim(
    monkeypatch, tmp_path, cpu, memory, passed,
):
    runner = _load_runner()
    sku = 'ecs.test.large'
    conclusion = {'deployment_parameters': {'InstanceType': sku}, 'hard_constraint_checks': [
        {'constraint': {'property': property_name, 'value': value, 'unit': unit}, 'status': 'satisfied',
         'actual_value': value, 'actual_unit': unit, 'parameter_values': {'InstanceType': sku}, 'evidence': []}
        for property_name, value, unit in [('vcpu', 2, 'count'), ('memory', 4, 'GiB')]]}
    snapshot = {'display': {'toolResults': [{'toolName': 'complete_step', 'isError': False,
                                           'input': {'conclusion': conclusion}}]}}
    h = SimpleNamespace(args=SimpleNamespace(python=sys.executable), server_cwd=str(tmp_path),
                        server_env={}, diagnostics={})

    def api(product, action, params):
        assert (product, action, params) == ('ecs', 'DescribeInstanceTypes', {'InstanceTypes': [sku]})
        print('private provider diagnostics')
        return {'InstanceTypes': {'InstanceType': [
            {'InstanceTypeId': sku, 'CpuCoreCount': cpu, 'MemorySize': memory}]}}

    monkeypatch.setitem(sys.modules, 'scripts.repl.e2e.run_pipeline_scenarios', SimpleNamespace(_call_aliyun_api=api))
    monkeypatch.setitem(sys.modules, 'scripts.a2a.e2e.run_recovery_scenarios', runner)

    def execute(command, **kwargs):
        assert kwargs['timeout'] == 45 and kwargs['env'] == {}
        monkeypatch.setattr(sys, 'argv', [command[0], *command[3:]])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(compile(command[2], 'readonly-sdk-oracle', 'exec'), {})
        assert 'private' not in output.getvalue()
        return SimpleNamespace(stdout=output.getvalue())

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    assert runner._has_2c4g_structured_evidence(snapshot) is False
    assert runner._verify_final_2c4g_with_sdk(h, snapshot) is passed
    assert h.diagnostics['2c4g_independent_sdk_verified'] is passed
    assert h.diagnostics['2c4g_cost_completion_count'] == 1
    assert h.diagnostics['2c4g_sdk_returned_count'] == 1
    assert h.diagnostics['2c4g_sdk_cpu_mismatch_count'] == int(cpu != 2)
    assert h.diagnostics['2c4g_sdk_memory_mismatch_count'] == int(memory != 4)
    assert h.diagnostics['2c4g_sdk_observed_sizes'] == [{'cpu': cpu, 'memoryGiB': memory}]
    conclusion['hard_constraint_checks'][1]['actual_unit'] = 'MiB'
    # A malformed model claim cannot change the independently verified real SKU.
    assert runner._verify_final_2c4g_with_sdk(h, snapshot) is passed
    assert h.diagnostics['2c4g_sdk_actual_types_correct'] is passed
    if passed:
        assert h.diagnostics['2c4g_sdk_probe_category'] == 'incomplete_model_verification'


@pytest.mark.parametrize('actual_correct', [True, False])
def test_web_2c4g_acceptance_always_uses_real_sdk_oracle(monkeypatch, actual_correct):
    runner = _load_runner()
    original = runner.StreamSummary(name='initial', prompt=runner.IAC_CODE_WEB_2C4G_PROMPT,
                                   last_input_required_step_id='confirm_and_select')
    h = SimpleNamespace(checks={}, diagnostics={}, notes=[], summaries={},
                        stream=lambda **_: original, fetch_state=lambda _: {})
    snapshot = {'display': {'toolResults': [
        {'toolName': 'ros_preview_template', 'sequence': 1},
        {'toolName': 'ros_estimate_template_cost', 'sequence': 2},
    ]}}
    monkeypatch.setattr(runner, '_run_with_harness', lambda a, s, cb: cb(h))
    monkeypatch.setattr(runner, '_answer_intervening_ask_inputs', lambda h, s, **kw: s)
    monkeypatch.setattr(runner, '_snapshot_value', lambda *a: 'waiting_input')
    monkeypatch.setattr(runner, '_pending_step_id', lambda _: 'confirm_and_select')
    monkeypatch.setattr(runner, '_load_canonical_pipeline_snapshot', lambda _: snapshot)
    monkeypatch.setattr(runner, '_golden_solution_evidenced', lambda _: True)
    # This could previously short-circuit the independent verification.
    monkeypatch.setattr(runner, '_has_2c4g_structured_evidence', lambda _: True)
    calls = []
    def sdk(harness, state):
        calls.append(state)
        return actual_correct
    monkeypatch.setattr(runner, '_verify_final_2c4g_with_sdk', sdk)
    runner.run_iac_code_web_2c4g_step4(SimpleNamespace(), 'iac-code-web-2c4g-step4')
    assert calls == [snapshot]
    assert h.checks['structured 2 vCPU and 4 GiB evidence'] is actual_correct
    assert all(value for name, value in h.checks.items() if name != 'structured 2 vCPU and 4 GiB evidence')


def test_dynamic_chinese_image_refuses_missing_glyphs_before_render(monkeypatch):
    runner = _load_runner()
    monkeypatch.setattr(runner, '_load_text_image_font', lambda **_: runner.ImageFont.load_default())
    with pytest.raises(ValueError, match='Chinese glyphs'):
        runner._render_text_png('请创建云网络，本轮不部署。')


def test_noecho_diagnostics_are_safe_and_do_not_change_redaction_acceptance():
    runner = _load_runner()
    secret = 'FAKE_PRIVATE_CREDENTIAL'
    template = 'ROSTemplateFormatVersion: "2015-09-01"\nParameters:\n  DbPwd:\n    Type: String\n    NoEcho: true\n'
    snapshot = {'display': {'toolResults': [
        {'toolName': 'write_file', 'isError': False, 'input': {'content': template}},
        {'toolName': 'complete_step', 'isError': False,
         'input': {'conclusion': {'deployment_parameters': {'DbPwd': secret}}}},
    ]}}
    h = SimpleNamespace(checks={'canonical snapshot contains generated credential parameters': False}, diagnostics={})
    runner._record_noecho_redaction_diagnostics(h, snapshot)
    assert h.diagnostics == {'redaction_noecho_parameter_count': 1,
                             'redaction_noecho_non_password_name_count': 1,
                             'redaction_noecho_parameter_value_count': 1}
    assert secret not in json.dumps(h.diagnostics) and 'DbPwd' not in json.dumps(h.diagnostics)
    assert h.checks['canonical snapshot contains generated credential parameters'] is False


def test_image_font_override_is_checked_before_installed_fonts(monkeypatch, tmp_path):
    runner = _load_runner()
    path = tmp_path / 'ci-font.otf'
    path.write_bytes(b'fake-font')
    monkeypatch.setenv('IAC_CODE_E2E_FONT_PATH', str(path))
    calls = []
    monkeypatch.setattr(runner.ImageFont, 'truetype', lambda name, size: calls.append((name, size)) or 'font')
    assert runner._load_text_image_font(size=34) == 'font'
    assert calls == [(str(path), 34)]


def test_rollback_second_deployment_answers_real_refreshed_selector_without_losing_new_stack_constraint() -> None:
    runner = _load_runner()
    prompts: list[str] = []
    initial_summary = runner.StreamSummary(name="initial", prompt="", pipeline_event_types=["input_required"])

    class InitialStream:
        name = "initial"
        events = [_input_required_event(step_id="confirm_and_select")]

        def wait_for(self, *_args, **_kwargs):
            raise RuntimeError("initial ended")

    class AnswerStream:
        name = "answer"
        events: list[dict] = []

        def wait_for(self, predicate, *, description: str, timeout: float):
            event = {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "step_started",
                                    "step": {"id": "deploying"},
                                    "data": {},
                                }
                            }
                        }
                    }
                }
            }
            if predicate(event, initial_summary):
                return runner.EventMatch(description=description, event=event, summary=initial_summary)
            raise TimeoutError(description)

    def start_stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "rollback-answer-confirm_and_select-1"
        return AnswerStream()

    harness = SimpleNamespace(notes=[], start_stream=start_stream)

    streams = runner._wait_for_with_intervening_ask_inputs(
        harness,
        [InitialStream()],
        runner._step_started("deploying"),
        description="confirm step",
        timeout=1,
        name_prefix="rollback",
        answer_prompt=runner.ROLLBACK_PROMPT,
        answer_input_steps={"confirm_and_select"},
        step_input_prompts={"confirm_and_select": "Select a current candidate; require a fresh CreateStack receipt"},
    )

    assert prompts == ["Select a current candidate; require a fresh CreateStack receipt"]
    assert len(streams) == 2



def test_second_stack_receipt_can_arrive_in_real_parameter_reply_not_initial_selection_stream(tmp_path):
    runner = _load_runner()
    rows = [
        _stack_current_changed_event(action='GetStack', stack_id='queried-not-created', stack_name='fake',
                                     status='CREATE_COMPLETE', is_success=True),
        _stack_current_changed_event(action='CreateStack', stack_id='first-old', stack_name='fake',
                                     status='CREATE_COMPLETE', is_success=True),
        _stack_current_changed_event(action='CreateStack', stack_id='new-native', stack_name='fake',
                                     status='CREATE_COMPLETE', is_success=True),
    ]
    (tmp_path / 'parameter-reply.events.jsonl').write_text(
        '\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    ids = runner._created_stack_ids_in_turn_files(tmp_path, {'parameter-reply'}, exclude={'first-old'})
    assert ids == ['new-native']
    assert runner._created_stack_ids_in_turn_files(tmp_path, {'unrelated'}, exclude=set()) == []


@pytest.mark.parametrize('dispatch', [False, True])
def test_backup_fixture_dispatch_handshake_is_bounded_and_keeps_window_open(tmp_path, dispatch):
    runner = _load_runner()
    control = tmp_path / 'backup-delay-handshake'
    runner._write_json(runner._backup_delay_marker_path(control, 'arm'),
                      {'awaitRequestDispatch': True, 'dispatchWaitSeconds': 2.0})
    env = os.environ.copy()
    env.update(IAC_CODE_CONFIG_DIR=str(tmp_path / 'isolated-config'),
               IAC_CODE_E2E_BACKUP_DELAY_SECONDS='0.01', IAC_CODE_E2E_BACKUP_DELAY_CONTROL=str(control))
    env['PYTHONPATH'] = os.pathsep.join(
        v for v in (str(runner.BACKUP_DELAY_FIXTURE_ROOT.resolve()), env.get('PYTHONPATH', '')) if v)
    script = '''
import json, pathlib, threading, time
from iac_code.services.session_backup import BackupReason, SessionBackupService
control = pathlib.Path(__import__('os').environ['IAC_CODE_E2E_BACKUP_DELAY_CONTROL'])
def dispatched():
    started = control.with_name(control.name + '.started.json')
    deadline = time.monotonic() + 5.0
    while not started.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('backup never started')
        time.sleep(0.005)
    time.sleep(0.1)
    assert not control.with_name(control.name + '.finished.json').exists()
    control.with_name(control.name + '.dispatched.json').write_text(
        json.dumps({'requestStartedMonotonic': time.monotonic()}), encoding='utf-8')
'''
    if dispatch:
        script += 'threading.Thread(target=dispatched).start()\n'
    script += '''
time.sleep(0.15)  # Slow pre-backup work must not consume the dispatch delay.
try:
    SessionBackupService().backup_session('', 'session-1', reason=BackupReason.INPUT_REQUIRED, critical=False)
except TimeoutError:
    print('bounded_dispatch_timeout')
'''
    result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True,
                            text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    if dispatch:
        finished = runner._wait_for_backup_delay_marker(control, 'finished', timeout=1)
        dispatched = json.loads(runner._backup_delay_marker_path(control, 'dispatched').read_text(encoding='utf-8'))
        assert finished['startedMonotonic'] < dispatched['requestStartedMonotonic'] <= finished['finishedMonotonic']
    else:
        assert 'bounded_dispatch_timeout' in result.stdout
        assert not runner._backup_delay_marker_path(control, 'finished').exists()


def test_golden_diagnostics_distinguish_source_read_from_tagged_completion():
    runner = _load_runner()
    snapshot = {'display': {'toolResults': [
        {'toolName': 'read_file', 'isError': False,
         'input': {'path': '/private/references/solutions/iac-code-web.ros.yml'}},
        {'toolName': 'complete_step', 'isError': False, 'input': {'conclusion': {'template': 'private body'}}},
        {'toolName': 'write_file', 'isError': True,
         'input': {'content': 'acs:solution:iac-code:iac-code-web'}},
    ]}}
    facts = runner._golden_solution_diagnostics(snapshot)
    assert facts == {'golden_read_path_seen': True, 'golden_read_alias_seen': False,
                     'golden_tagged_write_seen': False, 'golden_tagged_completion_seen': False,
                     'golden_bash_path_seen': False}
    assert 'private' not in json.dumps(facts)
    assert runner._golden_solution_evidenced(snapshot) is False


@pytest.mark.parametrize("remove_metadata", [False, True])
def test_golden_structure_diagnostics_distinguish_tag_loss_from_application_loss(remove_metadata):
    runner = _load_runner()
    baseline = runner.yaml.safe_load((runner.E2E_SCRIPTS_DIR.parents[2] /
        "src/iac_code/skills/bundled/iac_aliyun/references/solutions/iac-code-web.ros.yml").read_text(
            encoding="utf-8"))
    if remove_metadata:
        baseline.pop("Metadata")
    snapshot = {"display": {"toolResults": [
        {"toolName": "complete_step", "isError": False,
         "input": {"conclusion": {"candidates": [{"name": "iac-code-web-single-ecs"}]}}},
        {"toolName": "write_file", "isError": False,
         "input": {"path": "/private/path", "content": runner.yaml.safe_dump(baseline)}},
    ]}}
    facts = runner._golden_template_structure_diagnostics(snapshot)
    assert facts["golden_candidate_name_seen"] is True
    assert facts["golden_generated_template_parsed"] is True
    assert facts["golden_generated_metadata_tag_seen"] is not remove_metadata
    assert facts["golden_generated_resource_types_match"] is True
    assert facts["golden_generated_bootstrap_matches"] is True
    assert facts["golden_generated_bootstrap_script_matches"] is True
    assert facts["golden_generated_bootstrap_bindings_match"] is True
    assert facts["golden_generated_outputs_match"] is True
    assert all(isinstance(value, bool) for value in facts.values())
    assert "private" not in json.dumps(facts)
    # Diagnostics cannot make an unread or untagged solution pass acceptance.
    assert runner._golden_solution_evidenced(snapshot) is False


@pytest.mark.parametrize("change", ["dependency_order", "command", "bindings", "timeout"])
def test_golden_bootstrap_diagnostics_separate_order_from_actual_content_changes(change):
    runner = _load_runner()
    template = runner.yaml.safe_load((runner.E2E_SCRIPTS_DIR.parents[2] /
        "src/iac_code/skills/bundled/iac_aliyun/references/solutions/iac-code-web.ros.yml").read_text(
            encoding="utf-8"))
    bootstrap = template["Resources"]["Bootstrap"]
    if change == "dependency_order":
        bootstrap["DependsOn"].reverse()
    elif change == "timeout":
        bootstrap["Properties"]["Timeout"] = 1200
    elif change == "command":
        bootstrap["Properties"]["CommandContent"]["Fn::Sub"][0] = "private modified command"
    else:
        bootstrap["Properties"]["CommandContent"]["Fn::Sub"][1]["LocalInstanceType"] = "private value"
    snapshot = {"display": {"toolResults": [
        {"toolName": "complete_step", "isError": False,
         "input": {"conclusion": {"template": runner.yaml.safe_dump(template)}}},
    ]}}
    facts = runner._golden_template_structure_diagnostics(snapshot)
    assert facts["golden_generated_bootstrap_matches"] is False
    assert facts["golden_generated_bootstrap_dependencies_match"] is True
    assert facts["golden_generated_bootstrap_type_matches"] is True
    assert facts["golden_generated_bootstrap_properties_match"] is (change != "timeout")
    assert facts["golden_generated_bootstrap_script_matches"] is (change != "command")
    assert facts["golden_generated_bootstrap_bindings_match"] is (change != "bindings")
    assert "private" not in json.dumps(facts)


def test_golden_structure_diagnostics_use_latest_native_template_not_stale_or_failed_writes():
    runner = _load_runner()
    baseline = (runner.E2E_SCRIPTS_DIR.parents[2] /
        "src/iac_code/skills/bundled/iac_aliyun/references/solutions/iac-code-web.ros.yml").read_text(
            encoding="utf-8")
    snapshot = {"display": {"toolResults": [
        {"sequence": 1, "toolName": "write_file", "isError": False, "input": {"content": baseline}},
        {"sequence": 2, "toolName": "complete_step", "isError": False,
         "input": {"conclusion": {"template": "Resources: {}\nDescription: private-secret"}}},
        {"sequence": 3, "toolName": "write_file", "isError": True, "input": {"content": baseline}},
    ]}}
    facts = runner._golden_template_structure_diagnostics(snapshot)
    assert facts["golden_generated_template_parsed"] is True
    assert facts["golden_generated_metadata_tag_seen"] is False
    assert facts["golden_generated_resource_types_match"] is False
    assert facts["golden_generated_bootstrap_matches"] is False
    assert facts["golden_generated_outputs_match"] is False
    assert "private" not in json.dumps(facts)


def test_final_target_diagnostic_describes_realized_evidence_without_accepting_intent():
    runner = _load_runner()
    context = {'selected_plan': {
        'deployment_parameters': {'VpcId': 'private-vpc'},
        'selected_candidate_result': {'template': 'private-template-body', 'private-field': 'private-secret'},
        'private-field': 'private-secret',
    }, 'intent': {'products': ['SecurityGroup']}}
    state = {'snapshot': {'steps': [], 'normalHandoff': {
        'summary': 'Included context:\n' + json.dumps(context),
    }}}
    harness = SimpleNamespace(diagnostics={})
    runner._record_final_target_diagnostics(harness, state)
    assert harness.diagnostics['final_target_context_present'] is True
    assert harness.diagnostics['final_target_selected_plan_fields'] == [
        'deployment_parameters', 'selected_candidate_result']
    assert harness.diagnostics['final_target_candidate_result_fields'] == ['template']
    assert harness.diagnostics['final_target_template_body_present'] is True
    assert harness.diagnostics['final_target_handoff_security_group'] is False
    assert 'SecurityGroup' not in runner._final_deployment_evidence(state)
    assert 'private' not in json.dumps(harness.diagnostics)


@pytest.mark.parametrize('create_vswitch', [False, True])
def test_final_target_diagnostic_distinguishes_template_resources_from_vswitch_metadata(create_vswitch):
    runner = _load_runner()
    resources = {'private-sg': {'Type': 'ALIYUN::ECS::SecurityGroup'}}
    if create_vswitch:
        resources['private-switch'] = {'Type': 'ALIYUN::ECS::VSwitch'}
    template = {'Description': 'Do not create a VSwitch private-secret', 'Resources': resources}
    context = {'selected_plan': {'selected_candidate_result': {
        'template': {'template': json.dumps(template)},
        'cost': {'resources': [{'name': 'VSwitch excluded private-value'}]},
    }}}
    state = {'snapshot': {'steps': [], 'normalHandoff': {
        'summary': 'Included context:\n' + json.dumps(context),
    }}}
    harness = SimpleNamespace(diagnostics={})
    runner._record_final_target_diagnostics(harness, state)
    assert harness.diagnostics['final_target_template_resources_inspected'] is True
    assert harness.diagnostics['final_target_template_security_group'] is True
    assert harness.diagnostics['final_target_template_vswitch'] is create_vswitch
    # Diagnostics do not alter the original target acceptance condition.
    assert runner._has_any_marker(runner._final_deployment_evidence(state), runner.VSWITCH_MARKERS)
    assert 'private' not in json.dumps(harness.diagnostics)
    harness.checks = {}
    runner._check_final_target_resource_types(harness)
    assert harness.checks['final deploying target resources inspected'] is True
    assert harness.checks['final deploying target is security group'] is True
    assert harness.checks['final deploying target is not VSwitch'] is (not create_vswitch)


@pytest.mark.parametrize(('inspected', 'groups', 'switches', 'expected_group', 'expected_no_switch'), [
    (True, 1, 0, True, True), (True, 1, 1, True, False), (True, 0, 0, False, True),
    (False, 1, 0, False, False),
])
def test_live_final_target_requires_owned_native_types_not_descriptions(
    inspected, groups, switches, expected_group, expected_no_switch,
):
    runner = _load_runner()
    harness = SimpleNamespace(args=SimpleNamespace(allow_real_cloud=True), checks={}, diagnostics={
        'final_target_native_resources_inspected': inspected, 'final_target_native_security_group_count': groups,
        'final_target_native_vswitch_count': switches,
        'final_target_security_group': True, 'final_target_vswitch': True,
        'final_target_template_resources_inspected': True, 'final_target_template_security_group': True,
        'final_target_template_vswitch': False,
    })
    runner._check_final_target_resource_types(harness)
    assert harness.checks['final deploying target resources inspected'] is inspected
    assert harness.checks['final deploying target is security group'] is expected_group
    assert harness.checks['final deploying target is not VSwitch'] is expected_no_switch


@pytest.mark.parametrize('use_tool_confirmation', [False, True])
def test_real_product_handoff_context_survives_appended_safety_and_missing_field_sections(
    monkeypatch, use_tool_confirmation,
):
    from iac_code.pipeline.engine.handoff import build_handoff_summary

    runner = _load_runner()
    monkeypatch.setenv('IAC_CODE_HANDOFF_USE_TOOL_CONFIRMATION', str(int(use_tool_confirmation)))
    context = {
        'selected_plan': {'resource_types': ['ALIYUN::ECS::SecurityGroup']},
        'deployment': {'status': 'failed'},
        'intent': {'products': ['VSwitch']},
    }
    summary = build_handoff_summary('selling', 'completed', context, [*context, 'missing_field'])
    state = {'snapshot': {'steps': [], 'normalHandoff': {'summary': summary}}}
    assert 'Safety requirements for normal chat:' in summary
    assert 'Missing context fields:' in summary
    # This reproduces the old parser's exact cut: product safety prose remains
    # after the JSON, so json.loads cannot recover the realized selected plan.
    raw = summary.split('Included context:\n', 1)[1].split('\n\nUse this context', 1)[0]
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert runner._handoff_context(state) == context
    evidence = runner._final_deployment_evidence(state)
    assert 'SecurityGroup' in evidence and 'VSwitch' not in evidence


@pytest.mark.parametrize('included', ['not JSON', '[]', '"SecurityGroup"', '{"private":'])
def test_handoff_context_parser_does_not_use_prose_or_non_object_as_target(included):
    runner = _load_runner()
    state = {'snapshot': {'normalHandoff': {'summary':
        'Included context:\n' + included + '\n\nSecurityGroup Safety requirements for normal chat:'}}}
    assert runner._handoff_context(state) is None
    assert 'SecurityGroup' not in runner._final_deployment_evidence(state)
@pytest.mark.parametrize(('resources', 'inspected', 'switches'), [
    ([{'ResourceType': 'ALIYUN::ECS::SecurityGroup', 'PhysicalResourceId': 'private-id',
       'StatusReason': 'VSwitch excluded private-secret'}], True, 0),
    ([{'ResourceType': 'ALIYUN::ECS::SecurityGroup'}, {'ResourceType': 'ALIYUN::ECS::VSwitch'}], True, 1),
    ([{'PhysicalResourceId': 'private-id'}], False, None),
    (None, False, None),
])
def test_native_final_target_reads_exact_receipt_owned_resource_types(monkeypatch, resources, inspected, switches):
    from iac_code.services import cloud_credentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

    runner = _load_runner()
    client = MagicMock()
    client.get_stack_with_options.return_value.body.to_map.return_value = {'Status': 'CREATE_COMPLETE'}
    client.list_stack_resources_with_options.return_value.body.to_map.return_value = {'Resources': resources}
    credential = object()
    monkeypatch.setattr(
        cloud_credentials, 'CloudCredentials', lambda: SimpleNamespace(get_provider=lambda _: credential))

    def create(received, region):
        assert received is credential and region == 'cn-hangzhou'
        return client

    monkeypatch.setattr(RosClientFactory, 'create', create)
    receipt = {'provider': 'ros', 'resource_type': 'stack', 'observed_action': 'CreateStack',
               'resource_id': 'private-stack', 'region_id': 'cn-hangzhou'}
    monkeypatch.setattr(runner, '_cleanup_ledger_items', lambda *_: [receipt])
    response = {'snapshot': {'stacks': {'current': {'stackId': 'private-stack', 'status': 'CREATE_COMPLETE'}}}}

    facts = runner._native_final_target_resource_facts(SimpleNamespace(), response)

    assert facts['final_target_native_resources_inspected'] is inspected
    assert facts.get('final_target_native_vswitch_count') == switches
    if inspected:
        assert facts['final_target_native_security_group_count'] == 1
    for call in (client.get_stack_with_options.call_args, client.list_stack_resources_with_options.call_args):
        request, options = call.args
        assert request.stack_id == 'private-stack' and request.region_id == 'cn-hangzhou'
        assert options.connect_timeout == 5000 and options.read_timeout == 10000 and options.autoretry is False
    assert 'private' not in json.dumps(facts)


def test_native_final_target_never_queries_unowned_current_stack(monkeypatch):
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

    runner = _load_runner()
    monkeypatch.setattr(runner, '_cleanup_ledger_items', lambda *_: [{
        'provider': 'ros', 'resource_type': 'stack', 'observed_action': 'CreateStack',
        'resource_id': 'private-owned-stack', 'region_id': 'cn-hangzhou',
    }])
    monkeypatch.setattr(RosClientFactory, 'create', lambda *_: pytest.fail('must not query unknown stack'))
    response = {'snapshot': {'stacks': {'current': {'stackId': 'private-unowned-stack', 'status': 'CREATE_COMPLETE'}}}}
    assert runner._native_final_target_resource_facts(SimpleNamespace(), response) == {
        'final_target_native_resources_inspected': False, 'final_target_native_probe_category': 'receipt_unavailable',
    }
