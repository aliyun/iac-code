from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest
import yaml


def _runner_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "pipeline" / "e2e" / "selling_solution_first" / "run_scenarios.py"
    spec = importlib.util.spec_from_file_location("selling_solution_first_real_e2e", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _runner_module()


def test_registry_has_exact_documented_45_cases(runner: ModuleType) -> None:
    assert len(runner.SCENARIOS) == 45
    assert len(runner.SCENARIO_BY_NAME) == 45
    assert [item.case_id for item in runner.SCENARIOS] == [
        *(f"A{index:02d}" for index in range(1, 28)),
        *(f"R{index:02d}" for index in range(1, 15)),
        "W01",
        "W02",
        "D01",
        "L01",
    ]
    assert sum(item.surface is runner.Surface.A2A for item in runner.SCENARIOS) == 27
    assert sum(item.surface is runner.Surface.REPL for item in runner.SCENARIOS) == 14
    assert sum(item.surface is runner.Surface.WEB for item in runner.SCENARIOS) == 2
    assert sum(item.surface is runner.Surface.DESKTOP for item in runner.SCENARIOS) == 1
    assert sum(item.surface is runner.Surface.LEGACY for item in runner.SCENARIOS) == 1


@pytest.mark.parametrize(
    ("suite", "expected_ids"),
    [
        ("smoke", ["A01", "R01", "W01"]),
        ("core", [*(f"A{i:02d}" for i in range(1, 9)), *(f"R{i:02d}" for i in range(1, 7))]),
        ("recovery", [*(f"A{i:02d}" for i in range(9, 24)), *(f"R{i:02d}" for i in range(7, 14))]),
        ("multimodal", ["A25", "A26", "A27", "R14", "W02"]),
        ("safety", ["A02", "A10", "A11", "A18", "A22", "A23", "A24", "D01", "L01"]),
        ("web", ["W01", "W02"]),
        ("desktop", ["D01"]),
        ("legacy", ["L01"]),
        ("all", []),
    ],
)
def test_suite_membership_matches_design(runner: ModuleType, suite: str, expected_ids: list[str]) -> None:
    if suite == "all":
        expected_ids = [item.case_id for item in runner.SCENARIOS]
    assert [item.case_id for item in runner.scenarios_for_suite(suite)] == expected_ids


def test_selection_deduplicates_and_keeps_registry_order(runner: ModuleType) -> None:
    selected = runner.select_scenarios(["web-full-flow", "a2a-happy-multi-plan"], ["smoke", "web"])
    assert [item.case_id for item in selected] == ["A01", "R01", "W01", "W02"]


def test_parser_defaults_to_concurrency_three_and_smoke(runner: ModuleType) -> None:
    args = runner.parse_args([])
    assert args.concurrency == 3
    assert [item.case_id for item in runner.select_scenarios(args.scenario, args.suite)] == ["A01", "R01", "W01"]
    with pytest.raises(SystemExit):
        runner.parse_args(["--concurrency", "0"])


def test_run_dir_and_cloud_write_validation(runner: ModuleType, tmp_path: Path) -> None:
    args = runner.parse_args(
        [
            "--scenario",
            "a2a-happy-multi-plan",
            "--run-dir",
            str(tmp_path),
            "--allow-real-cloud",
        ]
    )
    selected = runner.select_scenarios(args.scenario, args.suite)
    with pytest.raises(ValueError, match="--run-dir"):
        runner.validate_args(args, selected)
    args.concurrency = 1
    with pytest.raises(ValueError, match="--allow-cloud-write"):
        runner.validate_args(args, selected)
    args.allow_cloud_write = True
    runner.validate_args(args, selected)


def test_credentials_are_copied_with_safe_modes_and_source_is_unchanged(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "case" / "config"
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text(f"fake-{name}\n", encoding="utf-8")
    (source / "settings.yml").write_text("provider: fake\n", encoding="utf-8")
    before = runner.snapshot_credentials(source)
    audit = runner.copy_credentials(source, destination, inherit_settings=True)
    after = runner.snapshot_credentials(source)

    assert audit.credential_files_copied
    assert audit.settings_copied
    assert audit.directory_mode_ok
    assert audit.file_modes_ok
    assert audit.independent_files
    assert runner.credential_snapshot_unchanged(before, after)
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for name in (*runner.CREDENTIAL_FILES, "settings.yml"):
        target = destination / name
        assert not target.is_symlink()
        if os.name != "nt":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_text(encoding="utf-8") == (source / name).read_text(encoding="utf-8")


def test_credentials_reject_symlink_source(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    real = tmp_path / "credential"
    real.write_text("fake", encoding="utf-8")
    (source / runner.CREDENTIAL_FILES[0]).symlink_to(real)
    (source / runner.CREDENTIAL_FILES[1]).write_text("fake", encoding="utf-8")
    with pytest.raises(ValueError, match="non-symlink"):
        runner.copy_credentials(source, tmp_path / "config", inherit_settings=False)


def test_public_noecho_parameter_values_must_be_redacted(runner: ModuleType, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    template = workspace / "templates" / "database.yml"
    template.parent.mkdir(parents=True)
    template.write_text(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  MasterUserPassword:
    Type: String
    NoEcho: true
Resources: {}
""",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(paths=argparse.Namespace(workspace_dir=workspace, run_dir=tmp_path))

    assert runner._public_noecho_values_are_redacted(
        runtime,
        [{"parameter_name": "MasterUserPassword", "actual_value": "<redacted>"}],
    )
    assert not runner._public_noecho_values_are_redacted(
        runtime,
        [{"parameter_name": "MasterUserPassword", "actual_value": "Fake-test-password-9!"}],
    )


def test_repl_cloud_discovery_reads_persisted_tool_transcript(runner: ModuleType, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    transcript = (
        config_dir
        / "projects"
        / "project"
        / "session"
        / "pipeline"
        / "transcripts"
        / "transcript_att_0001"
        / "session.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    owned_name = "iac-e2e-ssf-repl-single-plan-happy-abc12345"
    transcript.write_text(
        json.dumps(
            {
                "tool_result": {
                    "stack_id": "test-stack-id-123456",
                    "stack_name": owned_name,
                    "region_id": "cn-hangzhou",
                }
            }
        )
        + "\n"
        + json.dumps(
            {
                "tool_result": {
                    "stack_id": "unowned-stack-id-123456",
                    "stack_name": "somebody-elses-stack",
                    "region_id": "cn-hangzhou",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(
        paths=argparse.Namespace(
            run_dir=tmp_path,
            config_dir=config_dir,
            artifacts_dir=tmp_path / "artifacts",
        ),
        owned_stack_names={owned_name},
        cloud_resources=[],
    )

    assert runner.discover_cloud_resources(runtime) == [
        {
            "provider": "ros",
            "resourceType": "stack",
            "stackId": "test-stack-id-123456",
            "stackName": owned_name,
            "regionId": "cn-hangzhou",
            "createdByCase": "false",
        }
    ]
    assert json.loads((tmp_path / "cloud-resources.json").read_text(encoding="utf-8")) == runtime.cloud_resources


def test_runtime_defaults_follow_real_settings_shape(runner: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "settings.yml").write_text(
        "activeProvider: openai_compatible\n"
        "providers:\n"
        "  openai_compatible:\n"
        "    model: test-model\n"
        "    apiBase: https://example.invalid/v1\n",
        encoding="utf-8",
    )
    assert runner.read_runtime_defaults(tmp_path) == {
        "provider": "openai_compatible",
        "model": "test-model",
        "api_base": "https://example.invalid/v1",
    }


def test_step1_clarification_answer_supplies_the_missing_product_intent(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=runner.SCENARIO_BY_NAME["a2a-step1-clarify"],
        cidr="10.250.0.0/24",
        args=argparse.Namespace(cleanup_vpc_id="", cleanup_zone_id=""),
    )

    plan = runner._a2a_plan(runtime)

    assert len(plan.ask_answers) == 1
    assert "Node.js 电商后端 API" in plan.ask_answers[0]
    assert "cn-hangzhou" in plan.ask_answers[0]


def test_a2a_multimodal_plan_uses_distinct_images_then_plain_text(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=runner.SCENARIO_BY_NAME["a2a-image-asks-confirmation"],
        cidr="10.250.0.0/24",
        args=argparse.Namespace(cleanup_vpc_id="", cleanup_zone_id=""),
    )
    plan = runner._a2a_plan(runtime)

    first_ask = runner._a2a_response_for_pending(runtime, "ask_user_question", plan)
    second_ask = runner._a2a_response_for_pending(runtime, "ask_user_question", plan)
    first_confirmation = runner._a2a_response_for_pending(runtime, "deployment_confirmation", plan)
    second_confirmation = runner._a2a_response_for_pending(runtime, "deployment_confirmation", plan)

    assert first_ask[1] == "ask-first-answer"
    assert second_ask[1] == "ask-second-answer"
    assert first_confirmation[1] == "confirmation-adjust"
    assert second_confirmation[1] == ""
    assert "调整参数" in first_confirmation[0]
    assert json.loads(second_confirmation[0])["action"] == "cancel"


def test_a2a_image_interrupt_only_uses_rollback_image_once(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=runner.SCENARIO_BY_NAME["a2a-image-interrupt-handoff"],
        cidr="10.250.0.0/24",
        args=argparse.Namespace(cleanup_vpc_id="", cleanup_zone_id=""),
    )
    plan = runner._a2a_plan(runtime)

    first_confirmation = runner._a2a_response_for_pending(runtime, "deployment_confirmation", plan)
    second_confirmation = runner._a2a_response_for_pending(runtime, "deployment_confirmation", plan)

    assert first_confirmation[1] == "rollback-interrupt"
    assert second_confirmation[1] == ""
    assert json.loads(second_confirmation[0])["action"] == "confirm"


def test_backup_window_reads_pending_input_from_prepublication_snapshot(runner: ModuleType) -> None:
    state = {
        "snapshot": {
            "status": "waiting_input",
            "pendingInput": {
                "kind": "ask_user_question",
                "step": {"id": runner.NEW_STEPS[1]},
                "options": [
                    {"id": "use-default", "label": "使用默认值"},
                    {"id": "vpc-unit123", "label": "测试 VPC"},
                ],
            },
        }
    }

    step_id, kind, pending = runner._pending_from_pipeline_state(state)

    assert step_id == runner.NEW_STEPS[1]
    assert kind == "ask_user_question"
    assert runner._first_pending_resource_option_id_from_data(pending) == "vpc-unit123"


def test_backup_window_normalizes_candidate_select_snapshot_kind(runner: ModuleType) -> None:
    state = {
        "snapshot": {
            "pendingInput": {
                "kind": "candidate_select",
                "step": {"id": runner.NEW_STEPS[0]},
            }
        }
    }

    assert runner._pending_from_pipeline_state(state)[:2] == (runner.NEW_STEPS[0], "candidate_selection")


def test_backup_window_next_pending_must_follow_consumed_sequence(runner: ModuleType) -> None:
    class FakeA2A:
        @staticmethod
        def _extract_pipeline_envelopes(event: object) -> list[dict[str, object]]:
            assert isinstance(event, dict)
            return event["envelopes"]  # type: ignore[return-value]

    replayed = {"envelopes": [{"eventType": "input_required", "sequence": 10}]}
    advanced = {"envelopes": [{"eventType": "input_required", "sequence": 12}]}

    predicate = runner._input_required_after_sequence(FakeA2A(), 11)

    assert predicate(replayed, None) is False
    assert predicate(advanced, None) is True


def test_backup_window_pending_input_must_follow_sequence_and_match_identity(runner: ModuleType) -> None:
    class FakeA2A:
        @staticmethod
        def _extract_pipeline_envelopes(event: object) -> list[dict[str, object]]:
            assert isinstance(event, dict)
            return event["envelopes"]  # type: ignore[return-value]

    predicate = runner._input_required_after_sequence_kind_and_step(
        FakeA2A(),
        10,
        runner.NEW_STEPS[0],
        "candidate_selection",
    )
    prior_ask = {
        "envelopes": [
            {
                "eventType": "input_required",
                "sequence": 9,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "ask_user_question"},
            }
        ]
    }
    candidate = {
        "envelopes": [
            {
                "eventType": "input_required",
                "sequence": 11,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection"},
            }
        ]
    }

    assert predicate(prior_ask, None) is False
    assert predicate(candidate, None) is True


def test_backup_window_consumed_input_must_follow_pending_sequence_and_match_identity(runner: ModuleType) -> None:
    class FakeA2A:
        @staticmethod
        def _extract_pipeline_envelopes(event: object) -> list[dict[str, object]]:
            assert isinstance(event, dict)
            return event["envelopes"]  # type: ignore[return-value]

    predicate = runner._input_received_after_sequence_kind_and_step(
        FakeA2A(),
        20,
        runner.NEW_STEPS[0],
        "candidate_selection",
    )
    replayed = {
        "envelopes": [
            {
                "eventType": "input_received",
                "sequence": 19,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection"},
            }
        ]
    }
    wrong_kind = {
        "envelopes": [
            {
                "eventType": "input_received",
                "sequence": 21,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "ask_user_question"},
            }
        ]
    }
    consumed = {
        "envelopes": [
            {
                "eventType": "input_received",
                "sequence": 21,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection"},
            }
        ]
    }

    assert predicate(replayed, None) is False
    assert predicate(wrong_kind, None) is False
    assert predicate(consumed, None) is True


def test_backup_delay_uses_artifact_directory_for_multiple_windows(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    runtime = argparse.Namespace(paths=argparse.Namespace(artifacts_dir=tmp_path / "artifacts"))
    runtime.paths.artifacts_dir.mkdir()
    harness = argparse.Namespace(server_env={})
    a2a = argparse.Namespace(BACKUP_DELAY_FIXTURE_ROOT=tmp_path, BACKUP_DELAY_SECONDS=0.01)

    first = runner._arm_a2a_backup_delay(runtime, harness, a2a, 1)
    second = runner._arm_a2a_backup_delay(runtime, harness, a2a, 2)

    assert harness.server_env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] == str(runtime.paths.artifacts_dir)
    assert runner._backup_delay_marker(first, "arm").is_file()
    assert runner._backup_delay_marker(second, "arm").is_file()


@pytest.mark.parametrize("state", ["TASK_STATE_FAILED", "TASK_STATE_CANCELED"])
def test_unexpected_a2a_terminal_state_fails_immediately(runner: ModuleType, state: str) -> None:
    summary = argparse.Namespace(last_status_state=state, text="pipeline_identity_mismatch")

    with pytest.raises(RuntimeError, match=f"{state}.*pipeline_identity_mismatch"):
        runner._raise_for_unexpected_a2a_terminal(summary)


def test_repl_waits_for_initial_prompt_before_sending_scenario_input(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    class FakePty:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def spawn(self) -> None:
            calls.append("spawn")

        def terminate(self) -> None:
            calls.append("terminate")

    fake_repl = argparse.Namespace(
        ReplPty=FakePty,
        _expect_initial_prompt=lambda _pty, _args: calls.append("ready"),
    )
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=1.0),
        env={},
        paths=argparse.Namespace(run_dir=tmp_path, workspace_dir=tmp_path),
        spec=argparse.Namespace(profile="happy_single"),
        event=lambda *_args, **_kwargs: None,
    )

    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: fake_repl)
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())
    monkeypatch.setattr(runner, "_repl_basic_flow", lambda _runtime, _pty: calls.append("scenario"))
    monkeypatch.setattr(runner, "_repl_wait_pipeline_completed", lambda _pty, _runtime: calls.append("terminal"))
    monkeypatch.setattr(runner, "_write_repl_artifacts", lambda _runtime, _pty, _repl: calls.append("artifacts"))

    runner._run_repl(runtime)

    assert calls == ["spawn", "ready", "scenario", "terminal", "terminate", "artifacts"]


def test_rollback_recovery_restates_the_case_owned_stack_name(runner: ModuleType) -> None:
    runtime = argparse.Namespace(stack_name="iac-e2e-ssf-owned-1234")

    prompt = runner._rollback_new_intent(runtime)

    assert "最终 ROS StackName 仍必须使用 iac-e2e-ssf-owned-1234" in prompt


def test_walk_exposes_event_dicts_nested_directly_in_arrays(runner: ModuleType) -> None:
    event = {"batch": [{"eventType": "step_started", "step": {"id": runner.NEW_STEPS[1]}}]}

    assert runner._started_steps([event]) == [(0, runner.NEW_STEPS[1])]


def test_web_state_wait_reads_hydrated_status_endpoint(runner: ModuleType) -> None:
    requested_paths: list[str] = []

    class FakeWeb:
        @staticmethod
        def _session_path(session_id: str, suffix: str = "") -> str:
            return f"/api/sessions/{session_id}{suffix}"

        @staticmethod
        def _json_request(_base_url: str, _method: str, path: str) -> dict[str, object]:
            requested_paths.append(path)
            return {
                "status": "waiting_input",
                "pipeline": {"pendingInput": {"kind": "candidate_selection"}},
            }

    state = runner._wait_web_state(
        FakeWeb,
        "http://127.0.0.1:1",
        "web-session",
        lambda value: runner._web_pending_kind(value) == "candidate_selection",
        0.1,
    )

    assert runner._web_pending_kind(state) == "candidate_selection"
    assert requested_paths == ["/api/sessions/web-session/status"]


def test_web_state_wait_stops_immediately_on_pipeline_failure(runner: ModuleType) -> None:
    calls = 0

    class FakeWeb:
        @staticmethod
        def _session_path(session_id: str, suffix: str = "") -> str:
            return f"/api/sessions/{session_id}{suffix}"

        @staticmethod
        def _json_request(_base_url: str, _method: str, _path: str) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "status": "idle",
                "pipeline": {
                    "snapshot": {
                        "status": "failed",
                        "normalHandoff": {
                            "status": "failed",
                            "outcome": "failed",
                            "action": "switch_to_normal",
                        },
                    }
                },
            }

    with pytest.raises(RuntimeError, match="terminal status 'failed'"):
        runner._wait_web_state(FakeWeb, "http://127.0.0.1:1", "web-session", lambda _value: False, 1800)

    assert calls == 1


def test_web_idle_waits_for_recovery_to_release_running_turn(runner: ModuleType) -> None:
    states = iter(
        [
            {
                "status": "running",
                "pipeline": {"pendingInput": {"kind": "deployment_confirmation"}},
            },
            {
                "status": "waiting_input",
                "pipeline": {"pendingInput": {"kind": "deployment_confirmation"}},
            },
        ]
    )

    class FakeWeb:
        @staticmethod
        def _session_path(session_id: str, suffix: str = "") -> str:
            return f"/api/sessions/{session_id}{suffix}"

        @staticmethod
        def _json_request(_base_url: str, _method: str, _path: str) -> dict[str, object]:
            return next(states)

    state = runner._wait_web_idle(FakeWeb, "http://127.0.0.1:1", "web-session", 1.0)

    assert state["status"] == "waiting_input"
    assert runner._web_pending_kind(state) == "deployment_confirmation"


def test_web_confirmation_boundary_accepts_repeated_parameter_questions(runner: ModuleType) -> None:
    for kind in ("ask_user_question", "deployment_confirmation"):
        assert runner._web_at_confirmation_boundary(
            {"pipeline": {"snapshot": {"pendingInput": {"kind": kind}}}}
        )

    assert not runner._web_at_confirmation_boundary(
        {"pipeline": {"snapshot": {"pendingInput": {"kind": "candidate_selection"}}}}
    )


def test_web_materialize_boundary_fails_fast_on_unexpected_rollback(runner: ModuleType) -> None:
    for kind in ("ask_user_question", "deployment_confirmation", "candidate_selection", "candidate_select"):
        assert runner._web_at_materialize_boundary(
            {"pipeline": {"snapshot": {"pendingInput": {"kind": kind}}}}
        )


def test_w02_parameter_answer_preserves_create_goal(runner: ModuleType) -> None:
    state = {
        "pipeline": {
            "waitingInput": {
                "kind": "ask_user_question",
                "data": {
                    "options": [
                        {"id": "use-existing-vswitch", "label": "直接使用已有交换机"},
                        {"id": "create-new-vswitch", "label": "改用不重叠网段新建交换机"},
                    ]
                },
            }
        }
    }

    answer = runner._web_w02_ask_answer(state)

    assert "create-new-vswitch" in answer
    assert "保持当前已选方案和部署目标不变" in answer
    assert "use-existing-vswitch" not in answer


def test_w02_parameter_answer_uses_exact_resource_option(runner: ModuleType) -> None:
    state = {
        "pipeline": {
            "snapshot": {
                "pendingInput": {
                    "kind": "ask_user_question",
                    "options": [
                        {"id": "vpc-unit123", "label": "测试 VPC"},
                        {"id": "vpc-unit456", "label": "备用 VPC"},
                    ],
                }
            }
        }
    }

    assert "vpc-unit123" in runner._web_w02_ask_answer(state)


def test_legacy_smoke_cancels_at_candidate_selection_without_selecting(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        checks={},
        cidr="10.250.0.0/24",
        spec=argparse.Namespace(profile="legacy_smoke", cloud_write=False),
    )
    runtime.paths.artifacts_dir.mkdir()

    class FakeHarness:
        context_id = "ctx-legacy"
        pipeline_task_id = "task-legacy"

        def __init__(self) -> None:
            self.canceled: list[str] = []

        def cancel_pipeline_task(self, name: str) -> dict[str, object]:
            self.canceled.append(name)
            return {"result": {"state": "canceled"}}

    class FakeA2A:
        @staticmethod
        def _latest_pending_kind(_path: Path) -> str:
            return "candidate_selection"

    harness = FakeHarness()
    monkeypatch.setattr(runner, "_initial_prompt", lambda _runtime: "legacy prompt")
    monkeypatch.setattr(
        runner,
        "_a2a_turn",
        lambda _runtime, _harness, **_kwargs: argparse.Namespace(name="legacy-initial"),
    )

    runner._run_a2a_legacy_smoke(runtime, harness, FakeA2A())

    assert harness.canceled == ["legacy-smoke-cancel-at-candidate-selection"]
    assert runtime.checks["legacy canceled at candidate selection"] is True
    assert json.loads((runtime.paths.artifacts_dir / "waiting-sequence.json").read_text(encoding="utf-8")) == [
        "candidate_selection"
    ]


def test_legacy_smoke_answers_clarification_before_canceling_at_candidate_selection(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        checks={},
        cidr="10.250.0.0/24",
        spec=argparse.Namespace(profile="legacy_smoke", cloud_write=False),
    )
    runtime.paths.artifacts_dir.mkdir()

    class FakeHarness:
        context_id = "ctx-legacy"
        pipeline_task_id = "task-legacy"

        def cancel_pipeline_task(self, _name: str) -> dict[str, object]:
            return {"result": {"state": "canceled"}}

    summaries = iter(
        [
            argparse.Namespace(name="legacy-initial", last_input_required_step_id="intent_parsing"),
            argparse.Namespace(name="legacy-candidate", last_input_required_step_id="confirm_and_select"),
        ]
    )
    prompts: list[str] = []

    def turn(_runtime, _harness, *, prompt: str, **_kwargs):
        prompts.append(prompt)
        return next(summaries)

    class FakeA2A:
        @staticmethod
        def _latest_pending_kind(path: Path) -> str:
            return "ask_user_question" if "legacy-initial" in path.name else "candidate_selection"

    monkeypatch.setattr(runner, "_initial_prompt", lambda _runtime: "legacy prompt")
    monkeypatch.setattr(runner, "_a2a_turn", turn)

    runner._run_a2a_legacy_smoke(runtime, FakeHarness(), FakeA2A())

    assert prompts[0] == "legacy prompt"
    assert "cn-hangzhou" in prompts[1]
    assert json.loads((runtime.paths.artifacts_dir / "waiting-sequence.json").read_text(encoding="utf-8")) == [
        "intent_parsing:ask_user_question",
        "confirm_and_select:candidate_selection",
    ]


def test_web_replacement_intent_does_not_prematurely_request_cancel(runner: ModuleType) -> None:
    for multimodal in (False, True):
        prompt = runner._web_replacement_intent_prompt(multimodal=multimodal)
        assert "新" in prompt or "改需求" in prompt
        assert "替换" in prompt or "不再创建" in prompt
        assert "取消" not in prompt
        assert "不部署" not in prompt


def test_web_candidate_selection_uses_long_action_timeout(runner: ModuleType) -> None:
    calls: list[tuple[str, str, str, object, float]] = []

    class FakeWeb:
        @staticmethod
        def _json_request(
            base_url: str,
            method: str,
            path: str,
            payload: object,
            *,
            timeout: float,
        ) -> dict[str, bool]:
            calls.append((base_url, method, path, payload, timeout))
            return {"accepted": True}

    result = runner._select_web_candidate(
        FakeWeb,
        "http://127.0.0.1:1",
        "model-session",
        timeout=123.0,
    )

    assert result == {"accepted": True}
    assert calls == [
        (
            "http://127.0.0.1:1",
            "POST",
            "/api/pipeline/candidates/select",
            {"sessionId": "model-session", "candidateIndex": 0, "parameterOverrides": {}},
            123.0,
        )
    ]


def test_web_session_uses_valid_unattended_permission_mode(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        paths=argparse.Namespace(workspace_dir=tmp_path / "workspace"),
        env={"IAC_CODE_PROVIDER": "dashscope", "IAC_CODE_MODEL": "test-model"},
    )

    payload = runner._web_session_create_payload(runtime)

    assert payload["permissionMode"] == "bypass_permissions"
    assert payload["pipelineName"] == runner.PIPELINE_NAME
    assert payload["mode"] == "pipeline"


def test_browser_dependency_preflight_reports_missing_node(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: None)

    result = runner._run_browser_dependency_preflight(timeout=1.0)

    assert result == {"ok": False, "reason": "Node.js is unavailable"}


def test_browser_dependency_preflight_accepts_playwright_probe(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/test/node")
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> argparse.Namespace:
        observed["command"] = command
        observed.update(kwargs)
        return argparse.Namespace(returncode=0, stdout="PLAYWRIGHT_CORE_OK\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner._run_browser_dependency_preflight(timeout=7.0)

    assert result == {"ok": True, "reason": "PLAYWRIGHT_CORE_OK"}
    assert observed["command"][0] == "/test/node"
    assert observed["timeout"] == 7.0


def test_started_steps_accepts_repl_display_record_shape(runner: ModuleType) -> None:
    event = {"type": "step_started", "step_id": runner.NEW_STEPS[1], "payload": {"index": 2}}

    assert runner._started_steps([event]) == [(0, runner.NEW_STEPS[1])]


def test_ros_short_form_intrinsics_are_collected_as_templates(runner: ModuleType, tmp_path: Path) -> None:
    template = tmp_path / "template.yml"
    template.write_text(
        "ROSTemplateFormatVersion: '2015-09-01'\n"
        "Resources:\n"
        "  Vpc:\n"
        "    Type: ALIYUN::ECS::VPC\n"
        "Outputs:\n"
        "  VpcId:\n"
        "    Value: !GetAtt Vpc.VpcId\n",
        encoding="utf-8",
    )

    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.safe_load(template.read_text(encoding="utf-8"))
    assert runner._is_iac_template_file(template)


def _pipeline_check_runtime(runner: ModuleType, tmp_path: Path, profile: str) -> argparse.Namespace:
    return argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.A2A, profile=profile),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )


def test_deploy_order_uses_confirm_action_not_later_cancel(runner: ModuleType, tmp_path: Path) -> None:
    runtime = _pipeline_check_runtime(runner, tmp_path, "happy_multi")
    values = [
        {
            "eventType": "input_received",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {"kind": "deployment_confirmation", "action": "confirm"},
        },
        {"eventType": "tool_started", "data": {"toolName": "ros_deploy"}},
        {
            "eventType": "tool_result",
            "data": {"toolName": "ros_deploy", "result": '{"StackId": "stack-1"}'},
        },
        {
            "eventType": "input_received",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {"kind": "deployment_confirmation"},
        },
    ]

    runner._common_pipeline_checks(runtime, values)

    assert runtime.checks["no deploy before confirmation"] is True


def test_safe_cancel_requires_that_no_deployment_was_attempted(runner: ModuleType, tmp_path: Path) -> None:
    # A02 cancels instead of confirming, so ros_deploy must never be reached. Safe mode does not
    # restrict step tools, so an attempted deployment there would be a real cloud write.
    runtime = _pipeline_check_runtime(runner, tmp_path, "safe_cancel")
    canceled = [
        {
            "eventType": "input_received",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {"kind": "deployment_confirmation"},
        },
    ]

    runner._common_pipeline_checks(runtime, canceled)

    assert runtime.checks["cancel kept the deployment unattempted"] is True
    assert runtime.checks["safe mode and cancel made no cloud write"] is True

    attempted = _pipeline_check_runtime(runner, tmp_path, "safe_cancel")
    runner._common_pipeline_checks(
        attempted,
        [*canceled, {"eventType": "tool_started", "data": {"toolName": "ros_deploy"}}],
    )

    assert attempted.checks["cancel kept the deployment unattempted"] is False


def test_deploy_order_accepts_repl_display_confirmation_shape(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.REPL, profile="happy_single"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    values = [
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"kind": "deployment_confirmation", "action": "confirm"},
        },
        {"type": "tool_used", "step_id": runner.NEW_STEPS[2], "payload": {"name": "ros_deploy"}},
    ]

    runner._common_pipeline_checks(runtime, values)

    assert runtime.checks["no deploy before confirmation"] is True


def test_deploy_order_accepts_repl_free_text_only_when_it_enters_step3(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.REPL, profile="natural_adjust"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    values = [
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"kind": "deployment_confirmation", "structured": False, "selected_value": "调整参数"},
        },
        {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {}},
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"kind": "deployment_confirmation", "structured": False, "selected_value": "确认部署"},
        },
        {"type": "step_started", "step_id": runner.NEW_STEPS[2]},
        {"type": "tool_used", "step_id": runner.NEW_STEPS[2], "payload": {"name": "ros_deploy"}},
    ]

    runner._common_pipeline_checks(runtime, values)

    assert runtime.checks["no deploy before confirmation"] is True

    runtime.checks = {}
    runner._common_pipeline_checks(runtime, [values[0], values[-1]])
    assert runtime.checks["no deploy before confirmation"] is False


def test_confirmation_acceptance_uses_structured_free_quote(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.A2A, profile="step2_parameter"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    values = [
        {
            "eventType": "input_required",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {
                "kind": "deployment_confirmation",
                "solution_summary": "在已有 VPC 下创建一个 VSwitch。",
                "cost": {
                    "quote_status": "succeeded",
                    "monthly_estimate": "¥0/月",
                    "resources": [],
                },
            },
        }
    ]

    runner._common_pipeline_checks(runtime, values)

    assert runtime.checks["confirmation includes current solution and quote"] is True
    assert runtime.checks["A2A waiting input was exercised"] is True


def test_successful_quote_must_be_projected_as_succeeded(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.A2A, profile="step2_parameter"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    values = [
        {
            "eventType": "tool_result",
            "data": {"toolName": "ros_estimate_template_cost", "isError": False},
        },
        {
            "eventType": "input_required",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {
                "kind": "deployment_confirmation",
                "solution_summary": "create a network",
                "cost": {
                    "quote_status": "unavailable",
                    "monthly_estimate": "询价不可用",
                    "resources": [],
                },
            },
        },
    ]

    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["successful ROS quote projected into confirmation"] is False

    values[1]["data"]["cost"].update({"quote_status": "succeeded", "monthly_estimate": "¥0/月"})
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["successful ROS quote projected into confirmation"] is True


def test_common_checks_ignore_handled_tool_traceback_but_reject_terminal_traceback(
    runner: ModuleType, tmp_path: Path
) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.A2A, profile="step2_parameter"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    handled_tool_error = {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "tool_result",
                            "data": {
                                "isError": True,
                                "result": "STDERR:\nTraceback (most recent call last):\nValueError: bad input",
                            },
                        }
                    }
                }
            }
        }
    }

    runner._common_pipeline_checks(runtime, [handled_tool_error])
    assert runtime.checks["no unhandled terminal error"] is True
    assert "A2A waiting input was exercised" not in runtime.checks
    assert "confirmation includes current solution and quote" not in runtime.checks

    runner._common_pipeline_checks(
        runtime,
        [{"transcript": "Bash output:\nTraceback (most recent call last):\nModuleNotFoundError: optional tool"}],
    )
    assert runtime.checks["no unhandled terminal error"] is True

    runner._common_pipeline_checks(runtime, [{"message": "cancel before deployment_confirmation"}])
    assert "confirmation includes current solution and quote" not in runtime.checks

    runner._common_pipeline_checks(runtime, [{"error": "Traceback (most recent call last):\nRuntimeError: boom"}])
    assert runtime.checks["no unhandled terminal error"] is False


def test_repl_artifacts_reject_child_exit_before_runner_teardown(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = argparse.Namespace(
        env={},
        paths=argparse.Namespace(run_dir=tmp_path),
        checks={},
    )
    pty = argparse.Namespace(
        transcript="handled tool output",
        events=[
            {
                "type": "terminate",
                "force": False,
                "aliveBeforeTerminate": False,
                "exitStatus": 1,
                "signalStatus": None,
            }
        ],
    )
    repl = argparse.Namespace(
        _redact_sensitive_text=lambda text, _env: text,
        _normalize_transcript=lambda text: text,
    )
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: [])
    monkeypatch.setattr(runner, "_common_pipeline_checks", lambda *_args: None)

    runner._write_repl_artifacts(runtime, pty, repl)

    assert runtime.checks["REPL stayed alive until teardown"] is False
    assert runtime.checks["REPL has no terminal exception"] is False
    recorded = json.loads((tmp_path / "repl-events.jsonl").read_text(encoding="utf-8"))
    assert recorded["exitStatus"] == 1


def test_first_pending_resource_option_id_ignores_control_actions(runner: ModuleType) -> None:
    a2a = argparse.Namespace(
        _extract_pipeline_envelopes=lambda event: event["envelopes"],
    )
    event = {
        "envelopes": [
            {
                "eventType": "input_required",
                "data": {
                    "kind": "ask_user_question",
                    "options": [
                        {"label": "existing VPC", "id": "vpc-test123"},
                        {"label": "other", "id": "vpc-test456"},
                    ],
                },
            }
        ]
    }

    assert runner._first_pending_resource_option_id(a2a, event) == "vpc-test123"
    control_event = {
        "envelopes": [
            {
                "eventType": "input_required",
                "data": {"options": [{"label": "open console", "id": "open_console"}]},
            }
        ]
    }
    assert runner._first_pending_resource_option_id(a2a, control_event) == ""
    assert runner._first_pending_resource_option_id(a2a, {"envelopes": []}) == ""


def test_input_received_kind_and_step_matches_candidate_selection(runner: ModuleType) -> None:
    a2a = argparse.Namespace(_extract_pipeline_envelopes=lambda event: event["envelopes"])
    predicate = runner._input_received_kind_and_step(
        a2a,
        runner.NEW_STEPS[0],
        "candidate_selection",
    )
    matching = {
        "envelopes": [
            {
                "eventType": "input_received",
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection", "selectedIndex": 0},
            }
        ]
    }

    assert predicate(matching, None) is True
    assert predicate({"envelopes": [{"eventType": "input_required", "data": {}}]}, None) is False


def test_successful_tool_result_matches_solution_first_quote_tool(runner: ModuleType) -> None:
    a2a = argparse.Namespace(_extract_pipeline_envelopes=lambda event: event["envelopes"])
    predicate = runner._successful_tool_result(a2a, "ros_estimate_template_cost")

    assert predicate(
        {
            "envelopes": [
                {
                    "eventType": "tool_result",
                    "data": {"toolName": "ros_estimate_template_cost", "isError": False},
                }
            ]
        },
        None,
    )
    assert not predicate(
        {
            "envelopes": [
                {
                    "eventType": "tool_result",
                    "data": {"toolName": "ros_estimate_template_cost", "isError": True},
                }
            ]
        },
        None,
    )


def test_event_files_follow_request_order_for_recovery_streams(runner: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "fault-after-quote.events.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "fault-after-snapshot.events.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "fault-final.events.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "requests.jsonl").write_text(
        "\n".join(json.dumps({"name": name}) for name in ("fault-after-snapshot", "fault-after-quote", "fault-final"))
        + "\n",
        encoding="utf-8",
    )

    assert [path.name for path in runner._event_files(tmp_path)] == [
        "fault-after-snapshot.events.jsonl",
        "fault-after-quote.events.jsonl",
        "fault-final.events.jsonl",
    ]


def test_runtime_paths_are_isolated(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "real-config"
    source.mkdir()
    paths = runner.RuntimePaths.create(tmp_path / "run", source)
    assert paths.config_dir != paths.backup_dir != paths.workspace_dir
    assert all(
        runner.is_relative_to(path, paths.run_dir) for path in (paths.config_dir, paths.backup_dir, paths.workspace_dir)
    )
    with pytest.raises(ValueError, match="credential source"):
        runner.RuntimePaths.create(tmp_path, tmp_path / "config")


def test_create_runtime_isolates_env_and_cloud_identity(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text("fake: value\n", encoding="utf-8")
    args = runner.parse_args(
        [
            "--scenario",
            "a2a-step1-clarify",
            "--concurrency",
            "1",
            "--run-root",
            str(tmp_path / "runs"),
            "--credential-source-dir",
            str(source),
        ]
    )
    runtime = runner.create_runtime(runner.SCENARIO_BY_NAME["a2a-step1-clarify"], args, runner.RunnerServices(), {})
    assert runtime.env["IAC_CODE_PIPELINE_NAME"] == "selling_solution_first"
    assert runtime.env["IAC_CODE_CONFIG_DIR"] == str(runtime.paths.config_dir)
    assert runtime.env["IAC_CODE_CONFIG_BACKUP_DIR"] == str(runtime.paths.backup_dir)
    assert runtime.stack_name.startswith("iac-e2e-ssf-a2a-step1-clarify-")
    assert runtime.cidr.startswith("10.250.")
    assert runner.is_relative_to(runtime.paths.config_dir, runtime.paths.run_dir)
    assert runner.is_relative_to(runtime.paths.backup_dir, runtime.paths.run_dir)
    assert runner.is_relative_to(runtime.paths.workspace_dir, runtime.paths.run_dir)


def test_multimodal_runtime_default_is_not_overridden_by_inherited_text_model(
    runner: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text("fake: value\n", encoding="utf-8")
    args = runner.parse_args(
        [
            "--scenario",
            "repl-multimodal-lifecycle",
            "--concurrency",
            "1",
            "--run-root",
            str(tmp_path / "runs"),
            "--credential-source-dir",
            str(source),
        ]
    )

    runtime = runner.create_runtime(
        runner.SCENARIO_BY_NAME["repl-multimodal-lifecycle"],
        args,
        runner.RunnerServices(),
        {"provider": "dashscope", "model": runner.DEFAULT_TEXT_MODEL},
    )

    assert runtime.env["IAC_CODE_MODEL"] == runner.DEFAULT_MULTIMODAL_MODEL


def test_explicit_model_overrides_multimodal_runtime_default(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text("fake: value\n", encoding="utf-8")
    args = runner.parse_args(
        [
            "--scenario",
            "repl-multimodal-lifecycle",
            "--model",
            "explicit-vision-model",
            "--concurrency",
            "1",
            "--run-root",
            str(tmp_path / "runs"),
            "--credential-source-dir",
            str(source),
        ]
    )

    runtime = runner.create_runtime(
        runner.SCENARIO_BY_NAME["repl-multimodal-lifecycle"],
        args,
        runner.RunnerServices(),
        {"provider": "dashscope", "model": runner.DEFAULT_TEXT_MODEL},
    )

    assert runtime.env["IAC_CODE_MODEL"] == "explicit-vision-model"


def test_runtime_rejects_case_directory_inside_real_config(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="credential source"):
        runner.RuntimePaths.create(source / "case", source)


def test_port_and_cidr_allocators_are_thread_safe(runner: ModuleType) -> None:
    ports = runner.PortAllocator()
    cidrs = runner.CidrAllocator(["10.250.10.0/24"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        allocated_ports = list(pool.map(lambda _: ports.reserve(), range(40)))
        allocated_cidrs = list(pool.map(lambda _: cidrs.reserve(), range(40)))
    assert len(set(allocated_ports)) == 40
    assert len(set(allocated_cidrs)) == 40
    assert "10.250.10.0/24" not in allocated_cidrs
    vpc_allocator = runner.CidrAllocator(["192.168.0.0/24"], "192.168.0.0/16")
    assert vpc_allocator.reserve() == "192.168.1.0/24"


def test_named_resource_lock_only_serializes_same_name(runner: ModuleType) -> None:
    manager = runner.ResourceLockManager()
    active = 0
    maximum = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal active, maximum
        with manager.acquire("shared"):
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with guard:
                active -= 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: worker(), range(8)))
    assert maximum == 1


def _execution_args(runner: ModuleType, tmp_path: Path, *, concurrency: int = 3) -> argparse.Namespace:
    return argparse.Namespace(
        concurrency=concurrency,
        fail_fast=False,
        run_root=str(tmp_path),
        run_dir="",
    )


def test_worker_pool_honors_concurrency_and_returns_registry_order(runner: ModuleType, tmp_path: Path) -> None:
    selected = list(runner.SCENARIOS[:8])
    active = 0
    maximum = 0
    guard = threading.Lock()

    def fake_run(spec, _args, _services, _defaults, _root):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return runner.ScenarioResult(
            spec.case_id,
            spec.name,
            spec.surface.value,
            "passed",
            "start",
            "end",
            0.02,
            str(tmp_path / spec.name),
            {"fake": True},
            [],
            "completed",
        )

    results = runner.execute_selected(
        selected,
        _execution_args(runner, tmp_path),
        runner.RunnerServices(),
        {},
        tmp_path,
        run_one=fake_run,
    )
    assert maximum == 3
    assert [item.scenario for item in results] == [item.name for item in selected]
    assert all(item.passed for item in results)


def test_fail_fast_marks_unscheduled_cases_and_exit_codes(runner: ModuleType, tmp_path: Path) -> None:
    selected = list(runner.SCENARIOS[:3])
    args = _execution_args(runner, tmp_path, concurrency=1)
    args.fail_fast = True

    def fake_run(spec, _args, _services, _defaults, _root):
        return runner.ScenarioResult(
            spec.case_id,
            spec.name,
            spec.surface.value,
            "failed",
            "start",
            "end",
            0.0,
            "",
            {"fake": False},
            [],
            "completed",
        )

    results = runner.execute_selected(
        selected,
        args,
        runner.RunnerServices(),
        {},
        tmp_path,
        run_one=fake_run,
    )
    assert [item.status for item in results] == ["failed", "not-started", "not-started"]
    assert runner.suite_exit_code(results, credential_unchanged=True, interrupted=False) == 1
    assert runner.suite_exit_code([], credential_unchanged=False, interrupted=False) == 1
    assert runner.suite_exit_code([], credential_unchanged=True, interrupted=True) == 130
    assert runner.suite_exit_code([], credential_unchanged=True, interrupted=False) == 0


def test_terminate_processes_stops_registered_child(runner: ModuleType, tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("signal behavior is covered by Windows CI integration tests")
    process = __import__("subprocess").Popen([__import__("sys").executable, "-c", "import time; time.sleep(60)"])
    runtime = object.__new__(runner.ScenarioRuntime)
    runtime.processes = [process]
    assert runtime.terminate_processes()
    assert process.poll() is not None


def test_a2a_helper_server_process_is_registered_for_suite_interrupt(runner: ModuleType) -> None:
    process = __import__("subprocess").Popen([__import__("sys").executable, "-c", "import time; time.sleep(60)"])
    runtime = object.__new__(runner.ScenarioRuntime)
    runtime.processes = []

    class Harness:
        server = None

        def start_server(self) -> None:
            self.server = argparse.Namespace(process=process)

    harness = Harness()
    runner._track_a2a_server_processes(runtime, harness)
    try:
        harness.start_server()
        harness.start_server()
        assert runtime.processes == [process]
    finally:
        runtime.terminate_processes()


def test_terminate_active_processes_attempts_every_runtime(runner: ModuleType) -> None:
    calls: list[str] = []

    class Runtime:
        def __init__(self, name: str, clean: bool) -> None:
            self.name = name
            self.clean = clean

        def terminate_processes(self) -> bool:
            calls.append(self.name)
            return self.clean

    services = runner.RunnerServices()
    services.active_runtimes = {"first": Runtime("first", False), "second": Runtime("second", True)}
    assert not services.terminate_active_processes()
    assert calls == ["first", "second"]


def test_desktop_result_requires_the_full_native_contract(runner: ModuleType) -> None:
    result = {
        "pipelineName": runner.PIPELINE_NAME,
        "steps": list(runner.NEW_STEPS),
        **{name: True for name in runner.DESKTOP_RESULT_CHECKS},
        "cloudWriteObserved": False,
        "packageResources": {
            "yaml": True,
            "prompts": True,
            "skills": True,
            "hooks": True,
            "tools": True,
            "references": True,
        },
    }
    assert all(runner.validate_desktop_result(result).values())
    result["confirmationWaitingRestartRecovered"] = False
    assert not all(runner.validate_desktop_result(result).values())


def test_desktop_source_resource_audit_follows_linked_reference_directory(
    runner: ModuleType, tmp_path: Path
) -> None:
    source_root = tmp_path / "pipeline"
    shared_references = tmp_path / "shared-references"
    linked_references = source_root / "skills" / "materialize" / "references"
    shared_references.mkdir()
    (shared_references / "ros-template.md").write_text("reference", encoding="utf-8")
    linked_references.parent.mkdir(parents=True)
    linked_references.symlink_to(shared_references, target_is_directory=True)

    audit = runner.audit_desktop_source_resources(
        source_root,
        ("skills/materialize/references/ros-template.md", "pipeline.yaml"),
    )

    assert audit["sourceResourcesPresent"] == ["skills/materialize/references/ros-template.md"]
    assert audit["missingSourceResources"] == ["pipeline.yaml"]
    assert audit["allPresent"] is False


def test_case_artifact_credential_audit_ignores_config_but_detects_log_leak(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".credentials.yml").write_text("api_key: unit-secret-value\n", encoding="utf-8")
    (source / ".cloud-credentials.yml").write_text("access_key_secret: cloud-secret-value\n", encoding="utf-8")
    args = runner.parse_args(
        [
            "--scenario",
            "a2a-step1-clarify",
            "--concurrency",
            "1",
            "--run-root",
            str(tmp_path / "runs"),
            "--credential-source-dir",
            str(source),
        ]
    )
    runtime = runner.create_runtime(runner.SCENARIO_BY_NAME["a2a-step1-clarify"], args, runner.RunnerServices(), {})
    assert runner.credential_values_absent_from_artifacts(runtime)
    preflight_config = runtime.paths.run_dir / ".preflight" / "config"
    preflight_config.mkdir(parents=True)
    (preflight_config / ".credentials.yml").write_text("api_key: unit-secret-value\n", encoding="utf-8")
    assert runner.credential_values_absent_from_artifacts(runtime)
    (runtime.paths.logs_dir / "leak.log").write_text("unit-secret-value", encoding="utf-8")
    assert not runner.credential_values_absent_from_artifacts(runtime)


def test_reused_web_browser_helper_accepts_optional_dom_artifacts(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = runner._web_module()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    web._verify_browser(
        base_url="http://127.0.0.1:1",
        session_id="session-1",
        expected_text="方案",
        screenshot=tmp_path / "screen.png",
        dom_snapshot=tmp_path / "dom.txt",
        audit=tmp_path / "audit.json",
        require_quote=True,
        expand_pipeline_history=True,
    )
    command = captured["command"]
    assert isinstance(command, list)
    assert "--domSnapshot" in command
    assert "--audit" in command
    assert command[-4:] == [
        "--requireQuote",
        "true",
        "--expandPipelineHistory",
        "true",
    ]


def test_repl_selection_waits_for_durable_display_event_occurrence(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "candidate_selection_ready", "payload": {"round": 1}}),
                json.dumps({"type": "candidate_selection_ready", "payload": {"round": 2}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
        repl_candidate_wait_count=1,
    )
    pty = argparse.Namespace(events=[])

    runner._repl_wait_selection(pty, runtime)

    assert runtime.repl_candidate_wait_count == 2
    assert pty.events == [
        {
            "type": "display-event",
            "description": "selling_solution_first candidate selection",
            "event_type": "candidate_selection_ready",
            "occurrence": 2,
            "path": str(display_path),
            "at": pty.events[0]["at"],
        }
    ]


def test_repl_candidate_waiting_restart_uses_durable_events_and_handoff_delay(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def terminate(self, *, force: bool = False) -> None:
            calls.append(("terminate", force))

        def spawn(self, *, extra_args: list[str]) -> None:
            calls.append(("spawn", extra_args))

        def drain_output(self) -> None:
            calls.append("drain")

    monkeypatch.setattr(runner, "_repl_wait_selection", lambda _pty, _runtime: calls.append("selection"))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._restart_repl_at_waiting(
        Pty(),
        runner.REPL_SELECTION_PATTERNS,
        argparse.Namespace(),
        "candidate selection",
    )

    assert calls == [
        "selection",
        ("terminate", True),
        ("spawn", ["--continue"]),
        "selection",
        ("sleep", 0.5),
        "drain",
    ]


def test_repl_confirmation_restart_waits_for_ready_hint_only_once(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            calls.append(("expect", patterns, description, timeout))
            return patterns[0]

        def terminate(self, *, force: bool = False) -> None:
            calls.append(("terminate", force))

        def spawn(self, *, extra_args: list[str]) -> None:
            calls.append(("spawn", extra_args))

    monkeypatch.setattr(
        runner,
        "_prepare_restored_repl_confirmation",
        lambda _pty, _runtime: calls.append("prepare-confirmation"),
    )
    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0))

    runner._restart_repl_at_waiting(
        Pty(),
        runner.REPL_CONFIRMATION_PATTERNS,
        runtime,
        "deployment confirmation",
    )

    assert calls == [
        (
            "expect",
            runner.REPL_CONFIRMATION_PATTERNS,
            "deployment confirmation before restart",
            9.0,
        ),
        ("terminate", True),
        ("spawn", ["--continue"]),
        "prepare-confirmation",
    ]


def test_repl_step_started_wait_filters_by_target_step(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
                {"type": "step_started", "step_id": runner.NEW_STEPS[1]},
                {"type": "step_started", "step_id": runner.NEW_STEPS[1]},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
    )
    pty = argparse.Namespace(events=[])

    runner._repl_wait_step_started(
        pty,
        runtime,
        step_id=runner.NEW_STEPS[1],
        occurrence=2,
        description="resumed Step 2",
    )

    assert pty.events[0]["description"] == "resumed Step 2"
    assert pty.events[0]["step_id"] == runner.NEW_STEPS[1]
    assert pty.events[0]["occurrence"] == 2


def test_repl_running_checkpoint_uses_target_step_persisted_tool_use(runner: ModuleType, tmp_path: Path) -> None:
    pipeline_dir = tmp_path / "config" / "projects" / "project" / "session" / "pipeline"
    transcript_path = pipeline_dir / "transcripts" / "transcript_att_0002" / "session.jsonl"
    transcript_path.parent.mkdir(parents=True)
    (pipeline_dir / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "attempts": {
                    "items": {
                        "att_0001": {
                            "step_id": runner.NEW_STEPS[0],
                            "transcript_id": "transcript_att_0001",
                        },
                        "att_0002": {
                            "step_id": runner.NEW_STEPS[1],
                            "transcript_id": "transcript_att_0002",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    transcript_path.write_text(
        json.dumps(
            {
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "write_file", "id": "call-step2"},
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class Pty:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def drain_output(self) -> None:
            return None

    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
    )
    pty = Pty()

    runner._wait_repl_transcript_tool_use(
        pty,
        runtime,
        step_id=runner.NEW_STEPS[1],
        tool_names={"write_file"},
        description="Step 2 template checkpoint",
    )

    assert pty.events[0]["type"] == "transcript-tool-use"
    assert pty.events[0]["step_id"] == runner.NEW_STEPS[1]
    assert pty.events[0]["tool_name"] == "write_file"
    assert pty.events[0]["tool_use_id"] == "call-step2"
    assert pty.events[0]["path"] == str(transcript_path)


def test_repl_running_step2_checkpoint_rejects_already_reached_confirmation(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(
        json.dumps(
            {
                "type": "user_input_required",
                "step_id": runner.NEW_STEPS[1],
                "payload": {"kind": "deployment_confirmation"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
    )
    pty = argparse.Namespace(drain_output=lambda: None, events=[])

    with pytest.raises(RuntimeError, match="deployment confirmation"):
        runner._wait_repl_transcript_tool_use(
            pty,
            runtime,
            step_id=runner.NEW_STEPS[1],
            tool_names={"write_file"},
            description="Step 2 template checkpoint",
        )


def test_repl_running_checkpoint_rejects_already_terminal_pipeline(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(json.dumps({"type": "pipeline_completed"}) + "\n", encoding="utf-8")
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
    )
    pty = argparse.Namespace(drain_output=lambda: None, events=[])

    with pytest.raises(RuntimeError, match="pipeline_completed"):
        runner._wait_repl_transcript_tool_use(
            pty,
            runtime,
            step_id=runner.NEW_STEPS[2],
            tool_names={"ros_deploy"},
            description="Step 3 deployment checkpoint",
        )


def test_repl_running_step1_resume_waits_on_candidate_boundary_without_second_step_start(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []

    class Pty:
        def __init__(self, **_kwargs: object) -> None:
            self.events: list[dict[str, object]] = []

        def spawn(self, *, extra_args: list[str] | None = None) -> None:
            calls.append(("spawn", extra_args))

        def terminate(self, *, force: bool = False) -> None:
            calls.append(("terminate", force))

        def drain_output(self) -> None:
            calls.append("drain")

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    fake_repl = argparse.Namespace(ReplPty=Pty, _expect_initial_prompt=lambda *_args: calls.append("ready"))
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=1.0),
        env={},
        paths=argparse.Namespace(run_dir=tmp_path, workspace_dir=tmp_path),
        spec=argparse.Namespace(profile="running_step1", cloud_write=False),
        checks={},
        event=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: fake_repl)
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_args: calls.append("initial"))
    monkeypatch.setattr(
        runner,
        "_repl_wait_step_started",
        lambda *_args, **kwargs: calls.append(("step-started", kwargs["occurrence"])),
    )
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args: calls.append("selection"))
    monkeypatch.setattr(runner, "_repl_select_current", lambda *_args: calls.append("select"))
    monkeypatch.setattr(runner, "_repl_wait_confirmation", lambda *_args: calls.append("confirmation"))
    monkeypatch.setattr(runner, "_repl_choose_direct_input", lambda *_args: calls.append("cancel"))
    monkeypatch.setattr(runner, "_repl_wait_pipeline_completed", lambda *_args: calls.append("completed"))
    monkeypatch.setattr(runner, "_write_repl_artifacts", lambda *_args: calls.append("artifacts"))
    monkeypatch.setattr(runner.time, "sleep", lambda *_args: None)

    runner._run_repl(runtime)

    assert [item for item in calls if isinstance(item, tuple) and item[0] == "step-started"] == [("step-started", 1)]
    assert ("spawn", ["--continue"]) in calls
    assert calls.index("selection") > calls.index(("spawn", ["--continue"]))
    assert runtime.checks[f"{runner.NEW_STEPS[0]} auto-continued after --continue"] is True


def test_repl_display_wait_fails_fast_on_terminal_pipeline_event(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(json.dumps({"type": "pipeline_user_aborted"}) + "\n", encoding="utf-8")
    runtime = argparse.Namespace(paths=argparse.Namespace(config_dir=tmp_path / "config"))

    with pytest.raises(RuntimeError, match="pipeline_user_aborted.*candidate_selection_ready"):
        runner._wait_repl_display_event(
            runtime,
            event_type="candidate_selection_ready",
            occurrence=1,
            timeout=1.0,
        )


def test_repl_candidate_switch_uses_right_arrow_before_enter(runner: ModuleType) -> None:
    sent: list[tuple[str, str]] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            sent.append((text, label))

    runner._repl_select_current(Pty(), next_candidate=True)

    assert sent == [("\x1b[C", "candidate-right"), ("\r", "candidate-enter")]


def test_repl_restored_line_input_uses_paste_then_separate_enter(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_line_input(Pty(), "杭州 VSwitch", label="answer")

    assert calls == [
        ("send", "\x1b[200~杭州 VSwitch\x1b[201~", "answer-paste"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "answer-enter"),
    ]


def test_repl_pipeline_interrupt_waits_for_editor_before_submitting(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    fake_repl = argparse.Namespace(_expect_interrupt_input_ready=lambda *_args, **_kwargs: calls.append("ready"))
    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: fake_repl)
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_pipeline_interrupt(Pty(), argparse.Namespace(), "改为只创建空 VPC")

    assert calls == [
        ("send", "\x1b", "pipeline-stream-interrupt"),
        "ready",
        ("sleep", 0.25),
        "drain",
        ("send", "\x1b[200~改为只创建空 VPC\x1b[201~", "pipeline-stream-interrupt-input-paste"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "pipeline-stream-interrupt-input-enter"),
    ]


def test_repl_direct_input_focuses_editable_row_before_typing(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            events.append((text, label))

        def drain_output(self) -> None:
            events.append("drain")

    runtime = argparse.Namespace(repl_confirmation_action_count=3)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    runner._repl_choose_direct_input(runtime, Pty(), "调整参数")

    assert events == [
        ("\x1b[B", "confirmation-input-down-1"),
        ("\x1b[B", "confirmation-input-down-2"),
        ("\x1b[B", "confirmation-input-down-3"),
        ("\x1b[200~调整参数\x1b[201~", "confirmation-direct-input-paste"),
        ("sleep", 0.1),
        "drain",
        ("\r", "confirmation-direct-input-enter"),
    ]


def test_repl_direct_image_focuses_editable_row_and_submits_after_paste(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(repl_confirmation_action_count=2)
    monkeypatch.setattr(
        runner,
        "_repl_paste_generated_image",
        lambda _runtime, _pty, key, text: calls.append(("image", key, text)),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_choose_direct_image(runtime, Pty(), "adjustment", "调整 VSwitch 网段")

    assert calls == [
        ("send", "\x1b[B", "confirmation-input-down-1"),
        ("send", "\x1b[B", "confirmation-input-down-2"),
        ("image", "adjustment", "调整 VSwitch 网段"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "confirmation-direct-image-enter"),
    ]


def test_repl_image_fixture_uses_separate_enter_after_refresh(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def paste_image_fixture(self, key: str) -> None:
            calls.append(("fixture", key))

        def drain_output(self) -> None:
            calls.append("drain")

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_image_fixture(Pty(), "normal-followup", label="normal-image-enter")

    assert calls == [
        ("fixture", "normal-followup"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "normal-image-enter"),
    ]


def test_repl_generated_image_uses_separate_enter_after_refresh(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def drain_output(self) -> None:
            calls.append("drain")

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    monkeypatch.setattr(
        runner,
        "_repl_paste_generated_image",
        lambda _runtime, _pty, key, text: calls.append(("image", key, text)),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_generated_image(
        argparse.Namespace(),
        Pty(),
        "initial",
        "方案选定后必须由我选择 VPC",
        label="initial-image-enter",
    )

    assert calls == [
        ("image", "initial", "方案选定后必须由我选择 VPC"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "initial-image-enter"),
    ]


def test_repl_confirmation_records_action_count_from_display(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {
                    "type": "user_input_required",
                    "step_id": runner.NEW_STEPS[1],
                    "payload": {
                        "kind": "deployment_confirmation",
                        "options": [{"action": "confirm"}, {"action": "cancel"}],
                    },
                },
                {
                    "type": "user_input_required",
                    "step_id": runner.NEW_STEPS[1],
                    "payload": {
                        "kind": "deployment_confirmation",
                        "options": [
                            {"action": "confirm"},
                            {"action": "reselect"},
                            {"action": "cancel"},
                        ],
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    class Pty:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def expect_any(self, patterns, *, description, timeout):
            assert patterns == runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS
            assert description == "deployment confirmation selector ready #2"
            assert timeout == 9.0
            calls.append("expect")
            return patterns[0]

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=9.0),
        repl_confirmation_wait_count=1,
        repl_confirmation_action_count=0,
    )
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    pty = Pty()

    runner._repl_wait_confirmation(pty, runtime)

    assert calls == ["expect", "drain"]
    assert runtime.repl_confirmation_wait_count == 2
    assert runtime.repl_confirmation_action_count == 3
    assert pty.events[0]["occurrence"] == 2


def test_repl_recovery_confirmation_uses_durable_event_without_rematching_drained_hint(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    event = {
        "type": "user_input_required",
        "step_id": runner.NEW_STEPS[1],
        "payload": {
            "kind": "deployment_confirmation",
            "options": [{"action": "confirm"}, {"action": "cancel"}],
        },
    }

    class Pty:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def expect_any(self, *_args, **_kwargs):
            raise AssertionError("recovery must not rematch an already-drained Live hint")

        def drain_output(self) -> None:
            calls.append("drain")

    monkeypatch.setattr(runner, "_wait_repl_display_event", lambda *_args, **_kwargs: (event, Path("display")))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=9.0),
        repl_confirmation_wait_count=0,
        repl_confirmation_action_count=0,
    )
    pty = Pty()

    runner._repl_wait_confirmation(pty, runtime, require_input_ready=False)

    assert calls == [("sleep", 0.5), "drain"]
    assert runtime.repl_confirmation_action_count == 2
    assert pty.events[0]["event_type"] == "user_input_required"


def test_repl_post_rollback_confirmation_answers_parameter_ask_first(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    matches = iter(
        [
            runner.REPL_ASK_INPUT_READY_PATTERNS[0],
            runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS[0],
        ]
    )

    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            calls.append(("expect", description, timeout, patterns))
            return next(matches)

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=9.0, cleanup_vpc_id="vpc-test"),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        runner,
        "_repl_submit_line_input",
        lambda _pty, text, *, label: calls.append(("answer", text, label)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_wait_confirmation",
        lambda _pty, _runtime, *, require_input_ready: calls.append(("confirmation", require_input_ready)),
    )

    runner._repl_wait_confirmation_after_optional_parameter_asks(Pty(), runtime)

    assert calls == [
        (
            "expect",
            "post-rollback Step 2 ask or confirmation #1",
            9.0,
            runner.REPL_ASK_INPUT_READY_PATTERNS + runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS,
        ),
        ("sleep", 0.25),
        "drain",
        ("answer", "vpc-test", "post-rollback-parameter-answer-1"),
        (
            "expect",
            "post-rollback Step 2 ask or confirmation #2",
            9.0,
            runner.REPL_ASK_INPUT_READY_PATTERNS + runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS,
        ),
        ("confirmation", False),
    ]


def test_repl_multimodal_confirmation_answers_repeated_asks_before_confirmation(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    matches = iter(
        [
            runner.REPL_ASK_INPUT_READY_PATTERNS[0],
            runner.REPL_ASK_INPUT_READY_PATTERNS[0],
            runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS[0],
        ]
    )

    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            calls.append(("expect", description, timeout, patterns))
            return next(matches)

        def drain_output(self) -> None:
            calls.append("drain")

        def paste_image_fixture(self, key: str) -> None:
            calls.append(("fixture", key))

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        runner,
        "_repl_paste_generated_image",
        lambda _runtime, _pty, key, text: calls.append(("generated", key, text)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_wait_confirmation",
        lambda _pty, _runtime, *, require_input_ready: calls.append(("confirmation", require_input_ready)),
    )

    runner._repl_wait_multimodal_confirmation(
        runtime,
        Pty(),
        primary_image_key="ask-first-answer",
        phase="initial",
    )

    assert calls[0] == (
        "expect",
        "initial image ask or confirmation #1",
        9.0,
        runner.REPL_ASK_INPUT_READY_PATTERNS + runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS,
    )
    assert ("fixture", "ask-first-answer") in calls
    generated = next(item for item in calls if isinstance(item, tuple) and item[0] == "generated")
    assert generated[1] == "initial-parameter-2"
    assert "第一个默认 VPC" in generated[2]
    assert ("send", "\r", "initial-image-ask-enter-1") in calls
    assert ("send", "\r", "initial-image-ask-enter-2") in calls
    assert calls[-2][0:2] == ("expect", "initial image ask or confirmation #3")
    assert calls[-1] == ("confirmation", False)


def test_repl_multimodal_confirmation_uses_phase_specific_generated_answer(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    matches = iter(
        [
            runner.REPL_ASK_INPUT_READY_PATTERNS[0],
            runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS[0],
        ]
    )

    class Pty:
        def expect_any(self, _patterns, *, description, timeout):
            calls.append(("expect", description, timeout))
            return next(matches)

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        runner,
        "_repl_submit_generated_image",
        lambda _runtime, _pty, key, text, *, label: calls.append(("generated", key, text, label)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_wait_confirmation",
        lambda _pty, _runtime, *, require_input_ready: calls.append(("confirmation", require_input_ready)),
    )

    runner._repl_wait_multimodal_confirmation(
        runtime,
        Pty(),
        primary_image_key="rollback-ask-answer",
        primary_image_text="选择第一个已有 VPC，继续创建安全组，不创建 VSwitch。",
        phase="rollback",
    )

    assert (
        "generated",
        "rollback-ask-answer",
        "选择第一个已有 VPC，继续创建安全组，不创建 VSwitch。",
        "rollback-image-ask-enter-1",
    ) in calls
    assert calls[-1] == ("confirmation", False)


def test_repl_multimodal_selection_answers_step1_ask_before_candidates(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    display_events = iter([[], [], [{"type": "candidate_selection_ready"}]])

    class Pty:
        def __init__(self) -> None:
            self.transcript = ""
            self.events: list[dict[str, object]] = []
            self.args = argparse.Namespace(permission_prompt_response="pageup-enter")
            self.drain_count = 0

        def drain_output(self) -> None:
            calls.append("drain")
            self.drain_count += 1
            if self.drain_count == 1:
                self.transcript += "Yes, allow once"
            elif self.drain_count == 2:
                self.transcript += "  > \x1b"

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0), repl_candidate_wait_count=0)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        runner,
        "_legacy_repl_module",
        lambda: argparse.Namespace(
            PERMISSION_PROMPT_PATTERNS=(r"Yes, allow once",),
            _permission_prompt_response_sequence=lambda value: f"allow:{value}",
        ),
    )
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: next(display_events))
    monkeypatch.setattr(
        runner,
        "_repl_submit_generated_image",
        lambda _runtime, _pty, key, text, *, label: calls.append(("generated", key, text, label)),
    )
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args: calls.append("selection"))

    runner._repl_wait_multimodal_selection(runtime, Pty(), phase="rollback")

    assert ("send", "allow:pageup-enter", "permission-prompt-response") in calls
    generated = next(item for item in calls if isinstance(item, tuple) and item[0] == "generated")
    assert generated[1] == "rollback-step1-answer-1"
    assert "继续规划安全组" in generated[2]
    assert generated[3] == "rollback-step1-image-ask-enter-1"
    assert calls[-1] == "selection"


def test_repl_multimodal_handoff_waits_for_normal_prompt_before_image_followup(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def expect_any(self, patterns, *, description, timeout):
            calls.append(("expect", description, timeout))
            return patterns[0]

    pty = Pty()
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=9.0),
        cidr="10.250.0.0/24",
        checks={},
    )

    def submit_image(_pty, key: str, *, label: str) -> None:
        calls.append(("image", key, label))
        pty.events.append({"type": "paste-image-fixture", "image_key": key})

    def wait_multimodal(
        _runtime,
        _pty,
        *,
        primary_image_key: str,
        phase: str,
        primary_image_text: str | None = None,
    ) -> None:
        calls.append(("confirmation", phase, primary_image_key, primary_image_text))
        pty.events.append({"type": "paste-image-fixture", "image_key": primary_image_key})

    def direct_image(_runtime, _pty, key: str, text: str) -> None:
        calls.append(("direct-image", key, text))
        pty.events.append({"type": "paste-image-fixture", "image_key": key})

    monkeypatch.setattr(runner, "_repl_submit_image_fixture", submit_image)
    monkeypatch.setattr(
        runner,
        "_repl_submit_generated_image",
        lambda _runtime, _pty, key, text, *, label: submit_image(_pty, key, label=label),
    )
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args: calls.append("selection"))
    monkeypatch.setattr(
        runner,
        "_repl_wait_multimodal_selection",
        lambda _runtime, _pty, *, phase: calls.append(("multimodal-selection", phase)),
    )
    monkeypatch.setattr(runner, "_repl_wait_multimodal_confirmation", wait_multimodal)
    monkeypatch.setattr(runner, "_repl_choose_direct_image", direct_image)
    monkeypatch.setattr(runner, "_repl_choose_direct_input", lambda *_args: calls.append("cancel"))
    monkeypatch.setattr(
        runner,
        "_legacy_repl_module",
        lambda: argparse.Namespace(_expect_initial_prompt=lambda *_args: calls.append("normal-prompt-ready")),
    )
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())

    runner._run_repl_multimodal_lifecycle(runtime, pty)

    initial_confirmation = next(
        item
        for item in calls
        if isinstance(item, tuple) and item[:3] == ("confirmation", "initial", "ask-first-answer")
    )
    assert "第一个已有 VPC" in initial_confirmation[3]
    assert "不要再次询问" in initial_confirmation[3]
    handoff_index = calls.index(("expect", "multimodal pipeline handoff", 9.0))
    ready_index = calls.index("normal-prompt-ready")
    followup_index = calls.index(("image", "normal-followup", "normal-followup-image-enter"))
    response_index = calls.index(("expect", "normal image follow-up response", 9.0))
    assert handoff_index < ready_index < followup_index < response_index
    assert runtime.checks["REPL full image lifecycle exercised"] is True


def test_repl_step1_clarification_uses_repl_and_display_event_order(runner: ModuleType) -> None:
    repl_events = [
        {"type": "expect", "description": "pipeline question input ready"},
        {"type": "display-event", "event_type": "candidate_selection_ready"},
        {"type": "candidate-interrupt"},
        {"type": "display-event", "event_type": "candidate_selection_ready"},
    ]
    display_events = [
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_diagram", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_detail", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_diagram", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_detail", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
    ]

    assert runner._repl_step1_clarification_checks(repl_events, display_events) == (True, True)


def test_repl_step1_replan_uses_parameter_free_vpc_target(runner: ModuleType) -> None:
    prompt = runner._repl_step1_replan_prompt(argparse.Namespace(cidr="10.250.9.0/24"))

    assert "只创建一个空 VPC" in prompt
    assert "10.250.9.0/24" in prompt
    assert "不创建 VSwitch、安全组、ECS 或公网资源" in prompt


def test_repl_step1_clarification_answer_is_complete_enough_for_candidates(runner: ModuleType) -> None:
    answer = runner._repl_step1_clarification_answer(argparse.Namespace(cidr="10.250.9.0/24"))

    assert all(item in answer for item in ("杭州", "VPC", "VSwitch", "安全组", "ECS", "10.250.9.0/24"))
    assert all(item in answer for item in ("可用区", "实例规格", "公共镜像", "自动选择"))


@pytest.mark.parametrize(
    ("steps", "require_all", "expected"),
    [
        ((0, 1), False, True),
        ((0, 1), True, False),
        ((0, 1, 2), True, True),
        ((0, 1, 0, 1, 2), True, True),
        ((0, 2), False, False),
        ((1,), False, False),
    ],
)
def test_repl_progress_follows_three_step_state_machine(
    runner: ModuleType, steps: tuple[int, ...], require_all: bool, expected: bool
) -> None:
    display_events = [{"type": "step_started", "step_id": runner.NEW_STEPS[index]} for index in steps]

    assert runner._repl_progress_follows_step_order(display_events, require_all=require_all) is expected


def test_repl_natural_adjustment_is_proven_by_outcomes_without_structured_action(runner: ModuleType) -> None:
    display_events = [
        {
            "type": "user_input_required",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"solution_summary": "before", "effective_deployment_parameters": {"Cidr": "old"}},
        },
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"selected_value": "调整网段并重新询价", "structured": False},
        },
        {
            "type": "user_input_required",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"solution_summary": "after", "effective_deployment_parameters": {"Cidr": "new"}},
        },
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"selected_value": "确认部署", "structured": False},
        },
        {"type": "step_started", "step_id": runner.NEW_STEPS[2]},
    ]
    transcript_values = [
        {"name": "ros_preview_template"},
        {"name": "ros_estimate_template_cost"},
        {"name": "ros_preview_template"},
        {"name": "ros_estimate_template_cost"},
    ]

    assert runner._repl_natural_adjustment_checks(display_events, transcript_values) == {
        "REPL direct text produced an adjustment": True,
        "REPL natural language confirmation was classified": True,
        "REPL adjustment produced a refreshed confirmation": True,
        "REPL adjustment reran Preview and quote": True,
    }


def test_repl_question_waits_for_actual_input_prompt(runner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            calls.append(("question", (patterns, description, timeout)))

        def drain_output(self) -> None:
            calls.append(("drain", None))

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0, timeout=4.0))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_wait_ask(Pty(), runtime, description="Step 1 question")

    assert calls == [
        ("question", (runner.REPL_ASK_INPUT_READY_PATTERNS, "Step 1 question input ready", 9.0)),
        ("sleep", 0.25),
        ("drain", None),
    ]
    pattern = runner.REPL_ASK_INPUT_READY_PATTERNS[0]
    assert re.search(pattern, "\x1b[0m  > \x1b[?25h")
    assert not re.search(pattern, "> quoted model text")


def test_repl_step2_question_fails_fast_if_confirmation_appears_first(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            assert patterns == runner.REPL_ASK_INPUT_READY_PATTERNS + runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS
            assert description == "Step 2 VPC parameter question input ready"
            assert timeout == 9.0
            return runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS[0]

        def drain_output(self) -> None:
            raise AssertionError("a rejected confirmation must fail before the handoff drain")

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0))

    with pytest.raises(RuntimeError, match="deployment confirmation appeared before Step 2 VPC parameter question"):
        runner._repl_wait_ask(
            Pty(),
            runtime,
            description="Step 2 VPC parameter question",
            reject_confirmation=True,
        )


def test_repl_candidate_interrupt_waits_for_line_editor_handoff(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    fake_repl = argparse.Namespace(_expect_interrupt_input_ready=lambda *_args, **_kwargs: calls.append("ready"))
    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: fake_repl)
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_candidate_interrupt(Pty(), argparse.Namespace(), "改成空 VPC")

    assert calls == [
        ("send", "\x1b", "candidate-interrupt"),
        "ready",
        ("sleep", 0.25),
        "drain",
        ("send", "\x1b[200~改成空 VPC\x1b[201~", "candidate-interrupt-input"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "candidate-interrupt-enter"),
    ]


def test_repl_step2_parameter_waits_only_after_candidate_selection(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def sendline(self, text: str) -> None:
            calls.append(("sendline", text))

    runtime = argparse.Namespace(
        spec=argparse.Namespace(profile="step2_parameter", cloud_write=False),
        args=argparse.Namespace(cleanup_vpc_id="vpc-test", cleanup_zone_id="cn-hangzhou-i"),
    )
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_args: calls.append("initial"))
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args: calls.append("selection"))
    monkeypatch.setattr(
        runner,
        "_repl_select_current",
        lambda *_args, **kwargs: calls.append(("select", kwargs["next_candidate"])),
    )
    monkeypatch.setattr(
        runner,
        "_repl_wait_ask",
        lambda *_args, **kwargs: calls.append(("ask", kwargs["description"])),
    )
    monkeypatch.setattr(runner, "_repl_wait_confirmation", lambda *_args: calls.append("confirmation"))
    monkeypatch.setattr(
        runner,
        "_repl_choose_direct_input",
        lambda _runtime, _pty, text: calls.append(("direct", text)),
    )

    runner._repl_basic_flow(runtime, Pty())

    assert calls == [
        "initial",
        "selection",
        ("select", False),
        ("ask", "Step 2 VPC parameter question"),
        ("sendline", "vpc-test"),
        ("ask", "Step 2 zone parameter question"),
        ("sendline", "cn-hangzhou-i"),
        "confirmation",
        ("direct", "取消本次部署，不创建任何云资源。"),
    ]


def test_step2_parameter_prompt_requires_user_answers_instead_of_api_discovery(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(profile="step2_parameter"),
        stack_name="unused-stack",
        cidr="10.250.1.0/24",
    )

    prompt = runner._initial_prompt(runtime)

    assert "VpcId" in prompt
    assert "ZoneId" in prompt
    assert "user_required" in prompt
    assert "禁止通过 API、默认值或推断自行选择" in prompt
    assert "ask_user_question 逐项" in prompt


def test_repl_replace_invalid_uses_candidate_interrupt_editor(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(
        spec=argparse.Namespace(profile="replace_invalid", cloud_write=False),
        args=argparse.Namespace(),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_args: calls.append("initial"))
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args: calls.append("selection"))
    monkeypatch.setattr(
        runner,
        "_repl_submit_candidate_interrupt",
        lambda _pty, _runtime, text: calls.append(("candidate-input", text)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_select_current",
        lambda *_args, **kwargs: calls.append(("select", kwargs["next_candidate"])),
    )
    monkeypatch.setattr(runner, "_repl_wait_confirmation", lambda *_args: calls.append("confirmation"))
    monkeypatch.setattr(
        runner,
        "_repl_choose_direct_input",
        lambda _runtime, _pty, text: calls.append(("direct", text)),
    )

    runner._repl_basic_flow(runtime, Pty())

    assert calls == [
        "initial",
        "selection",
        ("send", "9", "candidate-invalid"),
        ("sleep", 0.25),
        "drain",
        ("candidate-input", "我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch。"),
        "selection",
        ("select", False),
        "confirmation",
        ("direct", "取消本次部署，不创建任何云资源。"),
    ]


def test_repl_replace_invalid_acceptance_requires_replanned_security_group_candidate(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(
            profile="replace_invalid", surface=runner.Surface.REPL, multimodal=False, cloud_write=False
        ),
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        events_path=tmp_path / "events.jsonl",
        checks={},
    )
    repl_events = [
        {"type": "candidate-invalid"},
        {
            "type": "candidate-interrupt-input",
            "text": "\x1b[200~我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch。\x1b[201~",
        },
    ]
    display_events = [
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_detail", "step_id": runner.NEW_STEPS[0], "payload": {"summary": "VPC"}},
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {
            "type": "candidate_detail",
            "step_id": runner.NEW_STEPS[0],
            "payload": {"summary": "仅创建一个安全组"},
        },
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selected", "step_id": runner.NEW_STEPS[0]},
    ]
    monkeypatch.setattr(runner, "_all_event_values", lambda _path: [])
    monkeypatch.setattr(runner, "_read_json_lines", lambda _path: repl_events)
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: display_events)

    runner.apply_profile_acceptance(runtime)

    assert runtime.checks == {
        "REPL invalid candidate preceded replacement intent": True,
        "REPL replacement reran Step 1 and produced selectable candidates": True,
        "REPL replacement candidate reflects the new security-group target": True,
        "REPL progress follows three-step state machine": True,
    }


def test_repl_step2_parameter_acceptance_requires_both_questions_after_selection(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(
            profile="step2_parameter", surface=runner.Surface.REPL, multimodal=False, cloud_write=False
        ),
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        events_path=tmp_path / "events.jsonl",
        checks={},
    )
    repl_events = [
        {"type": "candidate-enter"},
        {"type": "expect", "description": "Step 2 VPC parameter question input ready"},
        {"type": "expect", "description": "Step 2 zone parameter question input ready"},
    ]
    display_events = [
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {"type": "step_started", "step_id": runner.NEW_STEPS[1]},
    ]
    monkeypatch.setattr(runner, "_all_event_values", lambda _path: [])
    monkeypatch.setattr(runner, "_read_json_lines", lambda _path: repl_events)
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: display_events)

    runner.apply_profile_acceptance(runtime)

    assert runtime.checks == {
        "deployment parameters were requested only after Step 2 started": True,
        "REPL progress follows three-step state machine": True,
    }


def test_repl_initial_input_is_retried_until_history_acknowledges_it(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    text = "请创建测试网络"
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=config_dir),
        spec=argparse.Namespace(),
    )
    events: list[dict[str, object]] = []

    class Pty:
        def __init__(self) -> None:
            self.submissions = 0
            self.events = events
            self.pending_text = ""

        def send(self, submitted: str, *, label: str) -> None:
            if label.startswith("initial-input-paste-"):
                attempt = int(label.rsplit("-", 1)[1])
                assert attempt == self.submissions + 1
                assert submitted == f"\x1b[200~{text}\x1b[201~"
                self.pending_text = text
                return
            self.submissions += 1
            assert label == f"initial-input-enter-{self.submissions}"
            assert submitted == "\r"
            if self.submissions == 2 and self.pending_text:
                (config_dir / ".input_history").write_text(
                    json.dumps({"format": "iac-code-input-history-v1", "text": text}) + "\n",
                    encoding="utf-8",
                )

        def drain_output(self) -> None:
            return None

    pty = Pty()
    clock = {"value": 0.0}

    def monotonic() -> float:
        clock["value"] += 1.0
        return clock["value"]

    monkeypatch.setattr(runner, "_initial_prompt", lambda _runtime: text)
    monkeypatch.setattr(runner.time, "monotonic", monotonic)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    runner._repl_submit_initial_prompt(pty, runtime)

    assert pty.submissions == 2
    assert events[0]["type"] == "initial-input-accepted"
    assert events[0]["attempt"] == 2
