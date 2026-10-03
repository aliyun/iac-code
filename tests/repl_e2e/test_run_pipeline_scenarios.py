from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "repl" / "e2e" / "run_pipeline_scenarios.py"
    spec = importlib.util.spec_from_file_location("run_pipeline_scenarios", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _repl_pty_unit_instance(runner, *, args, run_dir: Path, cwd: Path, env: dict[str, str]):
    pty = runner.ReplPty.__new__(runner.ReplPty)
    pty.args = args
    pty.run_dir = run_dir
    pty.cwd = cwd
    pty.env = env
    pty.events = []
    pty.raw_chunks = []
    pty.child = None
    pty._live_transcript = False
    return pty


def test_repl_terminate_records_preexisting_child_exit_status(tmp_path: Path) -> None:
    runner = _load_runner()
    pty = _repl_pty_unit_instance(runner, args=None, run_dir=tmp_path, cwd=tmp_path, env={})

    class Child:
        before = "process output"
        exitstatus = 3
        signalstatus = None

        @staticmethod
        def isalive() -> bool:
            return False

        @staticmethod
        def terminate(*, force: bool) -> None:
            assert force is True

    pty.child = Child()

    pty.terminate()

    assert pty.events == [
        {
            "type": "terminate",
            "force": False,
            "aliveBeforeTerminate": False,
            "exitStatus": 3,
            "signalStatus": None,
            "at": pty.events[0]["at"],
        }
    ]


def _install_flow_fake_pty(
    monkeypatch,
    runner,
    transcript: str,
    actions: list[tuple[str, str]],
    *,
    scenario: str = "scenario1",
) -> None:
    class FakePty:
        def __init__(self, *, args, run_dir, cwd, env):
            self.args = args
            self.run_dir = run_dir
            self.cwd = cwd
            self.env = env
            self.events = []
            self.transcript = transcript
            if "first-stack-id" in transcript:
                self.cleanup_ledger = {
                    "observed_resources": [
                        {
                            "provider": "ros",
                            "resource_type": "stack",
                            "resource_id": "first-stack-id",
                            "resource_name": runner._cleanup_stack_name(run_dir, "first"),
                        },
                        {
                            "provider": "ros",
                            "resource_type": "stack",
                            "resource_id": "second-stack-id",
                            "resource_name": runner._cleanup_stack_name(run_dir, "second"),
                        },
                    ],
                    "cleanup_resources": [
                        {
                            "provider": "ros",
                            "resource_type": "stack",
                            "resource_id": "first-stack-id",
                            "cleanup_required": True,
                            "cleanup_status": "completed",
                            "progress_status": "DELETE_COMPLETE",
                        }
                    ],
                    "history": [
                        {"type": "cleanup_started", "resource": {"resource_id": "first-stack-id"}},
                        {"type": "cleanup_completed", "resource": {"resource_id": "first-stack-id"}},
                    ],
                }
                self.ros_stack_states = {
                    "first-stack-id": {
                        "status": "DELETE_COMPLETE",
                        "not_found": False,
                        "stack_name": runner._cleanup_stack_name(run_dir, "first"),
                    },
                    "second-stack-id": {
                        "status": "CREATE_COMPLETE",
                        "not_found": False,
                        "stack_name": runner._cleanup_stack_name(run_dir, "second"),
                    },
                }
            elif "vsw-" in transcript or "交换机 ID" in transcript:
                stack_name = runner._scenario_stack_name(run_dir, scenario)
                self.cleanup_ledger = {
                    "observed_resources": [
                        {
                            "provider": "ros",
                            "resource_type": "stack",
                            "resource_id": "normal-stack-id",
                            "resource_name": stack_name,
                            "observed_action": "CreateStack",
                        }
                    ]
                }
                self.ros_stack_states = {
                    "normal-stack-id": {
                        "status": "CREATE_COMPLETE",
                        "not_found": False,
                        "stack_name": stack_name,
                    }
                }

        def spawn(self, *, extra_args=None):
            actions.append(("spawn", " ".join(extra_args or [])))
            command = ["uv", "run", "python", "-m", "iac_code.cli.main"]
            if extra_args:
                command.extend(extra_args)
            self.events.append({"type": "spawn", "command": command, "transcript_offset": 0})

        def sendline(self, text):
            actions.append(("sendline", text))
            offset = self.transcript.find(text)
            if offset < 0 and text == self.args.rollback_prompt:
                offset = self.transcript.find("● Intent parsing (1/5)")
            if offset < 0 and text == self.args.ask_answer:
                offset = self.transcript.find("● Confirm and select (4/5)")
            self.events.append({"type": "sendline", "text": text, "transcript_offset": max(offset, 0)})

        def expect_any(self, patterns, *, description, timeout, state_check=None):
            actions.append(("expect", description))
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            actions.append(("expect_optional", description))
            if description == "second ask question after image answer":
                return False
            return True

        def send(self, text, *, label="send"):
            actions.append((label, text))
            self.events.append({"type": label, "text": text, "transcript_offset": 0})

        def paste_image_fixture(self, image_key: str):
            actions.append(("paste-image-fixture", image_key))
            self.events.append(
                {
                    "type": "paste-image-fixture",
                    "image_key": image_key,
                    "path": f"/repo/scripts/a2a/e2e/fixtures/text-images/{image_key}.png",
                    "transcript_offset": 0,
                }
            )

        def terminate(self, *, force=False):
            actions.append(("terminate", str(force)))
            self.events.append({"type": "terminate", "force": force})

    deleted_stack_ids: list[str] = []

    def fake_fresh_ros_stack_state(_pty, stack_id: str) -> dict[str, object]:
        if stack_id == "normal-stack-id":
            stack_name = runner._scenario_stack_name(_pty.run_dir, scenario)
            return {
                "status": "CREATE_COMPLETE",
                "not_found": False,
                "stack_name": stack_name,
                "region_id": "cn-hangzhou",
            }
        return {"status": "DELETE_COMPLETE", "not_found": False}

    def fake_delete_ros_stack(*, stack_id: str, region_id: str, redaction_env: dict[str, str] | None) -> None:
        deleted_stack_ids.append(stack_id)

    monkeypatch.setattr(runner, "_fresh_ros_stack_state", fake_fresh_ros_stack_state)
    monkeypatch.setattr(runner, "_delete_ros_stack", fake_delete_ros_stack)
    monkeypatch.setattr(
        runner,
        "_wait_for_ros_stack_deleted",
        lambda *, pty, stack_id, timeout: {"status": "DELETE_COMPLETE", "not_found": False},
    )
    monkeypatch.setattr(runner, "ReplPty", FakePty)


def _install_cleanup_teardown_fakes(monkeypatch, runner, run_dir: Path) -> list[str]:
    deleted_stack_ids: list[str] = []

    def fake_fresh_ros_stack_state(_pty, stack_id: str) -> dict[str, object]:
        if stack_id == "first-stack-id":
            return {
                "status": "DELETE_COMPLETE",
                "not_found": False,
                "stack_name": runner._cleanup_stack_name(run_dir, "first"),
            }
        return {
            "status": "CREATE_COMPLETE",
            "not_found": False,
            "stack_name": runner._cleanup_stack_name(run_dir, "second"),
            "region_id": "cn-hangzhou",
        }

    def fake_delete_ros_stack(*, stack_id: str, region_id: str, redaction_env: dict[str, str] | None) -> None:
        assert region_id == "cn-hangzhou"
        assert redaction_env is not None
        deleted_stack_ids.append(stack_id)

    monkeypatch.setattr(runner, "_fresh_ros_stack_state", fake_fresh_ros_stack_state)
    monkeypatch.setattr(runner, "_delete_ros_stack", fake_delete_ros_stack)
    monkeypatch.setattr(runner, "_discover_owned_cleanup_stack_ids", lambda _run_dir: [])
    monkeypatch.setattr(
        runner,
        "_wait_for_ros_stack_deleted",
        lambda *, pty, stack_id, timeout: {"status": "DELETE_COMPLETE", "not_found": False},
    )
    return deleted_stack_ids


def _install_observed_stack_teardown_fakes(
    monkeypatch,
    runner,
    *,
    stack_name: str = "vswitch-in-existing-vpc",
) -> list[str]:
    deleted_stack_ids: list[str] = []

    def fake_fresh_ros_stack_state(_pty, stack_id: str) -> dict[str, object]:
        return {
            "status": "CREATE_COMPLETE",
            "not_found": False,
            "stack_name": stack_name,
            "region_id": "cn-hangzhou",
        }

    def fake_delete_ros_stack(*, stack_id: str, region_id: str, redaction_env: dict[str, str] | None) -> None:
        assert region_id == "cn-hangzhou"
        assert redaction_env is not None
        deleted_stack_ids.append(stack_id)

    monkeypatch.setattr(runner, "_fresh_ros_stack_state", fake_fresh_ros_stack_state)
    monkeypatch.setattr(runner, "_delete_ros_stack", fake_delete_ros_stack)
    monkeypatch.setattr(
        runner,
        "_wait_for_ros_stack_deleted",
        lambda *, pty, stack_id, timeout: {"status": "DELETE_COMPLETE", "not_found": False},
    )
    return deleted_stack_ids


def test_parse_args_defaults_to_scenario1() -> None:
    runner = _load_runner()

    args = runner.parse_args([])

    assert args.scenario is None
    assert runner._selected_scenarios(args) == ["scenario1"]
    assert args.python == "uv run python"


def test_validate_requires_real_cloud_flag() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--scenario", "scenario1"])

    try:
        runner._validate_scenario_execution(args, "scenario1")
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError("scenario1 should require --allow-real-cloud")


def test_redaction_hides_sensitive_env_values() -> None:
    runner = _load_runner()

    text = "Authorization: Bearer sk-live-secret and token abcdefghijklmnop"
    env = {
        "IAC_CODE_API_KEY": "sk-live-secret",
        "CUSTOM_TOKEN": "abcdefghijklmnop",
        "IAC_CODE_MODEL": "deepseek-v4-flash-0731",
    }

    redacted = runner._redact_sensitive_text(text, env)

    assert "sk-live-secret" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "deepseek-v4-flash-0731" not in redacted
    assert "<redacted>" in redacted


def test_redaction_does_not_hide_ask_scenario_names() -> None:
    runner = _load_runner()

    redacted = runner._redact_sensitive_text("scenario=ask-waiting-resume", {})

    assert redacted == "scenario=ask-waiting-resume"


def test_normalize_transcript_strips_ansi_and_control_noise() -> None:
    runner = _load_runner()

    normalized = runner._normalize_transcript("\x1b[31mPipeline\x1b[0m\r\n❯  hello\x08\x08ok")

    assert "\x1b" not in normalized
    assert "Pipeline" in normalized
    assert "ok" in normalized


def test_build_child_env_sets_pipeline_mode_without_overriding_home(monkeypatch) -> None:
    runner = _load_runner()
    monkeypatch.setenv("HOME", "/Users/example")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", "/custom/iac")
    args = runner.parse_args(["--allow-real-cloud", "--provider", "dashscope", "--model", "custom-model"])

    env = runner._build_child_env(args, "scenario1")

    assert env["HOME"] == "/Users/example"
    assert env["IAC_CODE_CONFIG_DIR"] == "/custom/iac"
    assert env["IAC_CODE_MODE"] == "pipeline"
    assert env["IAC_CODE_PROVIDER"] == "dashscope"
    assert env["IAC_CODE_MODEL"] == "custom-model"
    assert env["PYTHONUTF8"] == "1"


def test_build_child_env_selects_default_model_per_scenario(monkeypatch) -> None:
    runner = _load_runner()
    monkeypatch.setenv("IAC_CODE_MODEL", "configured-model")
    args = runner.parse_args(["--allow-real-cloud"])

    text_env = runner._build_child_env(args, "scenario1")
    image_env = runner._build_child_env(args, "image-initial")

    assert text_env["IAC_CODE_MODEL"] == "deepseek-v4-flash-0731"
    assert image_env["IAC_CODE_MODEL"] == "qwen3.8-max"


def test_repeated_scenarios_are_preserved() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--scenario", "scenario1", "--scenario", "ask-waiting", "--allow-real-cloud"])

    assert runner._selected_scenarios(args) == ["scenario1", "ask-waiting"]


def test_all_regression_scenarios_are_parseable() -> None:
    runner = _load_runner()
    expected = [
        "scenario1",
        "ask-waiting",
        "ask-waiting-resume",
        "image-initial",
        "image-ask-waiting-resume",
        "image-selection-waiting-resume",
        "image-normal-handoff",
        "image-interrupt",
        "selection-waiting-resume",
        "selection-invalid-then-valid",
        "evaluate-resume",
        "rollback-step2",
        "rollback-step3",
        "rollback-step4-selection",
        "rollback-step5-cleanup",
        "rollback-step5-cleanup-recovery",
    ]

    args = runner.parse_args(
        ["--allow-real-cloud", *[item for scenario in expected for item in ("--scenario", scenario)]]
    )

    assert runner._selected_scenarios(args) == expected


def test_repl_image_fixture_paths_reuse_static_pngs() -> None:
    runner = _load_runner()

    for image_key in [
        "initial",
        "ask-first-answer",
        "ask-second-answer",
        "selection",
        "normal-followup",
        "rollback-interrupt",
    ]:
        path = runner._text_image_fixture_path(image_key)
        assert path.is_file()
        assert path.suffix == ".png"
        assert path.parent.name == "text-images"


def test_run_dir_requires_single_scenario() -> None:
    runner = _load_runner()

    try:
        runner.main(
            [
                "--scenario",
                "scenario1",
                "--scenario",
                "ask-waiting",
                "--allow-real-cloud",
                "--run-dir",
                "/tmp/repl-e2e",
            ]
        )
    except SystemExit as exc:
        assert "--run-dir can only be used with a single --scenario" in str(exc)
    else:
        raise AssertionError("--run-dir should reject multiple scenarios")


def test_write_result_writes_summary_and_transcripts(tmp_path: Path) -> None:
    runner = _load_runner()
    result = runner.ScenarioRunResult(
        scenario="scenario1",
        run_dir=str(tmp_path),
        passed=True,
        checks={"pipeline started": True},
        elapsed_seconds=1.25,
    )

    runner._write_run_artifacts(
        run_dir=tmp_path,
        env={"IAC_CODE_API_KEY": "sk-secret123456", "IAC_CODE_MODEL": "deepseek-v4-flash-0731"},
        raw_transcript="hello sk-secret123456",
        events=[{"type": "check", "name": "pipeline started", "passed": True}],
        result=result,
    )

    summary = (tmp_path / "summary.json").read_text(encoding="utf-8")
    raw = (tmp_path / "transcript.raw.log").read_text(encoding="utf-8")
    normalized = (tmp_path / "transcript.normalized.log").read_text(encoding="utf-8")
    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")

    assert "sk-secret123456" not in summary
    assert "sk-secret123456" not in raw
    assert "sk-secret123456" not in normalized
    assert "pipeline started" in events


def test_initial_prompt_wait_does_not_match_generic_angle_bracket() -> None:
    runner = _load_runner()
    observed_patterns: list[tuple[str, ...]] = []

    class FakePty:
        def expect_any(self, patterns, *, description, timeout):
            observed_patterns.append(patterns)
            return patterns[0]

    args = runner.parse_args(["--allow-real-cloud"])

    runner._expect_initial_prompt(FakePty(), args)

    assert r"❯" in observed_patterns[0]
    assert r">" not in observed_patterns[0]
    assert r"iac-code" not in observed_patterns[0]


def test_initial_prompt_waits_for_prompt_toolkit_ready_sequence() -> None:
    runner = _load_runner()
    descriptions: list[str] = []

    class FakePty:
        def expect_any(self, patterns, *, description, timeout):
            descriptions.append(description)
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            descriptions.append(description)
            return True

    args = runner.parse_args(["--allow-real-cloud"])

    runner._expect_initial_prompt(FakePty(), args)

    assert descriptions == ["initial prompt", "prompt input ready"]


def test_candidate_selection_patterns_match_real_repl_heading() -> None:
    runner = _load_runner()
    real_heading = "● Confirm and select (4/5)"

    assert any(re.search(pattern, real_heading) for pattern in runner.CANDIDATE_SELECTION_PATTERNS)


def test_candidate_evaluation_patterns_match_real_repl_heading() -> None:
    runner = _load_runner()
    real_heading = "● Evaluate candidates (3/5)"

    assert any(re.search(pattern, real_heading) for pattern in runner.CANDIDATE_EVALUATION_PATTERNS)


def test_candidate_evaluation_patterns_do_not_match_architecture_plan_text() -> None:
    runner = _load_runner()
    architecture_output = "这是一个简单明确的需求：在已有 VPC 下创建一个 VSwitch，没有设计取舍空间，只给出 1 个方案。"

    assert not any(re.search(pattern, architecture_output) for pattern in runner.CANDIDATE_EVALUATION_PATTERNS)


def test_pipeline_completed_patterns_do_not_match_step_or_candidate_completion() -> None:
    runner = _load_runner()
    non_terminal_text = "\n".join(
        [
            "✓ 已有VPC下新建VSwitch: Completed",
            "Step Architecture planning completed. Conclusion submitted.",
            "参数选择完成，准备进入部署阶段。",
        ]
    )

    assert not any(re.search(pattern, non_terminal_text) for pattern in runner.PIPELINE_COMPLETED_PATTERNS)


def test_pipeline_completed_patterns_match_real_deployment_success() -> None:
    runner = _load_runner()
    terminal_text = "ROS Stack(CreateStack cn-hangzhou)\ncreate-vswitch-stack(...) CREATE_COMPLETE\n✦ 部署成功！"

    assert any(re.search(pattern, terminal_text) for pattern in runner.PIPELINE_COMPLETED_PATTERNS)


def test_first_stack_created_patterns_do_not_match_create_stack_start() -> None:
    runner = _load_runner()

    assert not any(
        re.search(pattern, "● ROS Stack(CreateStack cn-hangzhou)") for pattern in runner.FIRST_STACK_CREATED_PATTERNS
    )
    assert any(
        re.search(pattern, "create-vswitch-stack(...) CREATE_COMPLETE")
        for pattern in runner.FIRST_STACK_CREATED_PATTERNS
    )


def test_create_stack_started_patterns_match_tool_render_not_prompt_text() -> None:
    runner = _load_runner()

    assert any(re.search(pattern, "● ROS Deploy") for pattern in runner.CREATE_STACK_STARTED_PATTERNS)
    assert not any(
        re.search(pattern, "第一次 CreateStack 的 params.StackName 必须精确等于 test")
        for pattern in runner.CREATE_STACK_STARTED_PATTERNS
    )


def test_candidate_selection_patterns_do_not_match_schema_explanation() -> None:
    runner = _load_runner()
    schema_error = (
        "方案名称（体现核心差异） output_path 模板文件路径，Outer argument example: {'conclusion': {'candidates': []}}"
    )

    assert not any(re.search(pattern, schema_error) for pattern in runner.CANDIDATE_SELECTION_PATTERNS)


def test_ask_patterns_match_real_repl_question_prompt() -> None:
    runner = _load_runner()
    real_prompt = "● Ask user question\n请描述你的产品类型、技术栈、预期访问量等信息"

    assert any(re.search(pattern, real_prompt) for pattern in runner.ASK_PATTERNS)


def test_ask_input_ready_patterns_match_answer_prompt_only() -> None:
    runner = _load_runner()
    colored_normal_prompt = "\x1b[1m\x1b[36m❯ \x1b[0m\r\x1b[2C\x1b[>4;2m"

    assert any(re.search(pattern, "\r\n  > ") for pattern in runner.ASK_INPUT_READY_PATTERNS)
    assert not any(re.search(pattern, colored_normal_prompt) for pattern in runner.ASK_INPUT_READY_PATTERNS)
    assert not any(re.search(pattern, "● Ask user question") for pattern in runner.ASK_INPUT_READY_PATTERNS)
    assert not any(re.search(pattern, "  → candidate 1") for pattern in runner.ASK_INPUT_READY_PATTERNS)


def test_candidate_selection_uses_semantic_controls_without_waiting_for_stale_raw_marker() -> None:
    runner = _load_runner()
    descriptions: list[str] = []

    class FakePty:
        def expect_any(self, patterns, *, description, timeout, state_check=None):
            descriptions.append(description)
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            descriptions.append(description)
            return True

    args = runner.parse_args(["--allow-real-cloud"])

    runner._expect_candidate_selection(FakePty(), args, description="candidate selection visible")

    assert descriptions == [
        "candidate selection visible",
        "candidate selection controls ready",
    ]


def test_candidate_selection_falls_back_to_raw_marker_when_semantic_controls_are_missing() -> None:
    runner = _load_runner()
    descriptions: list[str] = []

    class FakePty:
        def expect_any(self, patterns, *, description, timeout, state_check=None):
            descriptions.append(description)
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            descriptions.append(description)
            return False

    args = runner.parse_args(["--allow-real-cloud"])

    runner._expect_candidate_selection(FakePty(), args, description="candidate selection visible")

    assert descriptions == [
        "candidate selection visible",
        "candidate selection controls ready",
        "candidate selection input ready",
    ]


def test_expect_any_auto_approves_permission_prompt(tmp_path: Path) -> None:
    runner = _load_runner()

    class FakeChild:
        def __init__(self) -> None:
            self.calls = 0
            self.sent: list[str] = []
            self.before = ""
            self.after = ""

        def expect(self, patterns, timeout):
            self.calls += 1
            if self.calls == 1:
                self.after = "Yes, allow once"
                return patterns.index(r"Yes, allow once")
            self.after = "Pipeline completed"
            return 0

        def send(self, text):
            self.sent.append(text)

    args = runner.parse_args(["--allow-real-cloud"])
    child = FakeChild()
    pty = _repl_pty_unit_instance(runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={})
    pty.child = child

    matched = pty.expect_any((r"Pipeline completed",), description="pipeline completed", timeout=10)

    assert matched == r"Pipeline completed"
    assert child.sent == ["\x1b[5~\r"]
    assert any(event["type"] == "permission_prompt" for event in pty.events)
    assert any(event["type"] == "permission-prompt-response" for event in pty.events)


def test_expect_any_diagnoses_and_aborts_unexpected_input(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--wait-diagnosis-after", "0"])
    pty = _repl_pty_unit_instance(
        runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={"IAC_CODE_CONFIG_DIR": str(tmp_path)}
    )

    class Child:
        before = ""
        after = ""

        def expect(self, _patterns, timeout):
            pty.raw_chunks.append("● Ask user question: choose a VPC\n")
            raise runner.pexpect.TIMEOUT("waiting")

    pty.child = Child()
    monkeypatch.setattr(
        runner, "diagnose_wait", lambda *_args, **_kwargs: {"state": "waiting_for_input", "confidence": 0.93}
    )

    with pytest.raises(RuntimeError, match="unexpected input"):
        pty.expect_any(("Pipeline completed",), description="pipeline completed", timeout=300)

    assert pty._wait_diagnoses[-1]["action"] == "early_abort"
    assert pty._wait_diagnoses[-1]["cue"] == "ask_question"
    assert pty.events[-1]["type"] == "expect"
    assert pty.events[-1]["passed"] is False


def test_expect_any_keeps_waiting_when_model_cannot_confirm_input(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--wait-diagnosis-after", "0"])
    pty = _repl_pty_unit_instance(
        runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={"IAC_CODE_CONFIG_DIR": str(tmp_path)}
    )

    class Child:
        before = ""
        after = ""
        calls = 0

        def expect(self, _patterns, timeout):
            self.calls += 1
            if self.calls == 1:
                pty.raw_chunks.append("Cloud resource creation is running\n")
                raise runner.pexpect.TIMEOUT("waiting")
            self.after = "Pipeline completed"
            return 0

    pty.child = Child()
    monkeypatch.setattr(
        runner, "diagnose_wait", lambda *_args, **_kwargs: {"state": "normal_operation", "confidence": 0.95}
    )

    matched = pty.expect_any(("Pipeline completed",), description="pipeline completed", timeout=300)

    assert matched == "Pipeline completed"
    assert pty._wait_diagnoses[-1]["action"] == "observe"
    assert pty._wait_diagnoses[-1]["cue"] == "none"


def test_expect_any_does_not_abort_on_repl_prompt_while_pipeline_may_continue(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--wait-diagnosis-after", "0"])
    pty = _repl_pty_unit_instance(
        runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={"IAC_CODE_CONFIG_DIR": str(tmp_path)}
    )

    class Child:
        before = ""
        after = ""
        calls = 0

        def expect(self, _patterns, timeout):
            self.calls += 1
            if self.calls == 1:
                pty.raw_chunks.append("❯\x1b[>4;2m")
                raise runner.pexpect.TIMEOUT("waiting")
            self.after = "Confirm and select (3/5)"
            return 0

    pty.child = Child()
    monkeypatch.setattr(
        runner, "diagnose_wait", lambda *_args, **_kwargs: {"state": "waiting_for_input", "confidence": 0.95}
    )

    assert pty.expect_any(("Confirm and select",), description="candidate selection visible", timeout=300) == (
        "Confirm and select"
    )
    assert pty._wait_diagnoses[-1]["cue"] == "repl_prompt"
    assert pty._wait_diagnoses[-1]["action"] == "observe"


def test_expect_any_aborts_silent_non_cloud_wait_before_stream_timeout(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    pty = _repl_pty_unit_instance(runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={})
    clock = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    class Child:
        def expect(self, _patterns, timeout):
            clock[0] += runner.WAIT_IDLE_SECONDS + 1
            raise runner.pexpect.TIMEOUT("waiting")

    pty.child = Child()
    with pytest.raises(TimeoutError, match="no terminal output"):
        pty.expect_any(("Pipeline completed",), description="pipeline completed", timeout=1800)
    assert pty._wait_diagnoses[-1]["state"] == "no_output"


def test_expect_any_aborts_when_pipeline_finishes_before_first_stack_create(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    config_dir = tmp_path / "config"
    display = config_dir / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True)
    display.write_text('{"type":"pipeline_completed"}\n', encoding="utf-8")
    pty = _repl_pty_unit_instance(
        runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={"IAC_CODE_CONFIG_DIR": str(config_dir)}
    )
    clock = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    class Child:
        def expect(self, _patterns, timeout):
            clock[0] += runner.WAIT_PROGRESS_SECONDS + 1
            raise runner.pexpect.TIMEOUT("waiting")

    pty.child = Child()
    with pytest.raises(RuntimeError, match="pipeline completed before first stack create started"):
        pty.expect_any(("ROS Deploy",), description="first stack create started", timeout=1800)


def test_expect_any_allows_long_cloud_silence(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    pty = _repl_pty_unit_instance(runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={})
    pty.raw_chunks.append("● Deploying (5/5): CreateStack\n")
    clock = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    class Child:
        before = ""
        after = "Pipeline completed"
        calls = 0

        def expect(self, _patterns, timeout):
            self.calls += 1
            if self.calls == 1:
                clock[0] += runner.WAIT_IDLE_SECONDS + 1
                raise runner.pexpect.TIMEOUT("waiting")
            return 0

    pty.child = Child()
    assert pty.expect_any(("Pipeline completed",), description="pipeline completed", timeout=1800) == (
        "Pipeline completed"
    )


@pytest.mark.skipif(os.name == "nt", reason="pexpect PTY requires POSIX")
def test_expect_any_preserves_partial_pty_output_across_poll_timeouts(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    pty = _repl_pty_unit_instance(runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={})
    monkeypatch.setattr(runner, "WAIT_POLL_SECONDS", 0.02)
    child = runner.pexpect.spawn(
        sys.executable,
        ["-u", "-c", "import time; print('first', flush=True); time.sleep(0.15); print('second', flush=True)"],
        encoding="utf-8",
    )
    pty.child = child
    try:
        assert pty.expect_any((r"first\s+second",), description="two chunks", timeout=1) == r"first\s+second"
        assert "first" in pty.transcript
        assert "second" in pty.transcript
    finally:
        child.close(force=True)


def test_permission_prompt_response_sequence_supports_named_keys() -> None:
    runner = _load_runner()

    assert runner._permission_prompt_response_sequence("pageup-enter") == "\x1b[5~\r"
    assert runner._permission_prompt_response_sequence("up-enter") == "\x1b[A\r"
    assert runner._permission_prompt_response_sequence("enter") == "\r"
    assert runner._permission_prompt_response_sequence("1") == "1\r"


def test_repl_pty_sendline_chunks_long_input(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    sent: list[tuple[str, str]] = []
    reads = ["echoed chunk"]

    class FakeChild:
        def send(self, text):
            sent.append(("send", text))

        def sendline(self, text):
            sent.append(("sendline", text))

        def read_nonblocking(self, size, timeout):
            if reads:
                return reads.pop(0)
            raise runner.pexpect.TIMEOUT("done")

    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    pty = _repl_pty_unit_instance(runner, args=args, run_dir=tmp_path, cwd=tmp_path, env={})
    pty.child = FakeChild()

    pty.sendline("x" * (runner.PTY_SEND_CHUNK_SIZE + 1))

    assert [kind for kind, _ in sent] == ["send", "send", "sendline"]
    assert sent[-1] == ("sendline", "")
    assert "echoed chunk" in pty.transcript


def test_cleanup_pipeline_prompt_stays_pty_sized(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    prompt = runner._cleanup_pipeline_prompt(args, tmp_path)

    assert len(prompt) <= runner.PTY_SEND_CHUNK_SIZE


def test_cleanup_pipeline_prompt_forbids_default_stack_name(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    prompt = runner._cleanup_pipeline_prompt(args, tmp_path)

    assert "params.StackName 必须精确等于" in prompt
    assert "vswitch-in-existing-vpc" in prompt
    assert "不能复用已有资源栈" in prompt
    assert "两个不同的合法未占用 VSwitch CIDR" in prompt


def test_stack_creating_prompt_includes_test_owned_stack_name(tmp_path: Path) -> None:
    runner = _load_runner()

    stack_name = runner._scenario_stack_name(tmp_path, "ask-waiting-resume")
    prompt = runner._stack_creating_prompt("创建一个 VSwitch", tmp_path, "ask-waiting-resume")

    assert stack_name.startswith("iac-e2e-")
    assert "ROS 资源栈名称基础名" in prompt
    assert "最终 StackName 必须以该基础名开头" in prompt
    assert stack_name in prompt


def test_cleanup_pipeline_prompt_includes_explicit_network_target(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(
        [
            "--allow-real-cloud",
            "--cleanup-vpc-id",
            "vpc-test",
            "--cleanup-vpc-cidr",
            "172.16.0.0/12",
            "--cleanup-zone-id",
            "cn-hangzhou-h",
            "--cleanup-vswitch-cidr",
            "172.31.255.0/24",
            "--cleanup-rollback-vswitch-cidr",
            "172.31.254.0/24",
        ]
    )

    prompt = runner._cleanup_pipeline_prompt(args, tmp_path)

    assert "固定使用已有 VPC `vpc-test`" in prompt
    assert "VpcId=`vpc-test`" in prompt
    assert "ZoneId=`cn-hangzhou-h`" in prompt
    assert "CidrBlock=`172.31.255.0/24`" in prompt
    assert "172.31.254.0/24" not in prompt
    assert "禁止使用模板默认 CidrBlock" in prompt


def test_cleanup_pipeline_prompt_does_not_ask_llm_to_control_steps(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    prompt = runner._cleanup_pipeline_prompt(args, tmp_path)

    assert "complete_step" not in prompt
    assert "停在部署步骤" not in prompt


def test_find_available_vswitch_cidr_avoids_existing_subnets() -> None:
    runner = _load_runner()

    cidr = runner._find_available_vswitch_cidr(
        "192.168.0.0/16",
        ["192.168.255.0/24", "192.168.254.0/24", "192.168.10.0/24"],
    )

    assert cidr == "192.168.253.0/24"


def test_find_available_vswitch_cidrs_returns_distinct_subnets() -> None:
    runner = _load_runner()

    cidrs = runner._find_available_vswitch_cidrs("192.168.0.0/16", ["192.168.255.0/24"], count=2)

    assert cidrs == ["192.168.254.0/24", "192.168.253.0/24"]


def test_discover_cleanup_network_target_excludes_prior_scenario_cidrs(monkeypatch) -> None:
    runner = _load_runner()

    def call_api(_product: str, action: str, _params: dict[str, object]) -> dict[str, object]:
        if action == "DescribeVpcs":
            return {
                "Vpcs": {
                    "Vpc": [
                        {
                            "VpcId": "vpc-test",
                            "CidrBlock": "192.168.0.0/16",
                            "Status": "Available",
                        }
                    ]
                }
            }
        return {
            "VSwitches": {
                "VSwitch": [
                    {
                        "VSwitchId": "vsw-existing",
                        "CidrBlock": "192.168.10.0/24",
                        "Status": "Available",
                        "ZoneId": "cn-hangzhou-k",
                    }
                ]
            }
        }

    monkeypatch.setattr(runner, "_call_aliyun_api", call_api)

    target = runner._discover_cleanup_network_target(excluded_cidrs={"192.168.255.0/24", "192.168.254.0/24"})

    assert target.vswitch_cidr == "192.168.253.0/24"
    assert target.rollback_vswitch_cidr == "192.168.252.0/24"


def test_wait_for_cleanup_resource_status_drains_pty_while_polling(monkeypatch) -> None:
    runner = _load_runner()

    class FakePty:
        def __init__(self) -> None:
            self.drain_calls = 0

        def drain_output(self) -> None:
            self.drain_calls += 1

    pty = FakePty()

    def cleanup_resource(_pty, _stack_id: str) -> dict[str, str]:
        return {"cleanup_status": "completed" if pty.drain_calls >= 2 else "in_progress"}

    monkeypatch.setattr(runner, "_cleanup_resource_for_stack", cleanup_resource)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    runner._wait_for_cleanup_resource_status(pty, "stack-test", {"completed"}, timeout=1)

    assert pty.drain_calls == 2


def test_call_aliyun_api_uses_runtime_services_with_body_only_result(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    from iac_code import config
    from iac_code.services import cloud_credentials
    from iac_code.tools import base
    from iac_code.tools.cloud.aliyun import aliyun_api, runtime

    credential = type("Credential", (), {"mode": "AK", "region_id": "cn-test"})()

    class FakeCloudCredentials:
        def get_provider(self, provider: str):
            assert provider == "aliyun"
            return credential

    class FakeServices:
        credential_provider = None
        default_region_provider = None

        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    services = FakeServices()

    class FakeAliyunApi:
        name = "aliyun_api"

        def __init__(self, *, services: FakeServices) -> None:
            self.services = services

        def prepare_invocation_input(self, tool_input):
            assert self.services.default_region_provider() == "cn-test"
            return {**tool_input, "region_id": "cn-test"}

        async def check_permissions(self, tool_input, context):
            assert tool_input["region_id"] == "cn-test"
            assert context.invocation_binding.tool_name == self.name
            return type(
                "Permission",
                (),
                {
                    "behavior": "ask",
                    "message": "",
                    "snapshot_id": "snapshot-test",
                    "security_digest": "digest-test",
                    "execution_class": "concurrent",
                },
            )()

        async def execute(self, *, tool_input, context):
            assert tool_input == {
                "product": "vpc",
                "action": "DescribeVpcs",
                "params": {"PageSize": 50},
                "region_id": "cn-test",
            }
            assert isinstance(context, base.ToolContext)
            assert self.services.credential_provider() is credential
            assert context.snapshot_id == "snapshot-test"
            assert context.security_digest == "digest-test"
            assert context.execution_class == "concurrent"
            return base.ToolResult(content=runner.json.dumps({"Vpcs": {"Vpc": [{"VpcId": "vpc-test"}]}}))

    monkeypatch.setattr(config, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cloud_credentials, "CloudCredentials", FakeCloudCredentials)
    monkeypatch.setattr(runtime, "create_aliyun_runtime_services", lambda **_: services)
    monkeypatch.setattr(aliyun_api, "AliyunApi", FakeAliyunApi)

    result = runner._call_aliyun_api("vpc", "DescribeVpcs", {"PageSize": 50})

    assert result == {"Vpcs": {"Vpc": [{"VpcId": "vpc-test"}]}}
    assert services.closed is True


def test_cleanup_ledger_path_falls_back_from_stale_transcript_session(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    from iac_code.services.session_storage import SessionStorage

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    storage = SessionStorage()
    expected = Path(storage.session_dir(str(cwd), "22222222-2222-2222-2222-222222222222")) / "pipeline" / "cleanup.yaml"
    expected.parent.mkdir(parents=True)
    expected.write_text("schema_version: 1\n", encoding="utf-8")

    pty = type(
        "Pty",
        (),
        {
            "cwd": cwd,
            "transcript": "Session: 11111111-1111-1111-1111-111111111111",
        },
    )()

    assert runner._cleanup_ledger_path(pty) == expected


def test_cleanup_rollback_prompt_forces_second_stack_name(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    prompt = runner._cleanup_rollback_prompt(args, tmp_path)

    assert args.rollback_prompt in prompt
    assert runner._cleanup_stack_name(tmp_path, "second") in prompt
    assert "vswitch-in-existing-vpc" in prompt
    assert "不能复用已有资源栈" in prompt
    assert "只创建安全组，不创建 VSwitch" in prompt


def test_cleanup_rollback_prompt_uses_only_rollback_network_target(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(
        [
            "--allow-real-cloud",
            "--cleanup-vpc-id",
            "vpc-test",
            "--cleanup-vpc-cidr",
            "172.16.0.0/12",
            "--cleanup-zone-id",
            "cn-hangzhou-h",
            "--cleanup-vswitch-cidr",
            "172.31.255.0/24",
            "--cleanup-rollback-vswitch-cidr",
            "172.31.254.0/24",
        ]
    )

    prompt = runner._cleanup_rollback_prompt(args, tmp_path)

    assert "本次重新部署只创建安全组" in prompt
    assert "VpcId=`vpc-test`" in prompt
    assert "禁止创建 VSwitch" in prompt
    assert "禁止在第二个栈中使用 CidrBlock" in prompt


def test_permission_prompt_patterns_only_match_approval_options() -> None:
    runner = _load_runner()

    assert any("allow" in pattern.lower() or "允许" in pattern for pattern in runner.PERMISSION_PROMPT_PATTERNS)
    assert not any("reject" in pattern.lower() or "拒绝" in pattern for pattern in runner.PERMISSION_PROMPT_PATTERNS)


def test_acceptance_rejects_rollback_echo_without_post_rollback_output() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    rollback_offset = len("● Evaluate candidates (3/5)\n✎ ")

    class FakePty:
        transcript = "● Evaluate candidates (3/5)\n✎ 回退到 intent_parsing，选择一个已有vpc，创建一个安全组\n"
        events = [
            {"type": "send-esc"},
            {"type": "sendline", "text": args.rollback_prompt, "transcript_offset": rollback_offset},
        ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step3", args, FakePty(), checks)

    assert checks["acceptance: rollback reached evaluate_candidates step"] is True
    assert checks["acceptance: rollback produced post-interrupt pipeline progress"] is False


def test_acceptance_allows_rollback_when_pipeline_restarts_after_prompt() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    before_rollback = (
        "● Evaluate candidates (3/5)\n"
        "\x1b[?2004h\x1b[?1004h\x1b[>1u\x1b[>4;2m"
        "\x1b[>4;0m\x1b[<u\x1b[?1004l\x1b[?2004l\n"
        "回退到 intent_parsing，选择一个已有vpc，创建一个安全组\n"
    )

    class FakePty:
        transcript = (
            before_rollback
            + "\x1b[?2004h\x1b[?1004h\x1b[>1u\x1b[>4;2m"
            + "● Intent parsing (1/5)\nStep Intent parsing completed. Conclusion submitted.\n"
        )
        events = [
            {"type": "send-esc"},
            {"type": "sendline", "text": args.rollback_prompt, "transcript_offset": len(before_rollback)},
        ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step3", args, FakePty(), checks)

    assert checks["acceptance: rollback reached evaluate_candidates step"] is True
    assert checks["acceptance: rollback produced post-interrupt pipeline progress"] is True


def test_acceptance_records_step2_rollback_restart() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    before_rollback = "● Architecture planning (2/5)\n✎ " + args.rollback_prompt + "\n"

    class FakePty:
        transcript = before_rollback + "● Intent parsing (1/5)\n"
        events = [
            {"type": "send-esc"},
            {"type": "sendline", "text": args.rollback_prompt, "transcript_offset": len(before_rollback)},
        ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step2", args, FakePty(), checks)

    assert checks["acceptance: rollback reached architecture_planning step"] is True
    assert checks["acceptance: rollback produced post-interrupt pipeline progress"] is True


def test_acceptance_records_step4_rollback_restart() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    before_rollback = "● Confirm and select (4/5)\n✎ " + args.rollback_prompt + "\n"

    class FakePty:
        transcript = before_rollback + "● Intent parsing (1/5)\n"
        events = [
            {"type": "send-esc"},
            {"type": "sendline", "text": args.rollback_prompt, "transcript_offset": len(before_rollback)},
        ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step4-selection", args, FakePty(), checks)

    assert checks["acceptance: rollback reached candidate selection step"] is True
    assert checks["acceptance: rollback produced post-interrupt pipeline progress"] is True


def test_acceptance_records_evaluate_resume() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    fake_transcript = (
        "● Evaluate candidates (3/5)\n"
        "● Evaluate candidates (3/5)\n" + args.evaluate_resume_continue_prompt + "\n"
        "● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n"
    )

    class FakePty:
        transcript = fake_transcript
        events = [
            {"type": "spawn", "command": ["uv", "run", "python"]},
            {"type": "terminate", "force": True},
            {"type": "spawn", "command": ["uv", "run", "python", "--continue"]},
            {
                "type": "sendline",
                "text": args.evaluate_resume_continue_prompt,
                "transcript_offset": fake_transcript.find(args.evaluate_resume_continue_prompt),
            },
        ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("evaluate-resume", args, FakePty(), checks)

    assert checks["acceptance: evaluate_candidates was shown before resume"] is True
    assert checks["acceptance: evaluate_candidates was replayed after resume"] is True
    assert checks["acceptance: resume used --continue"] is True
    assert checks["acceptance: resume continue input was sent"] is True
    assert checks["acceptance: pipeline advanced after resume continue"] is True


def test_acceptance_records_ask_waiting_resume() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    fake_transcript = "● Ask user question\n● Ask user question\n" + args.ask_answer + "\n● Confirm and select (4/5)\n"

    class FakePty:
        transcript = fake_transcript
        events = [
            {"type": "spawn", "command": ["uv", "run", "python"]},
            {"type": "terminate", "force": True},
            {"type": "spawn", "command": ["uv", "run", "python", "--continue"]},
            {"type": "sendline", "text": args.ask_answer, "transcript_offset": fake_transcript.find(args.ask_answer)},
        ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("ask-waiting-resume", args, FakePty(), checks)

    assert checks["acceptance: ask user question was replayed after resume"] is True
    assert checks["acceptance: resume used --continue"] is True
    assert checks["acceptance: ask answer advanced pipeline after resume"] is True


def test_acceptance_records_invalid_selection_then_valid_completion() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    class FakePty:
        transcript = "● Confirm and select (4/5)\n✔ Pipeline completed\n交换机 ID   vsw-bp1234567890\n"
        events = [
            {"type": "select-invalid-candidate", "text": "9"},
            {"type": "select-default-candidate", "text": "1\r"},
        ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("selection-invalid-then-valid", args, FakePty(), checks)

    assert checks["acceptance: invalid selection input was sent"] is True
    assert checks["acceptance: valid selection input was sent after invalid input"] is True
    assert checks["acceptance: pipeline completed"] is True


def test_acceptance_records_rollback_step5_cleanup_completion() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    run_path = Path("/tmp/20260101T000000Z-1-abc12345")

    class FakePty:
        run_dir = run_path
        transcript = (
            "● Deploying (5/5)\n"
            "first-stack(first-stack-id) CREATE_COMPLETE\n"
            "检测到 1 个回滚残留资源，开始清理流程。\n"
            "↺ 回滚清理 [完成] first-stack · 资源栈 first-stack-id · DELETE_COMPLETE\n"
            "second-stack(second-stack-id) CREATE_COMPLETE\n"
        )
        events: list[dict[str, object]] = []
        cleanup_first_stack_id = "first-stack-id"
        cleanup_second_stack_id = "second-stack-id"
        cleanup_ledger = {
            "observed_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "first-stack-id",
                    "resource_name": runner._cleanup_stack_name(run_path, "first"),
                },
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "second-stack-id",
                    "resource_name": runner._cleanup_stack_name(run_path, "second"),
                },
            ],
            "cleanup_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "first-stack-id",
                    "cleanup_required": True,
                    "cleanup_status": "completed",
                    "progress_status": "DELETE_COMPLETE",
                }
            ],
        }
        ros_stack_states = {
            "first-stack-id": {"status": "DELETE_COMPLETE", "not_found": False},
            "second-stack-id": {"status": "CREATE_COMPLETE", "not_found": False},
        }

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step5-cleanup", args, FakePty(), checks)

    assert checks["acceptance: first rollback stack observed"] is True
    assert checks["acceptance: rollback cleanup ledger includes first stack"] is True
    assert checks["acceptance: second stack created after rollback"] is True
    assert checks["acceptance: first rollback stack name matches test stack"] is True
    assert checks["acceptance: second stack name matches test stack"] is True
    assert checks["acceptance: cleanup snapshot does not target second stack"] is True
    assert checks["acceptance: rollback cleanup completed"] is True
    assert checks["acceptance: no ROS create failure in cleanup transcript"] is True
    assert checks["acceptance: ROS first rollback stack deleted"] is True
    assert checks["acceptance: ROS second stack retained"] is True


def test_acceptance_rejects_rollback_step5_create_failed_transcript() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    run_path = Path("/tmp/20260101T000000Z-1-abc12345")

    class FakePty:
        run_dir = run_path
        transcript = (
            "● Deploying (5/5)\n"
            "first-stack(first-stack-id) CREATE_FAILED: RouteConflict.AlreadyExist\n"
            "检测到 1 个回滚残留资源，开始清理流程。\n"
            "↺ 回滚清理 [完成] first-stack · 资源栈 first-stack-id · DELETE_COMPLETE\n"
            "second-stack(second-stack-id) CREATE_COMPLETE\n"
        )
        events: list[dict[str, object]] = []
        cleanup_first_stack_id = "first-stack-id"
        cleanup_second_stack_id = "second-stack-id"
        cleanup_ledger = {
            "observed_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "first-stack-id",
                    "resource_name": runner._cleanup_stack_name(run_path, "first"),
                },
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "second-stack-id",
                    "resource_name": runner._cleanup_stack_name(run_path, "second"),
                },
            ],
            "cleanup_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "first-stack-id",
                    "cleanup_required": True,
                    "cleanup_status": "completed",
                    "progress_status": "DELETE_COMPLETE",
                }
            ],
        }
        ros_stack_states = {
            "first-stack-id": {"status": "DELETE_COMPLETE", "not_found": False},
            "second-stack-id": {"status": "CREATE_COMPLETE", "not_found": False},
        }

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step5-cleanup", args, FakePty(), checks)

    assert checks["acceptance: no ROS create failure in cleanup transcript"] is False


def test_acceptance_records_rollback_step5_cleanup_recovery() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    run_path = Path("/tmp/20260101T000000Z-1-abc12345")

    class FakePty:
        run_dir = run_path
        transcript = (
            "● Deploying (5/5)\n"
            "first-stack(first-stack-id) CREATE_COMPLETE\n"
            "检测到 1 个回滚残留资源，开始清理流程。\n"
            "↺ 回滚清理恢复：1 条记录，1 条进行中。\n"
            "↺ 回滚清理 [完成] first-stack · 资源栈 first-stack-id · DELETE_COMPLETE\n"
            "second-stack(second-stack-id) CREATE_COMPLETE\n"
        )
        events = [
            {"type": "terminate", "force": True},
            {"type": "spawn", "command": ["uv", "run", "python", "--continue"]},
            {"type": "sendline", "text": args.cleanup_continue_prompt},
        ]
        cleanup_first_stack_id = "first-stack-id"
        cleanup_second_stack_id = "second-stack-id"
        cleanup_ledger = {
            "observed_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "first-stack-id",
                    "resource_name": runner._cleanup_stack_name(run_path, "first"),
                },
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "second-stack-id",
                    "resource_name": runner._cleanup_stack_name(run_path, "second"),
                },
            ],
            "cleanup_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "first-stack-id",
                    "cleanup_required": True,
                    "cleanup_status": "completed",
                    "progress_status": "DELETE_COMPLETE",
                }
            ],
            "history": [
                {"type": "cleanup_started", "resource": {"resource_id": "first-stack-id"}},
                {"type": "cleanup_completed", "resource": {"resource_id": "first-stack-id"}},
            ],
        }
        ros_stack_states = {
            "first-stack-id": {"status": "DELETE_COMPLETE", "not_found": False},
            "second-stack-id": {"status": "CREATE_COMPLETE", "not_found": False},
        }

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step5-cleanup-recovery", args, FakePty(), checks)

    assert checks["acceptance: cleanup process was killed"] is True
    assert checks["acceptance: cleanup resume used --continue"] is True
    assert checks["acceptance: cleanup retriggered after restart"] is True
    assert checks["acceptance: rollback cleanup completed"] is True
    assert checks["acceptance: ROS first rollback stack deleted"] is True
    assert checks["acceptance: ROS second stack retained"] is True


def test_cleanup_final_teardown_deletes_owned_second_stack(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])

    class FakePty:
        run_dir = tmp_path
        env: dict[str, str] = {"ALIBABA_CLOUD_REGION_ID": "cn-hangzhou"}
        cleanup_first_stack_id = "first-stack-id"
        cleanup_second_stack_id = "second-stack-id"
        cleanup_ledger = {
            "observed_resources": [
                {"provider": "ros", "resource_type": "stack", "resource_id": "first-stack-id"},
                {"provider": "ros", "resource_type": "stack", "resource_id": "second-stack-id"},
            ]
        }

    deleted_stack_ids = _install_cleanup_teardown_fakes(monkeypatch, runner, tmp_path)
    checks: dict[str, bool] = {}
    notes: list[str] = []

    runner._teardown_cleanup_scenario_resources(
        args=args,
        scenario="rollback-step5-cleanup",
        pty=FakePty(),
        checks=checks,
        notes=notes,
    )

    assert deleted_stack_ids == ["second-stack-id"]
    assert checks["teardown: cleanup scenario owned ROS stacks deleted"] is True


def test_cleanup_final_teardown_discovers_stack_missing_from_ledger(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    first_name = runner._cleanup_stack_name(tmp_path, "first")
    deleted: list[str] = []

    class FakePty:
        run_dir = tmp_path
        env: dict[str, str] = {"ALIBABA_CLOUD_REGION_ID": "cn-hangzhou"}
        cleanup_ledger = {"observed_resources": []}

    monkeypatch.setattr(runner, "_discover_owned_cleanup_stack_ids", lambda _run_dir: ["first-stack-id"])
    monkeypatch.setattr(
        runner, "_fresh_ros_stack_state",
        lambda _pty, _id: {
            "status": "CREATE_COMPLETE", "not_found": False,
            "stack_name": first_name, "region_id": "cn-hangzhou",
        },
    )
    monkeypatch.setattr(runner, "_delete_ros_stack", lambda **kwargs: deleted.append(kwargs["stack_id"]))
    monkeypatch.setattr(
        runner, "_wait_for_ros_stack_deleted",
        lambda **_kwargs: {"status": "DELETE_COMPLETE", "not_found": False},
    )
    checks: dict[str, bool] = {}
    runner._teardown_cleanup_scenario_resources(
        args=args, scenario="rollback-step5-cleanup", pty=FakePty(), checks=checks, notes=[]
    )

    assert deleted == ["first-stack-id"]
    assert checks["teardown: owned ROS Stack discovery succeeded"] is True
    assert checks["teardown: cleanup scenario owned ROS stacks deleted"] is True


def test_cleanup_stack_discovery_matches_exact_run_owned_names(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    first_name = runner._cleanup_stack_name(tmp_path, "first")
    second_name = runner._cleanup_stack_name(tmp_path, "second")
    requested: list[str] = []

    def fake_call(_product: str, _action: str, params: dict) -> dict:
        requested.extend(params["StackName"])
        return {"Stacks": [
            {"StackName": first_name, "StackId": "first-stack-id", "Status": "CREATE_COMPLETE"},
            {"StackName": second_name, "StackId": "deleted-stack-id", "Status": "DELETE_COMPLETE"},
            {"StackName": "other-stack", "StackId": "other-stack-id", "Status": "CREATE_COMPLETE"},
        ]}

    monkeypatch.setattr(runner, "_call_aliyun_api", fake_call)
    assert runner._discover_owned_cleanup_stack_ids(tmp_path) == ["first-stack-id"]
    assert requested == sorted({first_name, second_name})


def test_cleanup_final_teardown_refuses_unowned_stack_name(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])

    class FakePty:
        run_dir = tmp_path
        env: dict[str, str] = {"ALIBABA_CLOUD_REGION_ID": "cn-hangzhou"}
        cleanup_first_stack_id = "first-stack-id"
        cleanup_second_stack_id = "second-stack-id"
        cleanup_ledger = {
            "observed_resources": [
                {"provider": "ros", "resource_type": "stack", "resource_id": "first-stack-id"},
                {"provider": "ros", "resource_type": "stack", "resource_id": "second-stack-id"},
            ]
        }

    def fake_fresh_ros_stack_state(_pty, stack_id: str) -> dict[str, object]:
        if stack_id == "first-stack-id":
            return {"status": "DELETE_COMPLETE", "not_found": False}
        return {"status": "CREATE_COMPLETE", "not_found": False, "stack_name": "vswitch-in-existing-vpc"}

    monkeypatch.setattr(runner, "_fresh_ros_stack_state", fake_fresh_ros_stack_state)
    monkeypatch.setattr(runner, "_delete_ros_stack", lambda **_kwargs: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(runner, "_discover_owned_cleanup_stack_ids", lambda _run_dir: [])

    checks: dict[str, bool] = {}
    notes: list[str] = []

    runner._teardown_cleanup_scenario_resources(
        args=args,
        scenario="rollback-step5-cleanup",
        pty=FakePty(),
        checks=checks,
        notes=notes,
    )

    assert checks["teardown: cleanup scenario owned ROS stacks deleted"] is False
    assert any("unexpected stack name vswitch-in-existing-vpc" in note for note in notes)


def test_non_cleanup_teardown_deletes_observed_create_stack(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    stack_name = runner._scenario_stack_name(tmp_path, "scenario1")

    class FakePty:
        run_dir = tmp_path
        env: dict[str, str] = {"ALIBABA_CLOUD_REGION_ID": "cn-hangzhou"}
        cleanup_ledger = {
            "observed_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "stack-created-by-scenario1",
                    "resource_name": stack_name,
                    "observed_action": "CreateStack",
                }
            ]
        }

    deleted_stack_ids = _install_observed_stack_teardown_fakes(monkeypatch, runner, stack_name=stack_name)
    checks: dict[str, bool] = {}
    notes: list[str] = []

    runner._teardown_real_cloud_scenario_resources(
        args=args,
        scenario="scenario1",
        pty=FakePty(),
        checks=checks,
        notes=notes,
    )

    assert deleted_stack_ids == ["stack-created-by-scenario1"]
    assert checks["teardown: observed ROS stacks deleted"] is True


def test_non_cleanup_teardown_refuses_observed_stack_name_mismatch(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    stack_name = runner._scenario_stack_name(tmp_path, "scenario1")

    class FakePty:
        run_dir = tmp_path
        env: dict[str, str] = {"ALIBABA_CLOUD_REGION_ID": "cn-hangzhou"}
        cleanup_ledger = {
            "observed_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "stack-created-by-scenario1",
                    "resource_name": stack_name,
                    "observed_action": "CreateStack",
                }
            ]
        }

    deleted_stack_ids = _install_observed_stack_teardown_fakes(monkeypatch, runner, stack_name="different-stack-name")
    checks: dict[str, bool] = {}
    notes: list[str] = []

    runner._teardown_real_cloud_scenario_resources(
        args=args,
        scenario="scenario1",
        pty=FakePty(),
        checks=checks,
        notes=notes,
    )

    assert deleted_stack_ids == []
    assert checks["teardown: observed ROS stacks deleted"] is False
    assert any("unexpected stack name different-stack-name" in note for note in notes)


def test_non_cleanup_teardown_refuses_non_test_owned_stack_name(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])

    class FakePty:
        run_dir = tmp_path
        env: dict[str, str] = {"ALIBABA_CLOUD_REGION_ID": "cn-hangzhou"}
        cleanup_ledger = {
            "observed_resources": [
                {
                    "provider": "ros",
                    "resource_type": "stack",
                    "resource_id": "stack-created-by-scenario1",
                    "resource_name": "vswitch-in-existing-vpc",
                    "observed_action": "CreateStack",
                }
            ]
        }

    deleted_stack_ids = _install_observed_stack_teardown_fakes(
        monkeypatch,
        runner,
        stack_name="vswitch-in-existing-vpc",
    )
    checks: dict[str, bool] = {}
    notes: list[str] = []

    runner._teardown_real_cloud_scenario_resources(
        args=args,
        scenario="scenario1",
        pty=FakePty(),
        checks=checks,
        notes=notes,
    )

    assert deleted_stack_ids == []
    assert checks["teardown: observed ROS stacks deleted"] is False
    assert any("unexpected test-owned stack name vswitch-in-existing-vpc" in note for note in notes)


def test_stack_creating_acceptance_requires_observed_ros_stack(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n" + args.normal_followup_prompt + "\n刚才创建了一个 VSwitch 交换机。\n"
    )

    class FakePty:
        pass

    FakePty.run_dir = tmp_path
    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.normal_followup_prompt,
            "transcript_offset": transcript.find(args.normal_followup_prompt),
        }
    ]
    FakePty.cleanup_ledger = {"observed_resources": []}

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("scenario1", args, FakePty(), checks)

    assert checks["acceptance: ROS stack observed in cleanup ledger"] is False
    assert checks["acceptance: ROS stack name is test-owned"] is False


def test_stack_creating_acceptance_records_observed_ros_stack(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    stack_name = runner._scenario_stack_name(tmp_path, "scenario1")
    transcript = (
        "● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n" + args.normal_followup_prompt + "\n刚才创建了一个 VSwitch 交换机。\n"
    )

    class FakePty:
        pass

    FakePty.run_dir = tmp_path
    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.normal_followup_prompt,
            "transcript_offset": transcript.find(args.normal_followup_prompt),
        }
    ]
    FakePty.cleanup_ledger = {
        "observed_resources": [
            {
                "provider": "ros",
                "resource_type": "stack",
                "resource_id": "stack-created-by-scenario1",
                "resource_name": stack_name,
                "observed_action": "CreateStack",
            }
        ]
    }
    FakePty.ros_stack_states = {
        "stack-created-by-scenario1": {
            "status": "CREATE_COMPLETE",
            "not_found": False,
            "stack_name": stack_name,
        }
    }

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("scenario1", args, FakePty(), checks)

    assert checks["acceptance: ROS stack observed in cleanup ledger"] is True
    assert checks["acceptance: ROS stack name is test-owned"] is True
    assert checks["acceptance: ROS created stack retained before teardown"] is True


def test_stack_creating_acceptance_allows_generated_suffix_on_test_stack_name(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    stack_name = f"{runner._scenario_stack_name(tmp_path, 'scenario1')}-20260809-a1b2c3"

    class FakePty:
        pass

    FakePty.run_dir = tmp_path
    FakePty.transcript = "● Confirm and select (4/5)\n✔ Pipeline completed\n交换机 ID   vsw-bp1234567890\n"
    FakePty.events = []
    FakePty.cleanup_ledger = {
        "observed_resources": [
            {
                "provider": "ros",
                "resource_type": "stack",
                "resource_id": "stack-created-by-scenario1",
                "resource_name": stack_name,
                "observed_action": "CreateStack",
            }
        ]
    }
    FakePty.ros_stack_states = {
        "stack-created-by-scenario1": {
            "status": "CREATE_COMPLETE",
            "not_found": False,
            "stack_name": stack_name,
        }
    }

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("scenario1", args, FakePty(), checks)

    assert checks["acceptance: ROS stack name is test-owned"] is True


def test_stack_creating_acceptance_rejects_non_test_owned_stack_name(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    class FakePty:
        pass

    FakePty.run_dir = tmp_path
    FakePty.transcript = "● Confirm and select (4/5)\n✔ Pipeline completed\n交换机 ID   vsw-bp1234567890\n"
    FakePty.events = []
    FakePty.cleanup_ledger = {
        "observed_resources": [
            {
                "provider": "ros",
                "resource_type": "stack",
                "resource_id": "stack-created-by-scenario1",
                "resource_name": "vswitch-in-existing-vpc",
                "observed_action": "CreateStack",
            }
        ]
    }
    FakePty.ros_stack_states = {
        "stack-created-by-scenario1": {
            "status": "CREATE_COMPLETE",
            "not_found": False,
            "stack_name": "vswitch-in-existing-vpc",
        }
    }

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("scenario1", args, FakePty(), checks)

    assert checks["acceptance: ROS stack observed in cleanup ledger"] is True
    assert checks["acceptance: ROS stack name is test-owned"] is False


def test_stack_creating_acceptance_allows_deleted_failed_stack_before_retained_retry(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    stack_name = runner._scenario_stack_name(tmp_path, "scenario1")
    transcript = (
        "● Confirm and select (4/5)\n"
        "failed-stack(failed-stack-id) CREATE_FAILED\n"
        "failed-stack(failed-stack-id) DELETE_COMPLETE\n"
        "retry-stack(retry-stack-id) CREATE_COMPLETE\n"
        "交换机 ID   vsw-bp1234567890\n" + args.normal_followup_prompt + "\n刚才创建了一个 VSwitch 交换机。\n"
    )

    class FakePty:
        pass

    FakePty.run_dir = tmp_path
    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.normal_followup_prompt,
            "transcript_offset": transcript.find(args.normal_followup_prompt),
        }
    ]
    FakePty.cleanup_ledger = {
        "observed_resources": [
            {
                "provider": "ros",
                "resource_type": "stack",
                "resource_id": "failed-stack-id",
                "resource_name": stack_name,
                "observed_action": "CreateStack",
            },
            {
                "provider": "ros",
                "resource_type": "stack",
                "resource_id": "retry-stack-id",
                "resource_name": stack_name,
                "observed_action": "CreateStack",
            },
        ]
    }
    FakePty.ros_stack_states = {
        "failed-stack-id": {"status": "DELETE_COMPLETE", "not_found": False, "stack_name": stack_name},
        "retry-stack-id": {"status": "CREATE_COMPLETE", "not_found": False, "stack_name": stack_name},
    }

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("scenario1", args, FakePty(), checks)

    assert checks["acceptance: ROS stack observed in cleanup ledger"] is True
    assert checks["acceptance: ROS stack name is test-owned"] is True
    assert checks["acceptance: ROS created stack retained before teardown"] is True


def test_acceptance_records_scenario1_business_evidence() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n" + args.normal_followup_prompt + "\n刚才创建了一个 VSwitch 交换机。\n"
    )

    class FakePty:
        pass

    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.normal_followup_prompt,
            "transcript_offset": transcript.find(args.normal_followup_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("scenario1", args, FakePty(), checks)

    assert checks["acceptance: candidate selection was shown"] is True
    assert checks["acceptance: pipeline completed"] is True
    assert checks["acceptance: VSwitch evidence found in PTY transcript"] is True
    assert checks["acceptance: normal follow-up answered created VSwitch"] is True


def test_acceptance_rejects_scenario1_normal_followup_without_resource_answer() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n" + args.normal_followup_prompt + "\n好的，我可以继续帮助你。\n"
    )

    class FakePty:
        pass

    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.normal_followup_prompt,
            "transcript_offset": transcript.find(args.normal_followup_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("scenario1", args, FakePty(), checks)

    assert checks["acceptance: VSwitch evidence found in PTY transcript"] is True
    assert checks["acceptance: normal follow-up answered created VSwitch"] is False


def test_cleanup_pipeline_completion_requires_normal_chat_active() -> None:
    runner = _load_runner()

    assert not runner._has_any_pattern(
        "iac-e2e-demo(second-id) CREATE_COMPLETE", runner.PIPELINE_FULLY_COMPLETED_PATTERNS
    )
    assert runner._has_any_pattern(
        "Pipeline completed. Normal chat is now active.",
        runner.PIPELINE_FULLY_COMPLETED_PATTERNS,
    )


def test_acceptance_records_vswitch_stack_business_evidence_without_vswitch_id() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    class FakePty:
        transcript = (
            "● Confirm and select (4/5)\n"
            "✔ Pipeline completed\n"
            "VSwitch（交换机） 单可用区\n"
            "✅ 部署成功\n"
            "Stack ID    f851142e-5f47-4d55-905b-116f8a0bf4b9\n"
        )
        events: list[dict[str, object]] = []

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("scenario1", args, FakePty(), checks)

    assert checks["acceptance: VSwitch evidence found in PTY transcript"] is True


def test_acceptance_records_standard_ros_deploy_success_render_as_business_evidence() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    class FakePty:
        transcript = (
            "● Confirm and select (4/5)\n"
            "VSwitch（交换机） 单可用区\n"
            "● ROS Deploy\n"
            "  ⎿  iac-e2e-demo creation succeeded (0d9a9bee)\n"
            "✔ Pipeline completed\n"
        )
        events: list[dict[str, object]] = []

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("selection-invalid-then-valid", args, FakePty(), checks)

    assert checks["acceptance: VSwitch evidence found in PTY transcript"] is True


def test_acceptance_rejects_unqualified_creation_succeeded_text() -> None:
    runner = _load_runner()

    assert runner._has_vswitch_business_evidence("VSwitch creation succeeded") is False


def test_acceptance_rejects_completed_vswitch_scenario_without_resource_evidence() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])

    class FakePty:
        transcript = "● Confirm and select (4/5)\n✔ Pipeline completed\n"
        events = [
            {"type": "select-invalid-candidate", "text": "9"},
            {"type": "select-default-candidate", "text": "1\r"},
        ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("selection-invalid-then-valid", args, FakePty(), checks)

    assert checks["acceptance: pipeline completed"] is True
    assert checks["acceptance: VSwitch evidence found in PTY transcript"] is False


def test_acceptance_rejects_rollback_security_group_target_from_echo_only() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = "● Evaluate candidates (3/5)\n" + args.rollback_prompt + "\n● Intent parsing (1/5)\n"

    class FakePty:
        pass

    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.rollback_prompt,
            "transcript_offset": transcript.find(args.rollback_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step3", args, FakePty(), checks)

    assert checks["acceptance: rollback produced post-interrupt pipeline progress"] is True
    assert checks["acceptance: post-rollback target is security group"] is False


def test_acceptance_records_rollback_security_group_target_after_prompt() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Evaluate candidates (3/5)\n"
        + args.rollback_prompt
        + "\n● Intent parsing (1/5)\n"
        + "Step Intent parsing completed. Conclusion submitted.\n"
        + "本轮目标资源为 ALIYUN::ECS::SecurityGroup 安全组。\n"
    )

    class FakePty:
        pass

    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.rollback_prompt,
            "transcript_offset": transcript.find(args.rollback_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step3", args, FakePty(), checks)

    assert checks["acceptance: post-rollback target is security group"] is True
    assert checks["acceptance: post-rollback target is not VSwitch"] is True


def test_post_rollback_security_group_target_waits_for_slow_candidate_evaluation() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--stream-timeout", "600"])
    observed_timeouts: list[float] = []

    class FakePty:
        def expect_any(self, patterns, *, description, timeout):
            observed_timeouts.append(timeout)
            return patterns[0]

    checks: dict[str, bool] = {}

    runner._expect_post_rollback_security_group_target(FakePty(), args, checks)

    assert observed_timeouts == [300.0]
    assert checks["post-rollback security group target visible"] is True


def test_acceptance_allows_post_rollback_forbidden_vswitch_context() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Architecture planning (2/5)\n"
        + args.rollback_prompt
        + "\n● Intent parsing (1/5)\n"
        + "Step Intent parsing completed. Conclusion submitted.\n"
        + "resource_intents: SecurityGroup=create, VSwitch=forbid。\n"
        + "在用户指定的已有VPC中创建一个安全组，安全组挂载在该VPC下，不创建新的VSwitch。\n"
    )

    class FakePty:
        pass

    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.rollback_prompt,
            "transcript_offset": transcript.find(args.rollback_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step2", args, FakePty(), checks)

    assert checks["acceptance: post-rollback target is security group"] is True
    assert checks["acceptance: post-rollback target is not VSwitch"] is True


def test_acceptance_allows_post_rollback_change_reason_mentions_old_vswitch_target() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Confirm and select (4/5)\n"
        + args.rollback_prompt
        + "\n╭─ Interrupt handling ─╮\n"
        + "用户明确要求回退到intent_parsing并将需求从创建 VSwitch\n"
        + "改为创建安全组，意图发生根本改变。\n"
        + "● Intent parsing (1/5)\n"
        + "Step Intent parsing completed. Conclusion submitted.\n"
        + "● Evaluate candidates (3/5)\n"
        + "✓ 已有VPC创建安全组: Completed\n"
    )

    class FakePty:
        pass

    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.rollback_prompt,
            "transcript_offset": transcript.find(args.rollback_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step4-selection", args, FakePty(), checks)

    assert checks["acceptance: post-rollback target is security group"] is True
    assert checks["acceptance: post-rollback target is not VSwitch"] is True


def test_acceptance_ignores_delayed_pre_rollback_candidate_render() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Confirm and select (4/5)\n"
        + args.rollback_prompt
        + "\ngraph TD\n  subgraph layer_VSwitch [VSwitch]\n  end\n"
        + "在用户已有的 VPC 中新建一个 VSwitch。\n"
        + "╭─ Interrupt handling ─╮\n"
        + "● Intent parsing (1/5)\n"
        + "Step Intent parsing completed. Conclusion submitted.\n"
        + "● Architecture planning (2/5)\n"
        + "在已有 VPC 中创建一个安全组，不创建新的交换机。\n"
    )

    class FakePty:
        pass

    pty = FakePty()
    pty.transcript = transcript
    pty.events = [
        {
            "type": "sendline",
            "text": args.rollback_prompt,
            "transcript_offset": transcript.find(args.rollback_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step4-selection", args, pty, checks)

    assert checks["acceptance: post-rollback target is security group"] is True
    assert checks["acceptance: post-rollback target is not VSwitch"] is True


def test_acceptance_allows_post_rollback_english_no_vswitch_context() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Confirm and select (4/5)\n"
        + args.rollback_prompt
        + "\n● Intent parsing (1/5)\n"
        + "Step Intent parsing completed. Conclusion submitted.\n"
        + "● Architecture planning (2/5)\n"
        + "create a security group in an existing VPC, with no VSwitch. Only one candidate is needed.\n"
        + "● Evaluate candidates (3/5)\n"
        + "✓ 已有VPC新建安全组: Completed\n"
    )

    class FakePty:
        pass

    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.rollback_prompt,
            "transcript_offset": transcript.find(args.rollback_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step4-selection", args, FakePty(), checks)

    assert checks["acceptance: post-rollback target is security group"] is True
    assert checks["acceptance: post-rollback target is not VSwitch"] is True


def test_acceptance_rejects_post_rollback_positive_vswitch_target() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    transcript = (
        "● Architecture planning (2/5)\n"
        + args.rollback_prompt
        + "\n● Intent parsing (1/5)\n"
        + "Step Intent parsing completed. Conclusion submitted.\n"
        + "本轮目标资源为 ALIYUN::ECS::VSwitch 交换机。\n"
    )

    class FakePty:
        pass

    FakePty.transcript = transcript
    FakePty.events = [
        {
            "type": "sendline",
            "text": args.rollback_prompt,
            "transcript_offset": transcript.find(args.rollback_prompt),
        }
    ]

    checks: dict[str, bool] = {}

    runner._apply_acceptance_checks("rollback-step2", args, FakePty(), checks)

    assert checks["acceptance: post-rollback target is not VSwitch"] is False


def test_run_with_pty_writes_acceptance_checks_after_callback_failure(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()

    class FakePty:
        def __init__(self, *, args, run_dir, cwd, env):
            self.events = []
            self.transcript = "captured transcript"

        def spawn(self, *, extra_args=None):
            return None

        def terminate(self, *, force=False):
            return None

    def callback(_pty, _checks):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "ReplPty", FakePty)
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])

    assert runner._run_with_pty(args, "scenario1", callback) == 1
    summary = (tmp_path / "summary.json").read_text(encoding="utf-8")

    assert "acceptance: PTY transcript captured" in summary


def test_scenario1_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    actions: list[tuple[str, str]] = []

    class FakePty:
        def __init__(self, *, args, run_dir, cwd, env):
            stack_name = runner._scenario_stack_name(run_dir, "scenario1")
            self.run_dir = run_dir
            self.env = env
            self.events = []
            self.transcript = (
                "● Confirm and select (4/5)\n"
                "✔ Pipeline completed\n"
                "交换机 ID   vsw-bp1234567890\n" + args.normal_followup_prompt + "\n刚才创建了一个 VSwitch 交换机。\n"
            )
            self.cleanup_ledger = {
                "observed_resources": [
                    {
                        "provider": "ros",
                        "resource_type": "stack",
                        "resource_id": "normal-stack-id",
                        "resource_name": stack_name,
                        "observed_action": "CreateStack",
                    }
                ]
            }
            self.ros_stack_states = {
                "normal-stack-id": {
                    "status": "CREATE_COMPLETE",
                    "not_found": False,
                    "stack_name": stack_name,
                }
            }

        def spawn(self, *, extra_args=None):
            actions.append(("spawn", ""))

        def sendline(self, text):
            actions.append(("sendline", text))
            self.events.append({"type": "sendline", "text": text, "transcript_offset": self.transcript.find(text)})

        def expect_any(self, patterns, *, description, timeout, state_check=None):
            actions.append(("expect", description))
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            actions.append(("expect_optional", description))
            return True

        def send(self, text, *, label="send"):
            actions.append((label, text))

        def terminate(self, *, force=False):
            actions.append(("terminate", str(force)))

    monkeypatch.setattr(runner, "ReplPty", FakePty)
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    stack_owned_initial = runner._stack_creating_prompt(args.initial_prompt, tmp_path, "scenario1")
    _install_observed_stack_teardown_fakes(
        monkeypatch,
        runner,
        stack_name=runner._scenario_stack_name(tmp_path, "scenario1"),
    )

    assert runner.run_scenario1(args, "scenario1") == 0
    assert ("sendline", stack_owned_initial) in actions
    assert ("select-default-candidate", f"{runner.DEFAULT_SELECTION_PROMPT}\r") in actions
    assert ("sendline", runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT) in actions
    assert ("sendline", "/exit") in actions


def test_image_initial_pastes_static_prompt_image(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = "● Confirm and select (4/5)\n✔ Pipeline completed\n交换机 ID   vsw-bp1234567890\n"
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="image-initial")

    assert runner.run_image_initial(args, "image-initial") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "sendline", "paste-image-fixture", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("paste-image-fixture", "initial"),
        ("sendline", runner._stack_name_constraint(tmp_path, "image-initial")),
        ("expect", "pipeline started"),
        ("expect", "candidate selection visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after image initial"),
        ("sendline", "/exit"),
    ]
    assert ("sendline", args.initial_prompt) not in actions


def test_ask_waiting_waits_for_answer_prompt_before_sending(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    stack_owned_answer = runner._stack_creating_prompt(args.ask_answer, tmp_path, "ask-waiting")
    transcript = (
        "● Ask user question\n" + stack_owned_answer + "\n● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="ask-waiting")

    assert runner.run_ask_waiting(args, "ask-waiting") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "spawn", "terminate", "sendline", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("spawn", ""),
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", args.ask_prompt),
        ("expect", "ask question visible"),
        ("expect", "ask answer input ready"),
        ("sendline", stack_owned_answer),
        ("expect", "pipeline continued after ask"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after ask"),
        ("sendline", "/exit"),
        ("terminate", "False"),
    ]


def test_image_ask_waiting_resume_pastes_static_answer_image(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Ask user question\n"
        "● Ask user question\n"
        "● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="image-ask-waiting-resume")

    assert runner.run_image_ask_waiting_resume(args, "image-ask-waiting-resume") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "spawn", "terminate", "sendline", "paste-image-fixture", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("spawn", ""),
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", args.ask_prompt),
        ("expect", "ask question visible before kill"),
        ("expect", "ask answer input ready before kill"),
        ("terminate", "True"),
        ("spawn", "--continue"),
        ("expect", "ask question replayed"),
        ("expect", "ask image answer input ready after resume"),
        ("paste-image-fixture", "ask-first-answer"),
        ("sendline", runner._stack_name_constraint(tmp_path, "image-ask-waiting-resume")),
        ("expect", "pipeline continued after ask image resume"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after ask image resume"),
        ("sendline", "/exit"),
        ("terminate", "False"),
    ]
    assert ("sendline", args.ask_answer) not in actions


def test_image_selection_waiting_resume_starts_with_image_and_recovers_selection(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Confirm and select (4/5)\n● Confirm and select (4/5)\n✔ Pipeline completed\n交换机 ID   vsw-bp1234567890\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="image-selection-waiting-resume")

    assert runner.run_image_selection_waiting_resume(args, "image-selection-waiting-resume") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "spawn", "terminate", "sendline", "paste-image-fixture", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("spawn", ""),
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("paste-image-fixture", "initial"),
        ("sendline", runner._stack_name_constraint(tmp_path, "image-selection-waiting-resume")),
        ("expect", "candidate selection visible before image resume kill"),
        ("terminate", "True"),
        ("spawn", "--continue"),
        ("expect", "candidate selection replayed after image resume"),
        ("expect", "live candidate selection controls ready"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after image selection resume"),
        ("sendline", "/exit"),
        ("terminate", "False"),
    ]


def test_image_normal_handoff_pastes_static_followup_image(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "Pipeline completed. Normal chat is now active.\n"
        "[Image #1]\n"
        "刚才创建了一个 VSwitch 交换机。\n"
        "交换机 ID   vsw-bp1234567890\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="image-normal-handoff")
    stack_owned_initial = runner._stack_creating_prompt(args.initial_prompt, tmp_path, "image-normal-handoff")

    assert runner.run_image_normal_handoff(args, "image-normal-handoff") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "sendline", "paste-image-fixture", "submit-image", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", stack_owned_initial),
        ("expect", "pipeline started"),
        ("expect", "candidate selection visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline fully completed"),
        ("expect", "normal prompt input ready"),
        ("paste-image-fixture", "normal-followup"),
        ("submit-image", "\r"),
        ("expect", "normal image follow-up answered created VSwitch"),
        ("sendline", "/exit"),
    ]
    assert ("sendline", args.normal_followup_prompt) not in actions


def test_image_interrupt_pastes_static_rollback_image(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Evaluate candidates (3/5)\n"
        "[Image #1]\n"
        "● Intent parsing (1/5)\n"
        "目标资源为 ALIYUN::ECS::SecurityGroup 安全组。\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="image-interrupt")

    assert runner.run_image_interrupt(args, "image-interrupt") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "send-esc", "sendline", "paste-image-fixture", "submit-image"}
    ]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", args.initial_prompt),
        ("expect", "candidate evaluation visible"),
        ("expect", "parallel interrupt input ready"),
        ("send-esc", "\x1b"),
        ("expect", "parallel interrupt text input ready"),
        ("paste-image-fixture", "rollback-interrupt"),
        ("submit-image", "\r"),
        ("expect", "post-rollback pipeline progress visible"),
        ("expect", "post-rollback security group target visible"),
        ("sendline", "/exit"),
    ]
    assert ("sendline", args.rollback_prompt) not in actions


def test_rollback_step3_sends_rollback_prompt_without_waiting_for_visible_interrupt(
    monkeypatch, tmp_path: Path
) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []

    class FakePty:
        def __init__(self, *, args, run_dir, cwd, env):
            self.events = []
            self.transcript = (
                "● Evaluate candidates (3/5)\n"
                "回退到 intent_parsing，选择一个已有vpc，创建一个安全组\n"
                "● Intent parsing (1/5)\n"
                "目标资源为 ALIYUN::ECS::SecurityGroup 安全组。\n"
            )

        def spawn(self, *, extra_args=None):
            actions.append(("spawn", ""))

        def sendline(self, text):
            actions.append(("sendline", text))
            offset = self.transcript.find("● Intent parsing (1/5)") if text == args.rollback_prompt else 0
            self.events.append({"type": "sendline", "text": text, "transcript_offset": offset})

        def expect_any(self, patterns, *, description, timeout):
            if description in {"candidate evaluation activity visible", "interrupt input visible"}:
                raise AssertionError(description)
            actions.append(("expect", description))
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            actions.append(("expect_optional", description))
            return True

        def send(self, text, *, label="send"):
            actions.append((label, text))
            self.events.append({"type": label, "transcript_offset": self.transcript.find("回退到")})

        def terminate(self, *, force=False):
            actions.append(("terminate", str(force)))

    monkeypatch.setattr(runner, "ReplPty", FakePty)

    assert runner.run_rollback_step3(args, "rollback-step3") == 0

    ordered_actions = [(kind, value) for kind, value in actions if kind in {"expect", "send-esc"}]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("expect", "candidate evaluation visible"),
        ("expect", "parallel interrupt input ready"),
        ("send-esc", "\x1b"),
        ("expect", "parallel interrupt text input ready"),
        ("expect", "post-rollback pipeline progress visible"),
        ("expect", "post-rollback security group target visible"),
    ]
    assert ("sendline", args.rollback_prompt) in actions


def test_rollback_step3_waits_for_interrupt_text_input_ready_after_escape(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []

    class FakePty:
        def __init__(self, *, args, run_dir, cwd, env):
            self.events = []
            self.transcript = (
                "● Evaluate candidates (3/5)\n"
                "回退到 intent_parsing，选择一个已有vpc，创建一个安全组\n"
                "● Intent parsing (1/5)\n"
                "目标资源为 ALIYUN::ECS::SecurityGroup 安全组。\n"
            )

        def spawn(self, *, extra_args=None):
            actions.append(("spawn", ""))

        def sendline(self, text):
            actions.append(("sendline", text))
            offset = self.transcript.find("● Intent parsing (1/5)") if text == args.rollback_prompt else 0
            self.events.append({"type": "sendline", "text": text, "transcript_offset": offset})

        def expect_any(self, patterns, *, description, timeout):
            actions.append(("expect", description))
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            actions.append(("expect_optional", description))
            return True

        def send(self, text, *, label="send"):
            actions.append((label, text))
            self.events.append({"type": label, "transcript_offset": self.transcript.find("回退到")})

        def terminate(self, *, force=False):
            actions.append(("terminate", str(force)))

    monkeypatch.setattr(runner, "ReplPty", FakePty)

    assert runner.run_rollback_step3(args, "rollback-step3") == 0

    assert actions.index(("send-esc", "\x1b")) < actions.index(("expect", "parallel interrupt text input ready"))
    assert actions.index(("expect", "parallel interrupt text input ready")) < actions.index(
        ("sendline", args.rollback_prompt)
    )


def test_rollback_step2_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Architecture planning (2/5)\n✎ "
        + args.rollback_prompt
        + "\n● Intent parsing (1/5)\n目标资源为 ALIYUN::ECS::SecurityGroup 安全组。\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions)

    assert runner.run_rollback_step2(args, "rollback-step2") == 0

    ordered_actions = [(kind, value) for kind, value in actions if kind in {"expect", "send-esc", "sendline"}]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", args.initial_prompt),
        ("expect", "architecture planning visible"),
        ("send-esc", "\x1b"),
        ("expect", "interrupt input visible"),
        ("expect", "interrupt prompt input ready"),
        ("sendline", args.rollback_prompt),
        ("expect", "post-rollback pipeline progress visible"),
        ("expect", "post-rollback security group target visible"),
        ("sendline", "/exit"),
    ]


def test_rollback_step4_selection_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Confirm and select (4/5)\n"
        + args.rollback_prompt
        + "\n● Intent parsing (1/5)\n目标资源为 ALIYUN::ECS::SecurityGroup 安全组。\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions)

    assert runner.run_rollback_step4_selection(args, "rollback-step4-selection") == 0

    ordered_actions = [(kind, value) for kind, value in actions if kind in {"expect", "send-esc", "sendline"}]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", args.initial_prompt),
        ("expect", "candidate selection visible"),
        ("send-esc", "\x1b"),
        ("expect", "candidate selection interrupt input visible"),
        ("expect", "candidate selection interrupt text input ready"),
        ("sendline", args.rollback_prompt),
        ("expect", "post-rollback pipeline progress visible"),
        ("expect", "post-rollback security group target visible"),
        ("sendline", "/exit"),
    ]


def test_evaluate_resume_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Evaluate candidates (3/5)\n"
        "● Evaluate candidates (3/5)\n" + args.evaluate_resume_continue_prompt + "\n"
        "● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="evaluate-resume")
    stack_owned_initial = runner._stack_creating_prompt(args.initial_prompt, tmp_path, "evaluate-resume")

    assert runner.run_evaluate_resume(args, "evaluate-resume") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "spawn", "terminate", "sendline", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("spawn", ""),
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", stack_owned_initial),
        ("expect", "candidate evaluation visible"),
        ("expect", "parallel interrupt input ready"),
        ("terminate", "True"),
        ("spawn", "--continue"),
        ("expect", "candidate evaluation replayed after resume"),
        ("expect", "evaluate resume prompt input ready"),
        ("sendline", args.evaluate_resume_continue_prompt),
        ("expect", "candidate selection visible after resume continue"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after evaluate resume"),
        ("sendline", "/exit"),
        ("terminate", "False"),
    ]


def test_ask_waiting_resume_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    stack_owned_answer = runner._stack_creating_prompt(args.ask_answer, tmp_path, "ask-waiting-resume")
    transcript = (
        "● Ask user question\n"
        "● Ask user question\n" + stack_owned_answer + "\n● Confirm and select (4/5)\n"
        "✔ Pipeline completed\n"
        "交换机 ID   vsw-bp1234567890\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="ask-waiting-resume")

    assert runner.run_ask_waiting_resume(args, "ask-waiting-resume") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "spawn", "terminate", "sendline", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("spawn", ""),
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", args.ask_prompt),
        ("expect", "ask question visible before kill"),
        ("expect", "ask answer input ready before kill"),
        ("terminate", "True"),
        ("spawn", "--continue"),
        ("expect", "ask question replayed"),
        ("expect", "ask answer input ready after resume"),
        ("sendline", stack_owned_answer),
        ("expect", "pipeline continued after ask resume"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after ask resume"),
        ("sendline", "/exit"),
        ("terminate", "False"),
    ]


def test_selection_waiting_resume_waits_for_live_controls_before_selecting(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Confirm and select (4/5)\n● Confirm and select (4/5)\n✔ Pipeline completed\n交换机 ID   vsw-bp1234567890\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="selection-waiting-resume")
    stack_owned_initial = runner._stack_creating_prompt(
        args.initial_prompt,
        tmp_path,
        "selection-waiting-resume",
    )

    assert runner.run_selection_waiting_resume(args, "selection-waiting-resume") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "spawn", "terminate", "sendline", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("spawn", ""),
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", stack_owned_initial),
        ("expect", "candidate selection visible"),
        ("terminate", "True"),
        ("spawn", "--continue"),
        ("expect", "candidate selection replayed"),
        ("expect", "live candidate selection controls ready"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after resume"),
        ("sendline", "/exit"),
        ("terminate", "False"),
    ]


def test_selection_invalid_then_valid_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = "● Confirm and select (4/5)\n✔ Pipeline completed\n交换机 ID   vsw-bp1234567890\n"
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions, scenario="selection-invalid-then-valid")
    stack_owned_initial = runner._stack_creating_prompt(
        args.initial_prompt,
        tmp_path,
        "selection-invalid-then-valid",
    )

    assert runner.run_selection_invalid_then_valid(args, "selection-invalid-then-valid") == 0

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "sendline", "select-invalid-candidate", "select-default-candidate"}
    ]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", stack_owned_initial),
        ("expect", "candidate selection visible"),
        ("select-invalid-candidate", "9"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed"),
        ("sendline", "/exit"),
    ]


def test_auto_cleanup_network_target_is_rediscovered_for_each_scenario(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args([])
    targets = [
        runner.CleanupNetworkTarget(
            vpc_id="vpc-first",
            vpc_cidr="172.16.0.0/12",
            zone_id="cn-hangzhou-h",
            vswitch_cidr="172.31.255.0/24",
            rollback_vswitch_cidr="172.31.254.0/24",
        ),
        runner.CleanupNetworkTarget(
            vpc_id="vpc-second",
            vpc_cidr="10.0.0.0/8",
            zone_id="cn-hangzhou-k",
            vswitch_cidr="10.255.255.0/24",
            rollback_vswitch_cidr="10.255.254.0/24",
        ),
    ]
    discoveries: list[runner.CleanupNetworkTarget] = []
    exclusions: list[set[str]] = []

    def discover(*, excluded_cidrs=()) -> runner.CleanupNetworkTarget:
        exclusions.append(set(excluded_cidrs))
        target = targets[len(discoveries)]
        discoveries.append(target)
        return target

    monkeypatch.setattr(runner, "_discover_cleanup_network_target", discover)

    assert runner._ensure_cleanup_network_target(args, tmp_path / "first") == targets[0]
    assert runner._ensure_cleanup_network_target(args, tmp_path / "second") == targets[1]
    assert discoveries == targets
    assert exclusions == [set(), {"172.31.255.0/24", "172.31.254.0/24"}]


def test_explicit_cleanup_network_target_is_reused(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(
        [
            "--cleanup-vpc-id",
            "vpc-explicit",
            "--cleanup-vpc-cidr",
            "172.16.0.0/12",
            "--cleanup-zone-id",
            "cn-hangzhou-h",
            "--cleanup-vswitch-cidr",
            "172.31.255.0/24",
            "--cleanup-rollback-vswitch-cidr",
            "172.31.254.0/24",
        ]
    )
    monkeypatch.setattr(
        runner,
        "_discover_cleanup_network_target",
        lambda **_: (_ for _ in ()).throw(AssertionError("explicit target must not be rediscovered")),
    )

    first = runner._ensure_cleanup_network_target(args, tmp_path / "first")
    second = runner._ensure_cleanup_network_target(args, tmp_path / "second")

    assert first == second
    assert first.vpc_id == "vpc-explicit"


def test_rollback_step5_cleanup_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Confirm and select (4/5)\n"
        "first-stack(first-stack-id) CREATE_COMPLETE\n"
        "● Confirm and select (4/5)\n"
        "second-stack(second-stack-id) CREATE_COMPLETE\n"
        "检测到 1 个回滚残留资源，开始清理流程。\n"
        "↺ 回滚清理 [完成] first-stack · 资源栈 first-stack-id · DELETE_COMPLETE\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions)
    monkeypatch.setattr(
        runner,
        "_ensure_cleanup_network_target",
        lambda _args, _run_dir: runner.CleanupNetworkTarget(
            vpc_id="vpc-test",
            vpc_cidr="172.16.0.0/12",
            zone_id="cn-hangzhou-h",
            vswitch_cidr="172.31.255.0/24",
            rollback_vswitch_cidr="172.31.254.0/24",
        ),
    )
    def observe_first_stack(*_, **__) -> str:
        actions.append(("stack-observed", "first-stack-id"))
        return "first-stack-id"

    monkeypatch.setattr(runner, "_wait_for_latest_observed_stack_id", observe_first_stack)
    monkeypatch.setattr(runner, "_cleanup_target_stack_ids", lambda *_, **__: ["first-stack-id"])
    monkeypatch.setattr(runner, "_wait_for_cleanup_resource_status", lambda *_, **__: None)
    monkeypatch.setattr(
        runner,
        "_latest_observed_stack_id",
        lambda _pty, *, exclude: "second-stack-id" if "first-stack-id" in exclude else "first-stack-id",
    )
    deleted_stack_ids = _install_cleanup_teardown_fakes(monkeypatch, runner, tmp_path)

    assert runner.run_rollback_step5_cleanup(args, "rollback-step5-cleanup") == 0
    assert deleted_stack_ids == ["second-stack-id"]

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "send-esc", "sendline", "select-default-candidate", "stack-observed"}
        or (kind == "expect_optional" and value == "cleanup completed")
    ]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", runner._cleanup_pipeline_prompt(args, tmp_path)),
        ("expect", "initial candidate selection or clarification visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "first stack create started"),
        ("stack-observed", "first-stack-id"),
        ("send-esc", "\x1b"),
        ("expect", "deploying interrupt input visible"),
        ("expect", "deploying interrupt input ready"),
        ("sendline", runner._cleanup_rollback_prompt(args, tmp_path)),
        ("expect", "post-rollback candidate selection or clarification visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after second deployment"),
        ("expect", "cleanup start or normal follow-up prompt input ready"),
        ("sendline", args.normal_followup_prompt),
        ("expect", "cleanup started after normal follow-up"),
        ("expect_optional", "cleanup completed"),
        ("expect", "post-cleanup prompt input ready"),
        ("sendline", "/exit"),
    ]


def test_display_progress_counts_only_fixed_event_types(tmp_path: Path) -> None:
    runner = _load_runner()
    display = tmp_path / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True)
    (display.parent / "cleanup.yaml").write_text("observed_resources: []\n", encoding="utf-8")
    display.write_text(
        "\n".join(json.dumps(event) for event in (
            {"type": "candidate_selection_ready", "payload": {"secret": "sk-fixture"}},
            {"type": "user_input_received"},
            {"type": "private-sk-fixture"},
            {"type": ["candidate_selection_ready"]},
            {"type": "step_started", "step_id": "deploying"},
            {"type": "step_completed", "step_id": "deploying"},
            {"type": "tool_used", "payload": {"name": "ros_deploy", "secret": "sk-fixture"}},
            {"type": "tool_used", "payload": {"name": "aliyun_api", "secret": "sk-fixture"}},
            {"type": "tool_used", "payload": {"name": "ros_stack", "secret": "sk-fixture"}},
            {"type": "tool_used", "payload": {"name": "bash", "secret": "sk-fixture"}},
            {"type": "pipeline_completed", "payload": {"early_exit": True, "secret": "sk-fixture"}},
            {"type": "stack_progress", "payload": {"status": "CREATE_COMPLETE", "stack_id": "secret-id"}},
        )) + "\n",
        encoding="utf-8",
    )

    assert runner._display_progress(tmp_path) == {
        "candidate_selection_ready": 1, "user_input_received": 1,
        "step_started": 1, "step_started_deploying": 1,
        "step_completed": 1, "step_completed_deploying": 1,
        "ros_deploy_used": 1,
        "aliyun_api_used": 1, "ros_stack_used": 1, "bash_used": 1,
        "pipeline_completed": 1, "pipeline_completed_early_exit": 1,
        "stack_progress": 1, "stack_progress_create_complete": 1, "cleanup_ledger_files": 1,
    }


def test_candidate_selection_retries_only_until_durable_submission(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    display = tmp_path / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True)
    display.write_text("", encoding="utf-8")
    sent: list[str] = []
    clock = [0.0]

    class Pty:
        env = {"IAC_CODE_CONFIG_DIR": str(tmp_path)}

        def send(self, text: str, *, label: str):
            sent.append(label)
            if len(sent) == 2:
                display.write_text('{"type":"candidate_selection_submitted"}\n', encoding="utf-8")

        def drain_output(self):
            pass

    def tick() -> float:
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(runner.time, "monotonic", tick)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    runner._select_default_candidate(Pty(), type("Args", (), {"selection_prompt": ""})())

    assert sent == ["select-default-candidate", "select-default-candidate-retry-2"]


def test_reliable_sendline_drains_paste_before_enter(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    pty = _repl_pty_unit_instance(runner, args=None, run_dir=tmp_path, cwd=tmp_path, env={})
    actions: list[str] = []

    class Child:
        def send(self, text: str):
            actions.append(text)

    pty.child = Child()
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(pty, "drain_output", lambda: actions.append("drain"))
    pty.sendline_reliable("rollback")

    assert actions == ["\x1b[200~rollback\x1b[201~", "drain", "\r"]
    assert pty.events[-1]["type"] == "sendline"


def test_transcript_tool_progress_counts_results_without_content(tmp_path: Path) -> None:
    runner = _load_runner()
    transcript = (
        tmp_path / "projects" / "project" / "session" / "pipeline" / "transcripts" / "attempt" / "session.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(json.dumps(item) for item in [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "first", "name": "ros_deploy", "input": {"secret": "sk-fixture"}},
                {"type": "tool_use", "id": "second", "name": "ros_deploy", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "first", "content": "sk-fixture", "is_error": True},
                {"type": "tool_result", "tool_use_id": "unrelated", "content": "", "is_error": False},
            ]},
        ]) + "\n",
        encoding="utf-8",
    )

    assert runner._transcript_tool_progress(tmp_path) == {
        "ros_deploy_result": 1,
        "ros_deploy_result_error": 1,
    }


def test_first_stack_create_uses_display_deploy_event_when_terminal_marker_is_absent(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    config_dir = tmp_path / "config"
    display = config_dir / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True)

    class FakePty:
        env = {"IAC_CODE_CONFIG_DIR": str(config_dir)}
        transcript = ""
        events: list[dict[str, object]] = []

        def expect_any(self, patterns, *, description, timeout):
            assert patterns == runner.CREATE_STACK_STARTED_PATTERNS
            assert description == "first stack create started"
            display.write_text('{"type":"tool_used","payload":{"name":"ros_deploy"}}\n', encoding="utf-8")
            raise TimeoutError("timed out waiting for first stack create started")

    pty = FakePty()
    runner._expect_first_stack_create_started(pty, args)
    assert pty.events[-1]["pattern"] == "display:ros_deploy"


def test_first_stack_create_rejects_deploy_event_after_step_completed(tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    config_dir = tmp_path / "config"
    display = config_dir / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True)
    display.write_text(
        '{"type":"tool_used","payload":{"name":"ros_deploy"}}\n'
        '{"type":"step_completed","step_id":"deploying"}\n',
        encoding="utf-8",
    )

    class FakePty:
        env = {"IAC_CODE_CONFIG_DIR": str(config_dir)}
        transcript = ""
        events: list[dict[str, object]] = []

        def expect_any(self, patterns, *, description, timeout):
            raise AssertionError("the completed deployment must be detected before waiting on PTY")

    with pytest.raises(RuntimeError, match="ROS deployment finished before rollback interrupt"):
        runner._expect_first_stack_create_started(FakePty(), args)


def test_first_stack_observation_stops_when_deploying_finishes_without_stack(tmp_path: Path) -> None:
    runner = _load_runner()
    config_dir = tmp_path / "config"
    display = config_dir / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True)
    display.write_text('{"type":"step_completed","step_id":"deploying"}\n', encoding="utf-8")

    class FakePty:
        env = {"IAC_CODE_CONFIG_DIR": str(config_dir)}

    with pytest.raises(RuntimeError, match="deploying finished before rollback observed a ROS stack"):
        runner._wait_for_latest_observed_stack_id(FakePty(), exclude=set(), timeout=10)


def test_first_stack_observation_drains_pty_while_waiting(monkeypatch) -> None:
    runner = _load_runner()

    class FakePty:
        env: dict[str, str] = {}
        drained = False

        def drain_output(self) -> None:
            self.drained = True

    pty = FakePty()
    monkeypatch.setattr(
        runner,
        "_latest_observed_stack_id",
        lambda _pty, *, exclude: "stack-id" if pty.drained else None,
    )

    assert runner._wait_for_latest_observed_stack_id(pty, exclude=set(), timeout=10) == "stack-id"
    assert pty.drained is True


def test_cleanup_target_observation_drains_pty_while_waiting(monkeypatch) -> None:
    runner = _load_runner()

    class FakePty:
        drained = False

        def drain_output(self) -> None:
            self.drained = True

    pty = FakePty()
    monkeypatch.setattr(
        runner,
        "_cleanup_target_stack_ids",
        lambda _pty, *, exclude: ["stack-id"] if pty.drained else [],
    )

    assert runner._wait_for_cleanup_target_stack_ids(pty, exclude=set(), timeout=10) == ["stack-id"]
    assert pty.drained is True


def test_first_stack_observation_stops_when_cloud_completed_without_ledger(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    ticks = iter([0.0, 121.0, 121.0])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner, "_latest_observed_stack_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_discover_owned_cleanup_stack_ids", lambda _run_dir: ["stack-id"])
    monkeypatch.setattr(
        runner,
        "_fresh_ros_stack_state",
        lambda _pty, _stack_id: {
            "stack_name": runner._cleanup_stack_name(tmp_path, "first"),
            "status": "CREATE_COMPLETE",
        },
    )

    class FakePty:
        run_dir = tmp_path
        env: dict[str, str] = {}

    pty = FakePty()
    with pytest.raises(RuntimeError, match="ROS Stack completed but no resource reached the cleanup ledger"):
        runner._wait_for_latest_observed_stack_id(pty, exclude=set(), timeout=1800)
    assert pty.cloud_stack_without_ledger is True


def test_first_stack_observation_stops_when_cloud_never_created_stack(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    ticks = iter([0.0, 601.0, 601.0])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner, "_latest_observed_stack_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_discover_owned_cleanup_stack_ids", lambda _run_dir: [])

    class FakePty:
        run_dir = tmp_path
        env: dict[str, str] = {}

    pty = FakePty()
    with pytest.raises(RuntimeError, match="did not create a test Stack within 10 minutes"):
        runner._wait_for_latest_observed_stack_id(pty, exclude=set(), timeout=1800)
    assert pty.cloud_stack_not_created is True


def test_cleanup_ready_accepts_marker_already_drained_after_followup(monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    marker = "\x1b[>4;2m"

    class FakePty:
        transcript = "old prompt marker\n" + marker + "\nfollowup response\n" + marker
        events: list[dict[str, object]] = []

        def drain_output(self) -> None:
            return None

        def expect_optional(self, patterns, *, description, timeout):
            return True

        def expect_any(self, patterns, *, description, timeout):
            raise AssertionError("buffered prompt marker should avoid another blocking expect")

    pty = FakePty()
    followup_offset = pty.transcript.index("followup response")
    monkeypatch.setattr(runner, "_wait_for_cleanup_resource_status", lambda *_, **__: None)

    runner._wait_for_cleanup_completed_and_ready(
        pty,
        args,
        "stack-id",
        prompt_ready_since=followup_offset,
    )

    assert pty.events[-1]["description"] == "post-cleanup prompt input ready"
    assert pty.events[-1]["buffered"] is True


def test_raw_input_ready_ignores_buffered_marker_before_requested_offset() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    marker = "\x1b[>4;2m"

    class FakePty:
        transcript = "old prompt\n" + marker + "\nsecond deployment completed"
        events: list[dict[str, object]] = []
        expected = False

        def drain_output(self) -> None:
            return None

        def expect_any(self, patterns, *, description, timeout):
            self.expected = True
            return patterns[0]

    pty = FakePty()
    second_deployment_offset = pty.transcript.index("second deployment")

    runner._expect_raw_input_ready(
        pty,
        args,
        description="normal follow-up prompt input ready",
        since_offset=second_deployment_offset,
    )

    assert pty.expected is True
    assert not pty.events


def test_expect_any_since_accepts_buffered_cleanup_start_after_offset() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    cleanup_marker = "DeleteStack"

    class FakePty:
        transcript = "old prompt\nsecond deployment completed\n" + cleanup_marker
        events: list[dict[str, object]] = []

        def drain_output(self) -> None:
            return None

        def expect_any(self, patterns, *, description, timeout):
            raise AssertionError("buffered cleanup marker should avoid another blocking expect")

    pty = FakePty()
    second_deployment_offset = pty.transcript.index("second deployment")

    matched = runner._expect_any_since(
        pty,
        args,
        runner.REPL_INPUT_READY_PATTERNS + runner.CLEANUP_STARTED_PATTERNS,
        description="cleanup start or normal follow-up prompt input ready",
        timeout=args.stream_timeout,
        since_offset=second_deployment_offset,
    )

    assert matched in runner.CLEANUP_STARTED_PATTERNS
    assert pty.events[-1]["buffered"] is True


def test_expect_any_since_prefers_earliest_buffered_event_over_pattern_order() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    prompt_marker = "\x1b[>4;2m"

    class FakePty:
        # cleanup 先发生、prompt 后发生；调用方的 patterns 则故意把 prompt 放在前面。
        transcript = "second deployment completed\nDeleteStack\n" + prompt_marker
        events: list[dict[str, object]] = []

        def drain_output(self) -> None:
            return None

        def expect_any(self, patterns, *, description, timeout):
            raise AssertionError("buffered events should avoid another blocking expect")

    matched = runner._expect_any_since(
        FakePty(),
        args,
        runner.REPL_INPUT_READY_PATTERNS + runner.CLEANUP_STARTED_PATTERNS,
        description="cleanup start or normal follow-up prompt input ready",
        timeout=args.stream_timeout,
        since_offset=0,
    )

    assert matched in runner.CLEANUP_STARTED_PATTERNS


def test_scenario_runtime_paths_override_shared_sandbox_state(tmp_path: Path) -> None:
    runner = _load_runner()
    run_dir = tmp_path / "runs" / "case-run-1"
    shared_environment = {
        "IAC_CODE_CONFIG_DIR": "/home/iac_code_config",
        "IAC_CODE_CONFIG_BACKUP_DIR": "/home/iac_code_config_backup",
    }
    paths = runner.ScenarioRuntimePaths.for_run(
        run_dir,
        environment=shared_environment,
    )
    environment = paths.apply(shared_environment)

    assert paths.config_dir == Path("/home/iac_code_config/.e2e-runs/case-run-1")
    assert paths.backup_dir == Path("/home/iac_code_config_backup/.e2e-runs/case-run-1")
    assert environment["IAC_CODE_CONFIG_DIR"] == str(paths.config_dir)
    assert environment["IAC_CODE_CONFIG_BACKUP_DIR"] == str(paths.backup_dir)


def test_explicit_source_config_is_copied_to_isolated_repl_config(tmp_path: Path) -> None:
    runner = _load_runner()
    source = tmp_path / "source"
    source.mkdir()
    names = (".credentials.yml", ".cloud-credentials.yml", "settings.yml")
    for name in names:
        (source / name).write_text("fixture", encoding="utf-8")
    destination = tmp_path / "isolated" / "config"

    runner._copy_runtime_config(source, destination)

    for name in names:
        assert (destination / name).read_text(encoding="utf-8") == "fixture"
        if os.name != "nt":
            assert (destination / name).stat().st_mode & 0o777 == 0o600
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o700


def test_cleanup_ledger_lookup_uses_case_isolated_config_dir(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    from iac_code.services.session_storage import SessionStorage

    cwd = str(tmp_path / "workspace")
    session_id = "session-current"
    isolated_config = tmp_path / "isolated-config"
    shared_config = tmp_path / "shared-config"
    isolated_storage = SessionStorage(projects_dir=isolated_config / "projects")
    shared_storage = SessionStorage(projects_dir=shared_config / "projects")
    isolated_path = isolated_storage.session_dir(cwd, session_id) / "pipeline" / "cleanup.yaml"
    shared_path = shared_storage.session_dir(cwd, "session-stale") / "pipeline" / "cleanup.yaml"
    isolated_path.parent.mkdir(parents=True)
    shared_path.parent.mkdir(parents=True)
    isolated_path.write_text("observed_resources: []\n", encoding="utf-8")
    shared_path.write_text("observed_resources:\n  - resource_id: stale\n", encoding="utf-8")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(shared_config))

    pty = type(
        "FakePty",
        (),
        {
            "cwd": cwd,
            "session_id": session_id,
            "transcript": "",
            "env": {"IAC_CODE_CONFIG_DIR": str(isolated_config)},
        },
    )()

    assert runner._cleanup_ledger_path(pty) == isolated_path


def test_rollback_step5_cleanup_recovery_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])
    actions: list[tuple[str, str]] = []
    transcript = (
        "● Confirm and select (4/5)\n"
        "first-stack(first-stack-id) CREATE_COMPLETE\n"
        "● Confirm and select (4/5)\n"
        "second-stack(second-stack-id) CREATE_COMPLETE\n"
        "检测到 1 个回滚残留资源，开始清理流程。\n"
        "↺ 回滚清理恢复：1 条记录，1 条进行中。\n"
        "↺ 回滚清理 [完成] first-stack · 资源栈 first-stack-id · DELETE_COMPLETE\n"
    )
    _install_flow_fake_pty(monkeypatch, runner, transcript, actions)
    monkeypatch.setattr(
        runner,
        "_ensure_cleanup_network_target",
        lambda _args, _run_dir: runner.CleanupNetworkTarget(
            vpc_id="vpc-test",
            vpc_cidr="172.16.0.0/12",
            zone_id="cn-hangzhou-h",
            vswitch_cidr="172.31.255.0/24",
            rollback_vswitch_cidr="172.31.254.0/24",
        ),
    )
    monkeypatch.setattr(runner, "_wait_for_latest_observed_stack_id", lambda *_, **__: "first-stack-id")
    monkeypatch.setattr(runner, "_cleanup_target_stack_ids", lambda *_, **__: ["first-stack-id"])
    monkeypatch.setattr(
        runner,
        "_latest_observed_stack_id",
        lambda _pty, *, exclude: "second-stack-id" if "first-stack-id" in exclude else "first-stack-id",
    )
    deleted_stack_ids = _install_cleanup_teardown_fakes(monkeypatch, runner, tmp_path)

    assert runner.run_rollback_step5_cleanup_recovery(args, "rollback-step5-cleanup-recovery") == 0
    assert deleted_stack_ids == ["second-stack-id"]

    ordered_actions = [
        (kind, value)
        for kind, value in actions
        if kind in {"expect", "spawn", "terminate", "send-esc", "sendline", "select-default-candidate"}
        or (kind == "expect_optional" and value in {"cleanup resume summary", "cleanup completed"})
    ]
    assert ordered_actions == [
        ("spawn", ""),
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", runner._cleanup_pipeline_prompt(args, tmp_path)),
        ("expect", "initial candidate selection or clarification visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "first stack create started"),
        ("send-esc", "\x1b"),
        ("expect", "deploying interrupt input visible"),
        ("expect", "deploying interrupt input ready"),
        ("sendline", runner._cleanup_rollback_prompt(args, tmp_path)),
        ("expect", "post-rollback candidate selection or clarification visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after second deployment"),
        ("expect", "cleanup start or normal follow-up prompt input ready"),
        ("sendline", args.normal_followup_prompt),
        ("expect", "cleanup started after normal follow-up"),
        ("terminate", "True"),
        ("spawn", "--continue"),
        ("expect_optional", "cleanup resume summary"),
        ("expect_optional", "cleanup completed"),
        ("expect", "post-cleanup prompt input ready"),
        ("sendline", "/exit"),
        ("terminate", "False"),
    ]


def test_cleanup_recovery_uses_ledger_when_resume_summary_is_not_visible(monkeypatch) -> None:
    runner = _load_runner()
    args = runner.parse_args(["--allow-real-cloud"])
    calls: list[tuple[object, ...]] = []

    class FakePty:
        def expect_optional(self, patterns, *, description, timeout):
            calls.append((patterns, description, timeout))
            return False

    monkeypatch.setattr(
        runner,
        "_wait_for_cleanup_resource_status",
        lambda pty, stack_id, statuses, *, timeout: calls.append((pty, stack_id, statuses, timeout)),
    )
    pty = FakePty()

    assert runner._wait_for_cleanup_resume_summary_or_completion(pty, args, "first-stack-id") is False
    assert calls == [
        (runner.CLEANUP_RESUME_SUMMARY_PATTERNS, "cleanup resume summary", 5.0),
        (pty, "first-stack-id", {"completed"}, args.stream_timeout),
    ]


def test_candidate_wait_handles_extra_question_before_selection(monkeypatch):
    runner = _load_runner()
    patterns = iter([runner.ASK_USER_QUESTION_HEADING_PATTERNS[0], runner.CANDIDATE_SELECTION_PATTERNS[0]])
    calls = []
    pty = SimpleNamespace(expect_any=lambda *_args, **_kw: next(patterns))
    monkeypatch.setattr(runner, '_answer_legacy_repl_question', lambda *_: calls.append('answered'))
    monkeypatch.setattr(runner, '_expect_candidate_selection_ready', lambda *_a, **_kw: calls.append('ready'))
    runner._expect_candidate_selection(
        pty, SimpleNamespace(stream_timeout=1), description='candidate selection visible'
    )
    assert calls == ['answered', 'ready']


@pytest.mark.parametrize('cleanup', [False, True])
def test_candidate_wait_answers_durable_question_when_heading_was_drained(tmp_path, monkeypatch, cleanup):
    runner = _load_runner()
    meta = tmp_path / 'projects' / 'project' / 'session' / 'pipeline' / 'meta.yaml'
    meta.parent.mkdir(parents=True)
    meta.write_text(runner.yaml.safe_dump({'status': 'running', 'execution': {
        'pending_input_kind': 'ask_user_question', 'pending_ask_user_question_input': {
            'toolUseId': 'question-fixture', 'question': '确认用途?', 'allowFreeText': True}}}), encoding='utf-8')
    calls = []
    def expect(_patterns, **kwargs):
        boundary = kwargs.get('state_check')
        if not calls:
            assert callable(boundary), 'terminal heading already consumed: checkpoint must drive the wait'
            return boundary()
        return runner.CANDIDATE_SELECTION_PATTERNS[0]
    pty = SimpleNamespace(env={'IAC_CODE_CONFIG_DIR': str(tmp_path)}, expect_any=expect)
    def answer(*_):
        calls.append('answered')
        meta.write_text('status: running\nexecution: {}\n', encoding='utf-8')
    monkeypatch.setattr(runner, '_answer_legacy_repl_question', answer)
    monkeypatch.setattr(runner, '_expect_candidate_selection_ready', lambda *_a, **_kw: calls.append('ready'))
    wait = (runner._expect_candidate_selection_after_optional_asks if cleanup
            else runner._expect_candidate_selection)
    wait(pty, SimpleNamespace(stream_timeout=1), description='candidate selection visible')
    assert calls == ['answered', 'ready']


def test_candidate_checkpoint_cannot_treat_early_completion_as_selection(tmp_path):
    runner = _load_runner()
    meta = tmp_path / 'projects' / 'project' / 'session' / 'pipeline' / 'meta.yaml'
    meta.parent.mkdir(parents=True)
    meta.write_text('status: completed\nnormal_handoff: {status: succeeded}\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='completed before candidate selection'):
        runner._durable_candidate_boundary(SimpleNamespace(env={'IAC_CODE_CONFIG_DIR': str(tmp_path)}))


@pytest.mark.parametrize('status,handoff,expected_error', [
    ('failed', None, 'terminal checkpoint'), ('running', 'failed', 'normal handoff failed'),
])
def test_native_completion_wait_rejects_terminal_checkpoint(tmp_path, status, handoff, expected_error):
    runner = _load_runner()
    meta = tmp_path / 'projects' / 'project' / 'session' / 'pipeline' / 'meta.yaml'
    meta.parent.mkdir(parents=True)
    state = {'status': status}
    if handoff:
        state['normal_handoff'] = {'status': handoff}
    meta.write_text(runner.yaml.safe_dump(state), encoding="utf-8")
    pty = SimpleNamespace(env={'IAC_CODE_CONFIG_DIR': str(tmp_path)})
    with pytest.raises(RuntimeError, match=expected_error):
        runner._durable_completion_boundary(pty)


def test_native_completion_wait_requires_successful_handoff(tmp_path):
    runner = _load_runner()
    meta = tmp_path / 'projects' / 'project' / 'session' / 'pipeline' / 'meta.yaml'
    meta.parent.mkdir(parents=True)
    pty = SimpleNamespace(env={'IAC_CODE_CONFIG_DIR': str(tmp_path)})
    meta.write_text('status: completed\nnormal_handoff: {status: pending}\n', encoding="utf-8")
    assert runner._durable_completion_boundary(pty) is None
    meta.write_text('status: completed\nnormal_handoff: {status: succeeded}\n', encoding="utf-8")
    assert runner._durable_completion_boundary(pty) == runner.PIPELINE_FULLY_COMPLETED_PATTERNS[0]


@pytest.mark.parametrize('owned', [True, False])
def test_missing_ledger_label_requires_exact_cloud_name_before_cleanup(monkeypatch, tmp_path, owned):
    runner = _load_runner()
    args = runner.parse_args(['--allow-real-cloud', '--run-dir', str(tmp_path)])
    expected_name = runner._scenario_stack_name(tmp_path, 'scenario1')
    pty = SimpleNamespace(run_dir=tmp_path, env={}, cleanup_ledger={'observed_resources': [
        {'provider': 'ros', 'resource_type': 'stack', 'resource_id': 'observed-created-stack',
         'observed_action': 'CreateStack', 'resource_name': ''}]})
    deleted = _install_observed_stack_teardown_fakes(monkeypatch, runner,
        stack_name=expected_name if owned else 'unrelated-stack')
    checks, notes = {}, []
    runner._teardown_real_cloud_scenario_resources(args=args, scenario='scenario1', pty=pty,
                                                 checks=checks, notes=notes)
    assert deleted == (['observed-created-stack'] if owned else [])
    assert checks['teardown: observed ROS stacks deleted'] is owned
