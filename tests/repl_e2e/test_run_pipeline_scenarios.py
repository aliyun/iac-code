from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "repl" / "e2e" / "run_pipeline_scenarios.py"
    spec = importlib.util.spec_from_file_location("run_pipeline_scenarios", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

        def expect_any(self, patterns, *, description, timeout):
            actions.append(("expect", description))
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            actions.append(("expect_optional", description))
            return True

        def send(self, text, *, label="send"):
            actions.append((label, text))
            self.events.append({"type": label, "text": text, "transcript_offset": 0})

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
        "IAC_CODE_MODEL": "qwen3.6-plus",
    }

    redacted = runner._redact_sensitive_text(text, env)

    assert "sk-live-secret" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "qwen3.6-plus" not in redacted
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
    args = runner.parse_args(["--allow-real-cloud", "--provider", "dashscope", "--model", "qwen3.6-plus"])

    env = runner._build_child_env(args)

    assert env["HOME"] == "/Users/example"
    assert env["IAC_CODE_CONFIG_DIR"] == "/custom/iac"
    assert env["IAC_CODE_MODE"] == "pipeline"
    assert env["IAC_CODE_PROVIDER"] == "dashscope"
    assert env["IAC_CODE_MODEL"] == "qwen3.6-plus"
    assert env["PYTHONUTF8"] == "1"


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
        env={"IAC_CODE_API_KEY": "sk-secret123456", "IAC_CODE_MODEL": "qwen3.6-plus"},
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


def test_candidate_selection_waits_for_input_ready_sequence() -> None:
    runner = _load_runner()
    descriptions: list[str] = []

    class FakePty:
        def expect_any(self, patterns, *, description, timeout):
            descriptions.append(description)
            return patterns[0]

        def expect_optional(self, patterns, *, description, timeout):
            descriptions.append(description)
            return False

    args = runner.parse_args(["--allow-real-cloud"])

    runner._expect_candidate_selection(FakePty(), args, description="candidate selection visible")

    assert descriptions == ["candidate selection visible", "candidate selection controls ready"]


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
    pty = runner.ReplPty(args=args, run_dir=tmp_path, cwd=tmp_path, env={})
    pty.child = child

    matched = pty.expect_any((r"Pipeline completed",), description="pipeline completed", timeout=10)

    assert matched == r"Pipeline completed"
    assert child.sent == ["\x1b[5~\r"]
    assert any(event["type"] == "permission_prompt" for event in pty.events)
    assert any(event["type"] == "permission-prompt-response" for event in pty.events)


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
    pty = runner.ReplPty(args=args, run_dir=tmp_path, cwd=tmp_path, env={})
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
    assert "CreateStack 的 params.StackName 必须精确等于" in prompt
    assert stack_name in prompt
    assert "禁止使用默认或自动生成 StackName" in prompt


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

        def expect_any(self, patterns, *, description, timeout):
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
        ("expect", "candidate selection input ready"),
        ("send-esc", "\x1b"),
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
    monkeypatch.setattr(runner, "_wait_for_latest_observed_stack_id", lambda *_, **__: "first-stack-id")
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
        if kind in {"expect", "send-esc", "sendline", "select-default-candidate"}
        or (kind == "expect_optional" and value == "cleanup completed")
    ]
    assert ordered_actions == [
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", runner._cleanup_pipeline_prompt(args, tmp_path)),
        ("expect", "initial candidate selection visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "first stack create started"),
        ("send-esc", "\x1b"),
        ("expect", "deploying interrupt input ready"),
        ("sendline", runner._cleanup_rollback_prompt(args, tmp_path)),
        ("expect", "post-rollback candidate selection visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after second deployment"),
        ("sendline", args.normal_followup_prompt),
        ("expect", "cleanup started"),
        ("expect_optional", "cleanup completed"),
        ("expect", "post-cleanup prompt input ready"),
        ("sendline", "/exit"),
    ]


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
        or (kind == "expect_optional" and value == "cleanup completed")
    ]
    assert ordered_actions == [
        ("spawn", ""),
        ("expect", "initial prompt"),
        ("expect", "prompt input ready"),
        ("sendline", runner._cleanup_pipeline_prompt(args, tmp_path)),
        ("expect", "initial candidate selection visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "first stack create started"),
        ("send-esc", "\x1b"),
        ("expect", "deploying interrupt input ready"),
        ("sendline", runner._cleanup_rollback_prompt(args, tmp_path)),
        ("expect", "post-rollback candidate selection visible"),
        ("select-default-candidate", f"{args.selection_prompt}\r"),
        ("expect", "pipeline completed after second deployment"),
        ("sendline", args.normal_followup_prompt),
        ("expect", "cleanup started before kill"),
        ("terminate", "True"),
        ("spawn", "--continue"),
        ("expect", "cleanup resume summary"),
        ("expect_optional", "cleanup completed"),
        ("expect", "post-cleanup prompt input ready"),
        ("sendline", "/exit"),
        ("terminate", "False"),
    ]
