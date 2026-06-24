#!/usr/bin/env python3
"""Run real interactive REPL pipeline E2E scenarios.

This runner intentionally drives the public terminal interface through a PTY.
It uses the user's real configuration by default and must not be imported by
ordinary package code.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import shlex
import signal
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pexpect
except ImportError:  # pragma: no cover - exercised manually when dependency missing
    pexpect = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is part of the project runtime
    yaml = None  # type: ignore[assignment]


RUN_LOG_ROOT_NAME = "iac-code-repl-e2e-runs"
PTY_SEND_CHUNK_SIZE = 512
PTY_SEND_CHUNK_DELAY_SECONDS = 0.01
TEXT_IMAGE_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "a2a" / "e2e" / "fixtures" / "text-images"
TEXT_IMAGE_FIXTURE_FILENAMES = {
    "initial": "initial.png",
    "selection": "selection.png",
    "normal-followup": "normal-followup.png",
    "ask-first-answer": "ask-first-answer.png",
    "ask-second-answer": "ask-second-answer.png",
    "rollback-interrupt": "rollback-interrupt.png",
}
DEFAULT_INITIAL_PROMPT = "选择一个已有vpc，创建一个vswitch"
DEFAULT_SELECTION_PROMPT = "1"
DEFAULT_ASK_PROMPT = "我有个产品要上线"
DEFAULT_ASK_ANSWER = "我要创建云网络资源；本次只选择已有 VPC 创建一个 VSwitch，不部署 ECS、EIP、SLB 或 Nginx。"
DEFAULT_NORMAL_FOLLOWUP_PROMPT = "你刚才创建了什么"
DEFAULT_ROLLBACK_PROMPT = "回退到 intent_parsing，选择一个已有vpc，创建一个安全组"
DEFAULT_INVALID_SELECTION_PROMPT = "9"
DEFAULT_EVALUATE_RESUME_CONTINUE_PROMPT = "continue"
DEFAULT_CLEANUP_CONTINUE_PROMPT = (
    "只执行上面的回滚清理：仅删除待清理列表中的 stack id，完成后停止，不要删除或检查其他 stack。"
)
DEFAULT_PERMISSION_PROMPT_RESPONSE = "pageup-enter"

PIPELINE_STARTED_PATTERNS = (r"Pipeline", r"pipeline", r"intent_parsing", r"意图")
CANDIDATE_SELECTION_PATTERNS = (
    r"(?i)Confirm and select\s*\(\d+/\d+\)",
    r"(?i)confirm[ _-]+and[ _-]+select\s*\(\d+/\d+\)",
    r"确认并选择\s*\(\d+/\d+\)",
    r"候选选择\s*\(\d+/\d+\)",
)
CANDIDATE_EVALUATION_PATTERNS = (r"(?i)Evaluate candidates\s*\(\d+/\d+\)", r"evaluate_candidates")
ARCHITECTURE_PLANNING_PATTERNS = (r"(?i)Architecture planning\s*\(\d+/\d+\)", r"architecture_planning")
ASK_PATTERNS = (r"Ask user question", r"请.*输入", r"请.*补充", r"请描述", r"需要.*信息", r"澄清", r"问题")
PIPELINE_COMPLETED_PATTERNS = (
    r"(?i)Pipeline completed",
    r"CREATE_COMPLETE",
    r"部署成功",
    r"Stack ID",
    r"(?i)handoff",
    r"交接",
)
PIPELINE_FULLY_COMPLETED_PATTERNS = (r"(?i)Pipeline completed\.\s+Normal chat is now active\.",)
POST_ROLLBACK_PROGRESS_PATTERNS = (
    r"●\s*Intent parsing\s*\(1/5\)",
    r"●\s*Architecture planning\s*\(2/5\)",
    r"Step Intent parsing completed",
)
DEPLOYING_STEP_PATTERNS = (r"●\s*Deploying\s*\(5/5\)", r"CreateStack", r"开始部署")
CREATE_STACK_STARTED_PATTERNS = (r"ROS Stack\(CreateStack", r"CreateStack")
FIRST_STACK_CREATED_PATTERNS = (r"CREATE_COMPLETE", r"Stack ID", r"StackId", r"stack_id")
CLEANUP_STARTED_PATTERNS = (
    r"检测到\s*\d+\s*个回滚残留资源",
    r"开始清理流程",
    r"回滚清理\s*\[删除中\]",
    r"DeleteStack",
)
CLEANUP_RESUME_SUMMARY_PATTERNS = (r"回滚清理恢复", r"回滚清理")
CLEANUP_COMPLETED_PATTERNS = (r"DELETE_COMPLETE", r"回滚清理\s*\[完成\]", r"清理.*完成")
CLEANUP_DEPLOYMENT_FAILURE_PATTERNS = (
    r"\bCREATE_FAILED\b",
    r"RouteConflict",
    r"StackExists",
    r"InvalidCidrBlock",
)
ROS_STACK_DELETED_STATUSES = {"DELETE_COMPLETE"}
REPL_PROMPT_PATTERNS = (r"❯",)
REPL_INPUT_READY_PATTERNS = (r"\x1b\[>4;2m",)
INTERRUPT_INPUT_PATTERNS = (r"✎", r"interrupt", r"输入", r"Judging")
CANDIDATE_SELECTION_READY_PATTERNS = (
    r"Press number keys to select a candidate",
    r"Enter to confirm",
    r"按数字键.*候选",
)
PERMISSION_PROMPT_PATTERNS = (
    r"Yes, allow once",
    r"允许一次",
)
TERMINAL_ERROR_PATTERNS = (
    r"Traceback \(most recent call last\)",
    r"pexpect\.(?:TIMEOUT|EOF)",
    r"rejected_in_prompt",
    r"Permission.*reject",
    r"权限.*拒绝",
)
VSWITCH_EVIDENCE_PATTERNS = (
    r"ALIYUN::ECS::VSwitch",
    r"VSwitchId",
    r"vsw-[A-Za-z0-9]+",
    r"交换机\s*ID",
)
VSWITCH_MENTION_PATTERNS = (
    r"(?i)VSwitch",
    r"交换机",
    r"vsw-[A-Za-z0-9]+",
)
SECURITY_GROUP_EVIDENCE_PATTERNS = (
    r"ALIYUN::ECS::SecurityGroup",
    r"SecurityGroupId",
    r"sg-[A-Za-z0-9]+",
    r"安全组\s*ID",
)
SECURITY_GROUP_MENTION_PATTERNS = (
    r"(?i)SecurityGroup",
    r"安全组",
    r"sg-[A-Za-z0-9]+",
)
POSITIVE_VSWITCH_TARGET_PATTERNS = (
    r"ALIYUN::ECS::VSwitch",
    r"VSwitchId",
    r"vsw-[A-Za-z0-9]+",
    r"(?:创建|新建|目标资源|资源类型|部署).*?(?:VSwitch|交换机)",
)
NEGATED_VSWITCH_TARGET_LINE_PATTERNS = (
    r"(?i)(?:不|不要|禁止|避免|无需|不再|不能|不得|forbid|forbidden).*?(?:VSwitch|交换机)",
    r"(?i)(?:VSwitch|交换机).*?(?:forbid|forbidden|不创建|禁止|不要|无需)",
    r"(?i)(?:no|without).*?(?:VSwitch|switch)",
    r"(?i)(?:VSwitch|switch).*?(?:no|without)",
    r"(?i)(?:从|由|将需求从|把需求从).*?(?:创建|新建).*?(?:VSwitch|交换机).*?(?:改为|变更为|切换为|转为).*?(?:SecurityGroup|安全组)",
    r"(?i)from.*?(?:create|creating).*?(?:VSwitch|switch).*?to.*?(?:SecurityGroup|security group)",
    r'(?i)"product"\s*:\s*"VSwitch".*?"action"\s*:\s*"forbid"',
)
NEGATED_VSWITCH_TARGET_SPAN_PATTERNS = (
    r"(?is)(?:从|由|将需求从|把需求从).{0,80}(?:创建|新建).{0,80}(?:VSwitch|交换机).{0,160}(?:改为|变更为|切换为|转为).{0,80}(?:SecurityGroup|安全组)",
    r"(?is)from.{0,80}(?:create|creating).{0,80}(?:VSwitch|switch).{0,160}to.{0,80}(?:SecurityGroup|security group)",
)
ARCHITECTURE_PLANNING_HEADING_PATTERNS = (r"●\s*Architecture planning\s*\(2/5\)",)
EVALUATE_CANDIDATES_HEADING_PATTERNS = (r"●\s*Evaluate candidates\s*\(3/5\)",)
ASK_USER_QUESTION_HEADING_PATTERNS = (r"●\s*Ask user question",)

STACK_CREATING_SCENARIOS = frozenset(
    {
        "scenario1",
        "ask-waiting",
        "ask-waiting-resume",
        "image-initial",
        "image-ask-waiting-resume",
        "image-selection-waiting-resume",
        "image-normal-handoff",
        "selection-waiting-resume",
        "selection-invalid-then-valid",
        "evaluate-resume",
    }
)


@dataclass
class ScenarioRunResult:
    scenario: str
    run_dir: str
    passed: bool
    checks: dict[str, bool]
    elapsed_seconds: float
    abort_reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CleanupNetworkTarget:
    vpc_id: str
    vpc_cidr: str
    zone_id: str
    vswitch_cidr: str
    rollback_vswitch_cidr: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run interactive REPL pipeline E2E scenarios.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(_SCENARIOS),
        help="Scenario to run. Can be repeated. Defaults to scenario1.",
    )
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--cwd", default="", help="Child process cwd. Defaults to <run-dir>/workspace.")
    parser.add_argument("--run-root", default=str(Path(tempfile.gettempdir()) / RUN_LOG_ROOT_NAME))
    parser.add_argument("--run-dir", default="", help="Explicit run dir. Only valid with one scenario.")
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--stream-timeout", type=float, default=1800.0)
    parser.add_argument("--terminal-width", type=int, default=140)
    parser.add_argument("--terminal-height", type=int, default=40)
    parser.add_argument("--candidate-selection-ready-timeout", type=float, default=30.0)
    parser.add_argument("--leave-running", action="store_true")
    parser.add_argument(
        "--skip-final-teardown",
        action="store_true",
        help="Do not delete test-owned ROS stacks after scenario acceptance checks.",
    )
    parser.add_argument("--final-teardown-timeout", type=float, default=900.0)
    parser.add_argument("--cleanup-vpc-id", default="", help="Existing VPC ID to use for cleanup E2E scenarios.")
    parser.add_argument("--cleanup-vpc-cidr", default="", help="CIDR of --cleanup-vpc-id, used only in prompts.")
    parser.add_argument("--cleanup-zone-id", default="", help="Zone ID to use for cleanup E2E scenarios.")
    parser.add_argument(
        "--cleanup-vswitch-cidr",
        default="",
        help="Free VSwitch CIDR to use for the first stack in cleanup E2E scenarios.",
    )
    parser.add_argument(
        "--cleanup-rollback-vswitch-cidr",
        default="",
        help="Different free VSwitch CIDR to use for the post-rollback stack in cleanup E2E scenarios.",
    )
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--selection-prompt", default=DEFAULT_SELECTION_PROMPT)
    parser.add_argument(
        "--permission-prompt-response",
        default=DEFAULT_PERMISSION_PROMPT_RESPONSE,
        help="Permission prompt response: pageup-enter, up-enter, enter, or literal text.",
    )
    parser.add_argument("--ask-prompt", default=DEFAULT_ASK_PROMPT)
    parser.add_argument("--ask-answer", default=DEFAULT_ASK_ANSWER)
    parser.add_argument("--normal-followup-prompt", default=DEFAULT_NORMAL_FOLLOWUP_PROMPT)
    parser.add_argument("--rollback-prompt", default=DEFAULT_ROLLBACK_PROMPT)
    parser.add_argument("--invalid-selection-prompt", default=DEFAULT_INVALID_SELECTION_PROMPT)
    parser.add_argument("--evaluate-resume-continue-prompt", default=DEFAULT_EVALUATE_RESUME_CONTINUE_PROMPT)
    parser.add_argument("--cleanup-continue-prompt", default=DEFAULT_CLEANUP_CONTINUE_PROMPT)
    return parser.parse_args(argv)


def _selected_scenarios(args: argparse.Namespace) -> list[str]:
    return args.scenario or ["scenario1"]


def _validate_scenario_execution(args: argparse.Namespace, scenario: str) -> None:
    if scenario in _REAL_CLOUD_SCENARIOS and not args.allow_real_cloud:
        raise SystemExit("refusing to run real REPL pipeline scenario without --allow-real-cloud: " + scenario)


def _split_python_command(value: str) -> list[str]:
    parts = shlex.split(value, posix=(os.name != "nt"))
    if not parts:
        raise ValueError("--python must not be empty")
    return parts


def _build_child_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["IAC_CODE_MODE"] = "pipeline"
    if args.provider:
        env["IAC_CODE_PROVIDER"] = args.provider
    if args.model:
        env["IAC_CODE_MODEL"] = args.model
    if args.api_base:
        env["IAC_CODE_BASE_URL"] = args.api_base
    return env


def _redact_sensitive_text(text: str, env: dict[str, str] | None) -> str:
    redacted = text
    for name, value in (env or {}).items():
        if not value or len(value) < 6:
            continue
        upper = name.upper()
        if any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            redacted = redacted.replace(value, "<redacted>")
    redacted = re.sub(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,'\"}]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(authorization\s*[:=]\s*)[^\s,'\"}]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}", "sk-<redacted>", redacted)
    return redacted


_ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _normalize_transcript(text: str) -> str:
    text = _ANSI_PATTERN.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\b", "")
    return "\n".join(line.rstrip() for line in text.splitlines())


def _compact_text(text: str, *, max_chars: int = 800) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def _permission_prompt_response_sequence(value: str) -> str:
    if value == "pageup-enter":
        return "\x1b[5~\r"
    if value == "up-enter":
        return "\x1b[A\r"
    if value == "enter":
        return "\r"
    return f"{value}\r" if value else "\r"


def _sendline_to_child(child: Any, text: str, *, capture: Callable[[str], None] | None = None) -> None:
    if len(text) <= PTY_SEND_CHUNK_SIZE:
        child.sendline(text)
        return
    for offset in range(0, len(text), PTY_SEND_CHUNK_SIZE):
        child.send(text[offset : offset + PTY_SEND_CHUNK_SIZE])
        _drain_child_output(child, capture=capture)
        time.sleep(PTY_SEND_CHUNK_DELAY_SECONDS)
    _drain_child_output(child, capture=capture)
    child.sendline("")


def _drain_child_output(child: Any, *, capture: Callable[[str], None] | None = None) -> None:
    reader = getattr(child, "read_nonblocking", None)
    if not callable(reader):
        return
    while True:
        try:
            text = reader(size=4096, timeout=0)
        except Exception as exc:
            if pexpect is not None and isinstance(exc, pexpect.TIMEOUT):
                return
            return
        if not text:
            return
        if capture is not None:
            capture(str(text))


def _new_run_dir(root: Path) -> Path:
    run_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return root / run_name


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def _redacted_env_summary(env: dict[str, str]) -> dict[str, str]:
    keys = ["HOME", "IAC_CODE_CONFIG_DIR", "IAC_CODE_MODE", "IAC_CODE_PROVIDER", "IAC_CODE_MODEL", "IAC_CODE_BASE_URL"]
    return {key: _redact_sensitive_text(env[key], env) for key in keys if key in env}


def _scenario_run_dir(args: argparse.Namespace, scenario: str) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser().resolve()
    return _new_run_dir(Path(args.run_root).expanduser().resolve() / scenario)


class ReplPty:
    def __init__(self, *, args: argparse.Namespace, run_dir: Path, cwd: Path, env: dict[str, str]) -> None:
        if os.name == "nt":
            raise SystemExit("real PTY REPL E2E is POSIX-only")
        if pexpect is None:
            raise RuntimeError("pexpect is required. Install dependencies with: uv sync --all-extras")
        self.args = args
        self.run_dir = run_dir
        self.cwd = cwd
        self.env = env
        self.events: list[dict[str, Any]] = []
        self.raw_chunks: list[str] = []
        self.child: Any | None = None
        self._live_transcript = False

    @property
    def transcript(self) -> str:
        return "".join(self.raw_chunks)

    def spawn(self, *, extra_args: list[str] | None = None) -> None:
        command = [
            *_split_python_command(self.args.python),
            "-m",
            "iac_code.cli.main",
            "--permission-mode",
            "bypass_permissions",
            *(extra_args or []),
        ]
        self.events.append({"type": "spawn", "command": command, "cwd": str(self.cwd), "at": _utc_now()})
        self.child = pexpect.spawn(
            command[0],
            command[1:],
            cwd=str(self.cwd),
            env=self.env,
            encoding="utf-8",
            codec_errors="replace",
            timeout=self.args.timeout,
            dimensions=(self.args.terminal_height, self.args.terminal_width),
        )
        self.child.logfile_read = _TranscriptCapture(self)
        self._live_transcript = True

    def sendline(self, text: str) -> None:
        transcript_offset = len(self.transcript)
        _sendline_to_child(self._require_child(), text, capture=self._capture_child_output_force)
        self.events.append(
            {
                "type": "sendline",
                "text": _redact_sensitive_text(text, self.env),
                "transcript_offset": transcript_offset,
                "at": _utc_now(),
            }
        )

    def send(self, text: str, *, label: str = "send") -> None:
        transcript_offset = len(self.transcript)
        self._require_child().send(text)
        self.events.append(
            {
                "type": label,
                "text": _redact_sensitive_text(text, self.env),
                "transcript_offset": transcript_offset,
                "at": _utc_now(),
            }
        )

    def paste_image_fixture(self, image_key: str) -> Path:
        path = _text_image_fixture_path(image_key)
        transcript_offset = len(self.transcript)
        child = self._require_child()
        child.send(f"\x1b[200~{path}\x1b[201~")
        _drain_child_output(child, capture=self._capture_child_output_force)
        self.events.append(
            {
                "type": "paste-image-fixture",
                "image_key": image_key,
                "path": _redact_sensitive_text(str(path), self.env),
                "transcript_offset": transcript_offset,
                "at": _utc_now(),
            }
        )
        return path

    def expect_any(self, patterns: tuple[str, ...], *, description: str, timeout: float) -> str:
        child = self._require_child()
        deadline = time.monotonic() + timeout
        all_patterns = list(patterns) + list(PERMISSION_PROMPT_PATTERNS)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {description}")
                index = child.expect(all_patterns, timeout=remaining)
                self._capture_child_output(f"{child.before}{child.after}")
                if index < len(patterns):
                    matched = patterns[index]
                    self.events.append(
                        {
                            "type": "expect",
                            "description": description,
                            "pattern": matched,
                            "passed": True,
                            "at": _utc_now(),
                        }
                    )
                    return matched
                matched = PERMISSION_PROMPT_PATTERNS[index - len(patterns)]
                self.events.append(
                    {
                        "type": "permission_prompt",
                        "description": description,
                        "pattern": matched,
                        "at": _utc_now(),
                    }
                )
                self.send(
                    _permission_prompt_response_sequence(self.args.permission_prompt_response),
                    label="permission-prompt-response",
                )
        except Exception as exc:
            self._capture_child_output(str(getattr(child, "before", "") or ""))
            tail = _compact_text(_normalize_transcript(self.transcript)[-2000:])
            self.events.append(
                {
                    "type": "expect",
                    "description": description,
                    "patterns": list(patterns),
                    "passed": False,
                    "error": str(exc),
                    "tail": _redact_sensitive_text(tail, self.env),
                    "at": _utc_now(),
                }
            )
            raise

    def expect_optional(self, patterns: tuple[str, ...], *, description: str, timeout: float) -> bool:
        child = self._require_child()
        try:
            index = child.expect(list(patterns), timeout=timeout)
            matched = patterns[index]
            self.events.append(
                {
                    "type": "expect",
                    "description": description,
                    "pattern": matched,
                    "passed": True,
                    "optional": True,
                    "at": _utc_now(),
                }
            )
            return True
        except Exception as exc:
            if pexpect is None or not isinstance(exc, pexpect.TIMEOUT):
                tail = _compact_text(_normalize_transcript(self.transcript)[-2000:])
                self.events.append(
                    {
                        "type": "expect",
                        "description": description,
                        "patterns": list(patterns),
                        "passed": False,
                        "optional": True,
                        "error": str(exc),
                        "tail": _redact_sensitive_text(tail, self.env),
                        "at": _utc_now(),
                    }
                )
                raise
            tail = _compact_text(_normalize_transcript(self.transcript)[-2000:])
            self.events.append(
                {
                    "type": "expect",
                    "description": description,
                    "patterns": list(patterns),
                    "passed": False,
                    "optional": True,
                    "tail": _redact_sensitive_text(tail, self.env),
                    "at": _utc_now(),
                }
            )
            return False

    def terminate(self, *, force: bool = False) -> None:
        child = self.child
        if child is None:
            return
        try:
            if force:
                child.kill(signal.SIGKILL)
            else:
                child.terminate(force=True)
        finally:
            self._capture_child_output(str(getattr(child, "before", "") or ""))
            self.events.append({"type": "terminate", "force": force, "at": _utc_now()})

    def _capture_child_output(self, text: str) -> None:
        if text and not self._live_transcript:
            self.raw_chunks.append(text)

    def _capture_child_output_force(self, text: str) -> None:
        if text:
            self.raw_chunks.append(text)

    def _require_child(self) -> Any:
        if self.child is None:
            raise RuntimeError("REPL child has not been spawned")
        return self.child


class _TranscriptCapture:
    def __init__(self, pty: ReplPty) -> None:
        self._pty = pty

    def write(self, text: str) -> None:
        if text:
            self._pty.raw_chunks.append(text)

    def flush(self) -> None:
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_json_value(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_text(value, env)
    if isinstance(value, list):
        return [_redact_json_value(item, env) for item in value]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            upper = key_text.upper()
            if any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTHORIZATION")):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = _redact_json_value(item, env)
        return redacted
    return value


def _write_run_artifacts(
    *,
    run_dir: Path,
    env: dict[str, str],
    raw_transcript: str,
    events: list[dict[str, Any]],
    result: ScenarioRunResult,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    redacted_raw = _redact_sensitive_text(raw_transcript, env)
    normalized = _normalize_transcript(redacted_raw)
    (run_dir / "transcript.raw.log").write_text(redacted_raw, encoding="utf-8")
    (run_dir / "transcript.normalized.log").write_text(normalized, encoding="utf-8")
    _write_json(run_dir / "child.env.json", _redacted_env_summary(env))
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(_redact_json_value(event, env), ensure_ascii=False, default=str) + "\n")
    _write_json(run_dir / "summary.json", _redact_json_value(asdict(result), env))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scenarios = _selected_scenarios(args)
    if args.run_dir and len(scenarios) != 1:
        raise SystemExit("--run-dir can only be used with a single --scenario")
    for scenario in scenarios:
        _validate_scenario_execution(args, scenario)
    results = [_SCENARIOS[scenario](args, scenario) for scenario in scenarios]
    return 0 if all(code == 0 for code in results) else 1


def _run_with_pty(
    args: argparse.Namespace,
    scenario: str,
    callback: Callable[[ReplPty, dict[str, bool]], None],
) -> int:
    started = time.monotonic()
    run_dir = _scenario_run_dir(args, scenario)
    workspace_dir = Path(args.cwd).expanduser().resolve() if args.cwd else run_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    env = _build_child_env(args)
    pty = ReplPty(args=args, run_dir=run_dir, cwd=workspace_dir, env=env)
    checks: dict[str, bool] = {}
    notes: list[str] = []
    abort_reason = ""
    passed = False
    acceptance_applied = False
    teardown_applied = False

    try:
        pty.spawn()
        callback(pty, checks)
        _apply_acceptance_checks(scenario, args, pty, checks)
        acceptance_applied = True
        _teardown_real_cloud_scenario_resources(args=args, scenario=scenario, pty=pty, checks=checks, notes=notes)
        teardown_applied = True
        passed = all(checks.values()) if checks else True
    except BaseException as exc:
        abort_reason = f"{type(exc).__name__}: {exc}"
        notes.append(abort_reason)
        passed = False
    finally:
        if not acceptance_applied:
            try:
                _apply_acceptance_checks(scenario, args, pty, checks)
                acceptance_applied = True
            except BaseException as exc:
                notes.append(f"acceptance check failed: {type(exc).__name__}: {exc}")
        if acceptance_applied and not teardown_applied:
            try:
                _teardown_real_cloud_scenario_resources(
                    args=args,
                    scenario=scenario,
                    pty=pty,
                    checks=checks,
                    notes=notes,
                )
                teardown_applied = True
                if passed:
                    passed = all(checks.values()) if checks else True
            except BaseException as exc:
                notes.append(f"final teardown failed: {type(exc).__name__}: {exc}")
                if passed:
                    passed = False
        if not args.leave_running:
            try:
                pty.terminate()
            except BaseException as exc:
                notes.append(f"terminal child termination failed: {type(exc).__name__}: {exc}")
                if passed:
                    passed = False
        result = ScenarioRunResult(
            scenario=scenario,
            run_dir=str(run_dir),
            passed=passed,
            checks=checks,
            elapsed_seconds=round(time.monotonic() - started, 3),
            abort_reason=abort_reason,
            notes=notes,
        )
        _write_run_artifacts(run_dir=run_dir, env=env, raw_transcript=pty.transcript, events=pty.events, result=result)
        _print_result(result)

    return 0 if passed else 1


def _print_result(result: ScenarioRunResult) -> None:
    print(f"\nREPL pipeline scenario: {result.scenario}")
    print(f"run_dir: {result.run_dir}")
    if result.abort_reason:
        print(f"abort_reason: {_compact_text(result.abort_reason, max_chars=1000)}")
    if result.notes:
        print("\nnotes:")
        for note in result.notes:
            print(f"  - {_compact_text(note, max_chars=1000)}")
    print("\nchecks:")
    for name, passed in result.checks.items():
        print(f"  {'OK' if passed else 'FAIL'} {name}")
    print(f"\nRESULT: {'PASS' if result.passed else 'FAIL'}")


def _has_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _count_pattern(text: str, patterns: tuple[str, ...]) -> int:
    return sum(len(re.findall(pattern, text)) for pattern in patterns)


def _resume_spawns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("type") == "spawn" and "--continue" in [str(item) for item in event.get("command", [])]
    ]


def _event_index(events: list[dict[str, Any]], event_type: str) -> int | None:
    for index, event in enumerate(events):
        if event.get("type") == event_type:
            return index
    return None


def _event_before(events: list[dict[str, Any]], before_type: str, after_type: str) -> bool:
    before = _event_index(events, before_type)
    after = _event_index(events, after_type)
    return before is not None and after is not None and before < after


def _has_sendline_event(events: list[dict[str, Any]], text: str) -> bool:
    return any(event.get("type") == "sendline" and event.get("text") == text for event in events)


def _has_image_fixture_event(events: list[dict[str, Any]], image_key: str) -> bool:
    return any(event.get("type") == "paste-image-fixture" and event.get("image_key") == image_key for event in events)


def _has_vswitch_business_evidence(transcript: str) -> bool:
    if _has_any_pattern(transcript, VSWITCH_EVIDENCE_PATTERNS):
        return True
    has_vswitch_text = bool(re.search(r"(?i)VSwitch|交换机", transcript))
    has_deploy_result = bool(re.search(r"Stack ID|Stack 名称|CREATE_COMPLETE|部署成功", transcript))
    return has_vswitch_text and has_deploy_result


def _has_vswitch_answer_evidence(text: str) -> bool:
    return _has_any_pattern(text, VSWITCH_MENTION_PATTERNS)


def _has_security_group_target_evidence(text: str) -> bool:
    return _has_any_pattern(text, SECURITY_GROUP_EVIDENCE_PATTERNS + SECURITY_GROUP_MENTION_PATTERNS)


def _has_positive_vswitch_target_evidence(text: str) -> bool:
    cleaned_text = text
    for pattern in NEGATED_VSWITCH_TARGET_SPAN_PATTERNS:
        cleaned_text = re.sub(pattern, "", cleaned_text)
    positive_context_lines = [
        line for line in cleaned_text.splitlines() if not _has_any_pattern(line, NEGATED_VSWITCH_TARGET_LINE_PATTERNS)
    ]
    return _has_any_pattern("\n".join(positive_context_lines), POSITIVE_VSWITCH_TARGET_PATTERNS)


def _last_event_suffix(
    transcript: str,
    events: list[dict[str, Any]],
    *,
    event_type: str,
    text: str | None = None,
) -> str:
    offset: int | None = None
    for event in events:
        if event.get("type") != event_type:
            continue
        if text is not None and event.get("text") != text:
            continue
        raw_offset = event.get("transcript_offset")
        if isinstance(raw_offset, int) and raw_offset >= 0:
            offset = raw_offset
    if offset is None:
        return ""
    return transcript[offset:]


def _suffix_after_sendline_text(
    transcript: str,
    events: list[dict[str, Any]],
    text: str,
) -> str:
    suffix = _normalize_transcript(_last_event_suffix(transcript, events, event_type="sendline", text=text))
    normalized_text = _normalize_transcript(text)
    if normalized_text and normalized_text in suffix:
        return suffix.split(normalized_text, 1)[1]
    return suffix


def _suffix_after_image_fixture(
    transcript: str,
    events: list[dict[str, Any]],
    image_key: str,
) -> str:
    offset: int | None = None
    for event in events:
        if event.get("type") != "paste-image-fixture" or event.get("image_key") != image_key:
            continue
        raw_offset = event.get("transcript_offset")
        if isinstance(raw_offset, int) and raw_offset >= 0:
            offset = raw_offset
    if offset is None:
        return ""
    return _normalize_transcript(transcript[offset:])


def _add_acceptance_check(checks: dict[str, bool], name: str, passed: bool) -> None:
    checks[f"acceptance: {name}"] = bool(passed)


def _cleanup_stack_name(run_dir: Path, label: str) -> str:
    suffix = Path(run_dir).name.rsplit("-", maxsplit=1)[-1] or "stack"
    safe_label = "".join(ch if ch.isalnum() else "-" for ch in label.lower()).strip("-") or "stack"
    return f"iac-e2e-{suffix[:12]}-{safe_label}"[:128]


def _scenario_stack_name(run_dir: Path, scenario: str) -> str:
    suffix = Path(run_dir).name.rsplit("-", maxsplit=1)[-1] or "stack"
    safe_scenario = "".join(ch if ch.isalnum() else "-" for ch in scenario.lower()).strip("-") or "scenario"
    return f"iac-e2e-{suffix[:12]}-{safe_scenario}"[:128]


def _stack_name_constraint(run_dir: Path, scenario: str) -> str:
    stack_name = _scenario_stack_name(run_dir, scenario)
    return f"本次 CreateStack 的 params.StackName 必须精确等于 `{stack_name}`，禁止使用默认或自动生成 StackName。"


def _stack_creating_prompt(text: str, run_dir: Path, scenario: str) -> str:
    return f"{text}。{_stack_name_constraint(run_dir, scenario)}"


def _text_image_fixture_path(image_key: str) -> Path:
    filename = TEXT_IMAGE_FIXTURE_FILENAMES.get(image_key)
    if not filename:
        raise KeyError(f"unknown text image fixture: {image_key}")
    path = (TEXT_IMAGE_FIXTURE_ROOT / filename).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"text image fixture not found: {path}")
    return path


def _submit_image_fixture(pty: ReplPty, image_key: str, *, caption: str = "") -> None:
    pty.paste_image_fixture(image_key)
    if caption:
        pty.sendline(caption)
    else:
        pty.send("\r", label="submit-image")


def _cleanup_network_target_from_args(args: argparse.Namespace) -> CleanupNetworkTarget | None:
    if not (
        args.cleanup_vpc_id
        and args.cleanup_zone_id
        and args.cleanup_vswitch_cidr
        and args.cleanup_rollback_vswitch_cidr
    ):
        return None
    return CleanupNetworkTarget(
        vpc_id=args.cleanup_vpc_id,
        vpc_cidr=args.cleanup_vpc_cidr,
        zone_id=args.cleanup_zone_id,
        vswitch_cidr=args.cleanup_vswitch_cidr,
        rollback_vswitch_cidr=args.cleanup_rollback_vswitch_cidr,
    )


def _cleanup_network_prompt_fragment(args: argparse.Namespace, *, rollback: bool) -> str:
    target = _cleanup_network_target_from_args(args)
    if target is None:
        return (
            "必须先读取所选 VPC 的 CIDR，并选择属于该 VPC CIDR 的未占用 VSwitch CIDR；"
            "第一次和回退后的第二次部署必须使用两个不同的合法未占用 VSwitch CIDR。"
        )

    vpc_cidr = f"（CIDR `{target.vpc_cidr}`）" if target.vpc_cidr else ""
    if rollback:
        return (
            f"固定使用已有 VPC `{target.vpc_id}`{vpc_cidr}、可用区 `{target.zone_id}`；"
            f"本次重新部署只创建安全组，CreateStack 模板参数必须显式设置 VpcId=`{target.vpc_id}`。"
            "禁止创建 VSwitch，禁止在第二个栈中使用 CidrBlock 或模板默认 CidrBlock。"
        )

    return (
        f"固定使用已有 VPC `{target.vpc_id}`{vpc_cidr}、可用区 `{target.zone_id}`、"
        f"首个 VSwitch CIDR `{target.vswitch_cidr}`；首次 CreateStack 模板参数必须显式设置 "
        f"VpcId=`{target.vpc_id}`、ZoneId=`{target.zone_id}`、CidrBlock=`{target.vswitch_cidr}`。"
        "禁止使用模板默认 CidrBlock。"
    )


def _cleanup_pipeline_prompt(args: argparse.Namespace, run_dir: Path) -> str:
    first_stack_name = _cleanup_stack_name(run_dir, "first")
    return (
        f"{args.initial_prompt}。第一次 CreateStack 的 params.StackName 必须精确等于 `{first_stack_name}`，"
        "禁止使用模板名、候选方案名或 vswitch-in-existing-vpc，也不能复用已有资源栈。"
        f"{_cleanup_network_prompt_fragment(args, rollback=False)}"
    )


def _cleanup_rollback_prompt(args: argparse.Namespace, run_dir: Path) -> str:
    second_stack_name = _cleanup_stack_name(run_dir, "second")
    return (
        f"{args.rollback_prompt}。重新部署时 CreateStack 的 params.StackName 必须精确等于 `{second_stack_name}`，"
        "禁止使用模板名、候选方案名或 vswitch-in-existing-vpc，也不能复用已有资源栈。"
        "本次回退后的新方案只创建安全组，不创建 VSwitch。"
        f"{_cleanup_network_prompt_fragment(args, rollback=True)}"
    )


async def _call_aliyun_api_async(product: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
    from iac_code.tools.base import ToolContext
    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi

    result = await AliyunApi().execute(
        tool_input={"product": product, "action": action, "params": params},
        context=ToolContext(),
    )
    if result.is_error:
        raise RuntimeError(_compact_text(result.content, max_chars=1000))
    body = json.loads(result.content)
    return body if isinstance(body, dict) else {}


def _call_aliyun_api(product: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_call_aliyun_api_async(product, action, params))


def _nested_api_items(data: dict[str, Any], outer_key: str, inner_key: str) -> list[dict[str, Any]]:
    outer = data.get(outer_key)
    if isinstance(outer, dict):
        items = outer.get(inner_key) or outer.get(inner_key.lower()) or []
    else:
        items = outer or []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _find_available_vswitch_cidrs(vpc_cidr: str, used_cidrs: Iterable[str], *, count: int) -> list[str]:
    try:
        vpc_network = ipaddress.ip_network(vpc_cidr, strict=False)
    except ValueError:
        return []
    if not isinstance(vpc_network, ipaddress.IPv4Network):
        return []

    used_networks: list[ipaddress.IPv4Network] = []
    for cidr in used_cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network):
            used_networks.append(network)

    prefixlen = max(24, vpc_network.prefixlen)
    available: list[str] = []
    if prefixlen == vpc_network.prefixlen:
        if not any(vpc_network.overlaps(used) for used in used_networks):
            available.append(str(vpc_network))
        return available

    for subnet in reversed(list(vpc_network.subnets(new_prefix=prefixlen))):
        if not any(subnet.overlaps(used) for used in used_networks):
            available.append(str(subnet))
            used_networks.append(subnet)
            if len(available) >= count:
                return available
    return available


def _find_available_vswitch_cidr(vpc_cidr: str, used_cidrs: Iterable[str]) -> str | None:
    cidrs = _find_available_vswitch_cidrs(vpc_cidr, used_cidrs, count=1)
    return cidrs[0] if cidrs else None


def _discover_cleanup_network_target() -> CleanupNetworkTarget:
    vpcs_data = _call_aliyun_api("vpc", "DescribeVpcs", {"PageSize": 50})
    for vpc in _nested_api_items(vpcs_data, "Vpcs", "Vpc"):
        vpc_id = str(vpc.get("VpcId") or "")
        vpc_cidr = str(vpc.get("CidrBlock") or "")
        if not vpc_id or not vpc_cidr or str(vpc.get("Status") or "") != "Available":
            continue

        vswitches_data = _call_aliyun_api("vpc", "DescribeVSwitches", {"VpcId": vpc_id, "PageSize": 50})
        vswitches = _nested_api_items(vswitches_data, "VSwitches", "VSwitch")
        zone_ids = [str(item.get("ZoneId") or "") for item in vswitches if str(item.get("ZoneId") or "")]
        used_cidrs = [str(item.get("CidrBlock") or "") for item in vswitches if str(item.get("CidrBlock") or "")]
        vswitch_cidrs = _find_available_vswitch_cidrs(vpc_cidr, used_cidrs, count=2)
        if zone_ids and len(vswitch_cidrs) >= 2:
            return CleanupNetworkTarget(
                vpc_id=vpc_id,
                vpc_cidr=vpc_cidr,
                zone_id=zone_ids[0],
                vswitch_cidr=vswitch_cidrs[0],
                rollback_vswitch_cidr=vswitch_cidrs[1],
            )

    raise RuntimeError("No available existing VPC with a free VSwitch CIDR was found for cleanup E2E.")


def _ensure_cleanup_network_target(args: argparse.Namespace, run_dir: Path) -> CleanupNetworkTarget:
    target = _cleanup_network_target_from_args(args)
    if target is None:
        target = _discover_cleanup_network_target()
        args.cleanup_vpc_id = target.vpc_id
        args.cleanup_vpc_cidr = target.vpc_cidr
        args.cleanup_zone_id = target.zone_id
        args.cleanup_vswitch_cidr = target.vswitch_cidr
        args.cleanup_rollback_vswitch_cidr = target.rollback_vswitch_cidr
    _write_json(Path(run_dir) / "cleanup-network-target.json", asdict(target))
    return target


def _session_id_from_transcript(transcript: str) -> str | None:
    patterns = (
        r"\bSession:\s*([0-9a-fA-F][0-9a-fA-F-]{7,})",
        r"\bsession_id[\"'\s:=]+([0-9a-fA-F][0-9a-fA-F-]{7,})",
        r"\bsession[\"'\s:=]+([0-9a-fA-F][0-9a-fA-F-]{7,})",
    )
    for pattern in patterns:
        match = re.search(pattern, transcript)
        if match:
            return match.group(1)
    return None


def _cleanup_ledger_path(pty: Any) -> Path | None:
    explicit = getattr(pty, "cleanup_ledger_path", None)
    if explicit:
        return Path(explicit)

    cwd = str(getattr(pty, "cwd", "") or "")
    if not cwd:
        return None
    session_id = str(getattr(pty, "session_id", "") or "") or _session_id_from_transcript(
        str(getattr(pty, "transcript", "") or "")
    )
    try:
        from iac_code.services.session_storage import SessionStorage

        storage = SessionStorage()
        if session_id:
            return Path(storage.session_dir(cwd, session_id)) / "pipeline" / "cleanup.yaml"

        project_dir_for = getattr(storage, "_project_dir_for", None)
        if callable(project_dir_for):
            project_dir = Path(project_dir_for(cwd))
            candidates = sorted(
                project_dir.glob("*/pipeline/cleanup.yaml"),
                key=lambda path: path.stat().st_mtime if path.exists() else 0,
                reverse=True,
            )
            if candidates:
                return candidates[0]
    except Exception:
        return None
    return None


def _cleanup_ledger_data(pty: Any) -> dict[str, Any]:
    inline = getattr(pty, "cleanup_ledger", None)
    if isinstance(inline, dict):
        return inline
    path = _cleanup_ledger_path(pty)
    if path is None or yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, Exception):
        return {}
    return data if isinstance(data, dict) else {}


def _cleanup_ledger_items(pty: Any, key: str) -> list[dict[str, Any]]:
    values = _cleanup_ledger_data(pty).get(key)
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _is_ros_stack_resource(resource: dict[str, Any]) -> bool:
    provider = str(resource.get("provider") or "").lower()
    resource_type = str(resource.get("resource_type") or resource.get("resourceType") or "").lower()
    return provider == "ros" and resource_type == "stack"


def _string_from_mapping(mapping: Any, *keys: str) -> str | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _unique_strings(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _latest_observed_stack_id(pty: Any, *, exclude: set[str]) -> str | None:
    resources = _cleanup_ledger_items(pty, "observed_resources")
    for resource in reversed(resources):
        if not _is_ros_stack_resource(resource):
            continue
        action = str(resource.get("observed_action") or resource.get("observedAction") or resource.get("action") or "")
        if action and action != "CreateStack":
            continue
        stack_id = _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        if stack_id and stack_id not in exclude:
            return stack_id
    return None


def _is_create_stack_observation(resource: dict[str, Any]) -> bool:
    action = str(resource.get("observed_action") or resource.get("observedAction") or resource.get("action") or "")
    return not action or action == "CreateStack"


def _observed_create_stack_resources(pty: Any) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resource in _cleanup_ledger_items(pty, "observed_resources"):
        if not _is_ros_stack_resource(resource) or not _is_create_stack_observation(resource):
            continue
        stack_id = _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        if not stack_id or stack_id in seen:
            continue
        seen.add(stack_id)
        resources.append(resource)
    return resources


def _observed_create_stack_ids(pty: Any) -> list[str]:
    return _unique_strings(
        _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        for resource in _observed_create_stack_resources(pty)
    )


def _observed_create_stack_names(pty: Any) -> list[str]:
    return _unique_strings(
        _string_from_mapping(resource, "resource_name", "resourceName", "stack_name", "stackName")
        for resource in _observed_create_stack_resources(pty)
    )


def _wait_for_latest_observed_stack_id(pty: Any, *, exclude: set[str], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stack_id = _latest_observed_stack_id(pty, exclude=exclude)
        if stack_id:
            return stack_id
        time.sleep(0.5)
    raise TimeoutError("Timed out waiting for rollback cleanup ledger to observe a ROS stack")


def _cleanup_target_stack_ids(pty: Any, *, exclude: set[str]) -> list[str]:
    stack_ids: list[str] = []
    for resource in _cleanup_ledger_items(pty, "cleanup_resources"):
        if not _is_ros_stack_resource(resource):
            continue
        if resource.get("cleanup_required") is False or resource.get("cleanupRequired") is False:
            continue
        stack_id = _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        if stack_id and stack_id not in exclude:
            stack_ids.append(stack_id)
    return _unique_strings(stack_ids)


def _wait_for_cleanup_target_stack_ids(pty: Any, *, exclude: set[str], timeout: float) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stack_ids = _cleanup_target_stack_ids(pty, exclude=exclude)
        if stack_ids:
            return stack_ids
        time.sleep(0.5)
    raise TimeoutError("Timed out waiting for rollback cleanup ledger to record target stacks")


def _cleanup_resource_for_stack(pty: Any, stack_id: str | None) -> dict[str, Any] | None:
    if not stack_id:
        return None
    for resource in _cleanup_ledger_items(pty, "cleanup_resources"):
        if _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId") == stack_id:
            return resource
    return None


def _cleanup_resource_completed(resource: dict[str, Any] | None) -> bool:
    if not isinstance(resource, dict):
        return False
    cleanup_status = resource.get("cleanupStatus") or resource.get("cleanup_status") or resource.get("status")
    stack_status = resource.get("stackStatus") or resource.get("progressStatus") or resource.get("progress_status")
    return cleanup_status == "completed" and stack_status == "DELETE_COMPLETE"


def _wait_for_cleanup_resource_status(pty: Any, stack_id: str, statuses: set[str], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resource = _cleanup_resource_for_stack(pty, stack_id)
        status = ""
        if isinstance(resource, dict):
            status = str(
                resource.get("cleanup_status") or resource.get("cleanupStatus") or resource.get("status") or ""
            )
        if status in statuses:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for cleanup ledger status {sorted(statuses)} on {stack_id}")


def _cleanup_history_has_event(pty: Any, stack_id: str | None, event_types: set[str]) -> bool:
    if not stack_id:
        return False
    for item in _cleanup_ledger_items(pty, "history"):
        event_type = str(item.get("type") or item.get("event_type") or item.get("eventType") or "")
        if event_type not in event_types:
            continue
        resource = item.get("resource")
        resource_id = _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        resource_id = resource_id or _string_from_mapping(item, "resource_id", "resourceId", "stack_id", "stackId")
        if resource_id == stack_id:
            return True
    return False


def _capture_ros_stack_states(pty: Any, stack_ids: Iterable[str], name: str) -> dict[str, dict[str, Any]]:
    existing = getattr(pty, "ros_stack_states", None)
    states: dict[str, dict[str, Any]] = {}
    if isinstance(existing, dict):
        states.update({str(key): value for key, value in existing.items() if isinstance(value, dict)})

    missing = [stack_id for stack_id in _unique_strings(stack_ids) if stack_id not in states]
    for stack_id in missing:
        region_id = _region_for_stack(pty, stack_id)
        states[stack_id] = _get_ros_stack_state(
            stack_id=stack_id,
            region_id=region_id,
            redaction_env=getattr(pty, "env", {}),
        )

    run_dir = getattr(pty, "run_dir", None)
    env = getattr(pty, "env", {})
    if run_dir is not None:
        _write_json(
            Path(run_dir) / f"{name}.ros-stack-states.json",
            _redact_json_value(states, env if isinstance(env, dict) else {}),
        )
    return states


def _fresh_ros_stack_state(pty: Any, stack_id: str) -> dict[str, Any]:
    return _get_ros_stack_state(
        stack_id=stack_id,
        region_id=_region_for_stack(pty, stack_id),
        redaction_env=getattr(pty, "env", {}),
    )


def _get_ros_stack_state(
    *,
    stack_id: str,
    region_id: str,
    redaction_env: dict[str, str] | None,
) -> dict[str, Any]:
    try:
        from alibabacloud_ros20190910 import models as ros_models

        from iac_code.services.cloud_credentials import CloudCredentials
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        credential = CloudCredentials().get_provider("aliyun")
        effective_region = region_id or (credential.region_id if credential is not None else "")
        client = RosClientFactory.create(credential, effective_region)
        request = ros_models.GetStackRequest(stack_id=stack_id, region_id=effective_region)
        response = client.get_stack(request)
        body = response.body.to_map()
        return {
            "stack_id": str(body.get("StackId") or stack_id),
            "stack_name": str(body.get("StackName") or ""),
            "region_id": effective_region,
            "status": str(body.get("Status") or ""),
            "status_reason": str(body.get("StatusReason") or ""),
            "not_found": False,
        }
    except Exception as exc:
        message = _redact_sensitive_text(str(exc), redaction_env)
        return {
            "stack_id": stack_id,
            "region_id": region_id,
            "status": "",
            "not_found": _is_ros_stack_not_found(exc),
            "error": _compact_text(message, max_chars=1000),
        }


def _delete_ros_stack(
    *,
    stack_id: str,
    region_id: str,
    redaction_env: dict[str, str] | None,
) -> None:
    try:
        from alibabacloud_ros20190910 import models as ros_models

        from iac_code.services.cloud_credentials import CloudCredentials
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        credential = CloudCredentials().get_provider("aliyun")
        effective_region = region_id or (credential.region_id if credential is not None else "")
        client = RosClientFactory.create(credential, effective_region)
        request = ros_models.DeleteStackRequest(stack_id=stack_id, region_id=effective_region)
        client.delete_stack(request)
    except Exception as exc:
        if _is_ros_stack_not_found(exc):
            return
        message = _redact_sensitive_text(str(exc), redaction_env)
        raise RuntimeError(_compact_text(message, max_chars=1000)) from exc


def _wait_for_ros_stack_deleted(
    *,
    pty: Any,
    stack_id: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = _fresh_ros_stack_state(pty, stack_id)
        if _ros_stack_deleted(last_state):
            return last_state
        time.sleep(5)
    status = last_state.get("status") or "<unknown>"
    raise TimeoutError(f"Timed out waiting for ROS stack deletion: {stack_id} ({status})")


def _is_ros_stack_not_found(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "")
    message = str(exc)
    combined = f"{code} {message}".lower()
    not_found_tokens = (
        "stacknotfound",
        "notfound.stack",
        "entitynotexist.stack",
        "specified stack does not exist",
        "stack could not be found",
        "stack not found",
    )
    return any(token in combined for token in not_found_tokens)


def _region_for_stack(pty: Any, stack_id: str) -> str:
    for key in ("cleanup_resources", "observed_resources"):
        for resource in reversed(_cleanup_ledger_items(pty, key)):
            if _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId") == stack_id:
                region = _string_from_mapping(resource, "region_id", "regionId", "RegionId")
                if region:
                    return region
    env = getattr(pty, "env", {})
    return env.get("ALIBABA_CLOUD_REGION_ID", "") if isinstance(env, dict) else ""


def _ros_stack_deleted(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("not_found") is True:
        return True
    return state.get("status") in ROS_STACK_DELETED_STATUSES


def _ros_stack_retained(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict) or state.get("not_found") is True:
        return False
    status = state.get("status")
    return isinstance(status, str) and bool(status) and not status.startswith("DELETE_")


def _ros_stack_states_for_acceptance(pty: Any, stack_ids: Iterable[str], name: str) -> dict[str, dict[str, Any]]:
    return _capture_ros_stack_states(pty, _unique_strings(stack_ids), name)


def _apply_cleanup_acceptance_checks(
    *,
    scenario: str,
    transcript: str,
    events: list[dict[str, Any]],
    pty: Any,
    checks: dict[str, bool],
) -> None:
    first_stack_id = str(getattr(pty, "cleanup_first_stack_id", "") or "")
    second_stack_id = str(getattr(pty, "cleanup_second_stack_id", "") or "")
    observed_stack_ids = {
        stack_id
        for stack_id in (
            _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
            for resource in _cleanup_ledger_items(pty, "observed_resources")
            if _is_ros_stack_resource(resource)
        )
        if stack_id
    }
    cleanup_stack_ids = _cleanup_target_stack_ids(pty, exclude={stack_id for stack_id in [second_stack_id] if stack_id})
    run_dir = Path(getattr(pty, "run_dir", ""))
    expected_first_stack_name = _cleanup_stack_name(run_dir, "first")
    expected_second_stack_name = _cleanup_stack_name(run_dir, "second")

    _add_acceptance_check(
        checks,
        "first rollback stack observed",
        bool(first_stack_id) and first_stack_id in observed_stack_ids,
    )
    _add_acceptance_check(
        checks,
        "rollback cleanup ledger includes first stack",
        bool(first_stack_id) and first_stack_id in cleanup_stack_ids,
    )
    _add_acceptance_check(checks, "rollback cleanup target stacks observed", bool(cleanup_stack_ids))
    _add_acceptance_check(
        checks,
        "second stack created after rollback",
        bool(second_stack_id) and second_stack_id != first_stack_id and second_stack_id in observed_stack_ids,
    )
    _add_acceptance_check(
        checks,
        "first rollback stack name matches test stack",
        bool(first_stack_id) and _observed_cleanup_stack_name(pty, first_stack_id) == expected_first_stack_name,
    )
    _add_acceptance_check(
        checks,
        "second stack name matches test stack",
        bool(second_stack_id) and _observed_cleanup_stack_name(pty, second_stack_id) == expected_second_stack_name,
    )
    _add_acceptance_check(
        checks,
        "cleanup snapshot does not target second stack",
        bool(second_stack_id) and _cleanup_resource_for_stack(pty, second_stack_id) is None,
    )
    _add_acceptance_check(
        checks,
        "rollback cleanup completed",
        bool(cleanup_stack_ids)
        and all(
            _cleanup_resource_completed(_cleanup_resource_for_stack(pty, stack_id)) for stack_id in cleanup_stack_ids
        ),
    )
    _add_acceptance_check(
        checks,
        "no ROS create failure in cleanup transcript",
        not _has_any_pattern(transcript, CLEANUP_DEPLOYMENT_FAILURE_PATTERNS),
    )

    ros_states = _ros_stack_states_for_acceptance(
        pty,
        [*cleanup_stack_ids, second_stack_id],
        "acceptance-after-cleanup",
    )
    _add_acceptance_check(
        checks,
        "ROS first rollback stack deleted",
        bool(first_stack_id) and _ros_stack_deleted(ros_states.get(first_stack_id, {})),
    )
    _add_acceptance_check(
        checks,
        "ROS rollback cleanup stacks deleted",
        bool(cleanup_stack_ids)
        and all(_ros_stack_deleted(ros_states.get(stack_id, {})) for stack_id in cleanup_stack_ids),
    )
    _add_acceptance_check(
        checks,
        "ROS second stack retained",
        bool(second_stack_id) and _ros_stack_retained(ros_states.get(second_stack_id, {})),
    )

    if scenario == "rollback-step5-cleanup-recovery":
        _add_acceptance_check(
            checks,
            "cleanup process was killed",
            any(event.get("type") == "terminate" and event.get("force") is True for event in events),
        )
        _add_acceptance_check(checks, "cleanup resume used --continue", bool(_resume_spawns(events)))
        _add_acceptance_check(
            checks,
            "cleanup retriggered after restart",
            bool(_resume_spawns(events))
            and _cleanup_history_has_event(
                pty,
                first_stack_id,
                {"cleanup_started", "cleanup_progress", "cleanup_completed"},
            ),
        )


def _owned_cleanup_stack_names(run_dir: Path) -> set[str]:
    return {_cleanup_stack_name(run_dir, "first"), _cleanup_stack_name(run_dir, "second")}


def _observed_cleanup_stack_ids(pty: Any) -> list[str]:
    stack_ids = [
        str(getattr(pty, "cleanup_first_stack_id", "") or ""),
        str(getattr(pty, "cleanup_second_stack_id", "") or ""),
    ]
    stack_ids.extend(
        _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        for resource in _cleanup_ledger_items(pty, "observed_resources")
        if _is_ros_stack_resource(resource)
    )
    return _unique_strings(stack_ids)


def _observed_cleanup_stack_name(pty: Any, stack_id: str) -> str:
    for resource in reversed(_cleanup_ledger_items(pty, "observed_resources")):
        if not _is_ros_stack_resource(resource):
            continue
        resource_id = _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        if resource_id != stack_id:
            continue
        return _string_from_mapping(resource, "resource_name", "resourceName", "stack_name", "stackName")
    return ""


def _apply_stack_creating_acceptance_checks(scenario: str, pty: Any, checks: dict[str, bool]) -> None:
    if scenario not in STACK_CREATING_SCENARIOS:
        return
    stack_ids = _observed_create_stack_ids(pty)
    expected_stack_name = _scenario_stack_name(Path(getattr(pty, "run_dir", "")), scenario)
    stack_names = _observed_create_stack_names(pty)
    _add_acceptance_check(checks, "ROS stack observed in cleanup ledger", bool(stack_ids))
    _add_acceptance_check(
        checks,
        "ROS stack name is test-owned",
        bool(stack_ids) and expected_stack_name in stack_names,
    )
    ros_states = _ros_stack_states_for_acceptance(pty, stack_ids, "acceptance-before-teardown") if stack_ids else {}
    _add_acceptance_check(
        checks,
        "ROS created stack retained before teardown",
        bool(stack_ids) and any(_ros_stack_retained(ros_states.get(stack_id, {})) for stack_id in stack_ids),
    )


def _teardown_cleanup_scenario_resources(
    *,
    args: argparse.Namespace,
    scenario: str,
    pty: Any,
    checks: dict[str, bool],
    notes: list[str],
) -> None:
    if scenario not in {"rollback-step5-cleanup", "rollback-step5-cleanup-recovery"}:
        return
    if args.skip_final_teardown:
        notes.append("final teardown skipped by --skip-final-teardown")
        return

    run_dir = Path(getattr(pty, "run_dir", ""))
    owned_stack_names = _owned_cleanup_stack_names(run_dir)
    stack_ids = _observed_cleanup_stack_ids(pty)
    if not stack_ids:
        checks["teardown: no cleanup scenario stacks leaked"] = True
        return

    deletion_failures: list[str] = []
    deleted_stack_ids: list[str] = []
    for stack_id in stack_ids:
        state = _fresh_ros_stack_state(pty, stack_id)
        if _ros_stack_deleted(state):
            continue

        stack_name = str(state.get("stack_name") or "")
        if stack_name not in owned_stack_names:
            deletion_failures.append(
                f"{stack_id} has unexpected stack name {stack_name or '<unknown>'}; "
                f"expected one of {sorted(owned_stack_names)}"
            )
            continue

        try:
            _delete_ros_stack(
                stack_id=stack_id,
                region_id=str(state.get("region_id") or _region_for_stack(pty, stack_id)),
                redaction_env=getattr(pty, "env", {}),
            )
            final_state = _wait_for_ros_stack_deleted(pty=pty, stack_id=stack_id, timeout=args.final_teardown_timeout)
            if _ros_stack_deleted(final_state):
                deleted_stack_ids.append(stack_id)
            else:
                deletion_failures.append(f"{stack_id} final status is {final_state.get('status') or '<unknown>'}")
        except Exception as exc:
            deletion_failures.append(f"{stack_id}: {type(exc).__name__}: {exc}")

    for failure in deletion_failures:
        notes.append(f"final teardown failed: {_compact_text(failure, max_chars=1000)}")

    checks["teardown: cleanup scenario owned ROS stacks deleted"] = not deletion_failures
    if deleted_stack_ids:
        notes.append(f"final teardown deleted ROS stacks: {', '.join(deleted_stack_ids)}")


def _teardown_real_cloud_scenario_resources(
    *,
    args: argparse.Namespace,
    scenario: str,
    pty: Any,
    checks: dict[str, bool],
    notes: list[str],
) -> None:
    if scenario in {"rollback-step5-cleanup", "rollback-step5-cleanup-recovery"}:
        _teardown_cleanup_scenario_resources(args=args, scenario=scenario, pty=pty, checks=checks, notes=notes)
        return
    if args.skip_final_teardown:
        notes.append("final teardown skipped by --skip-final-teardown")
        return

    resources = _observed_create_stack_resources(pty)
    if not resources:
        checks["teardown: no observed ROS stacks leaked"] = True
        return

    deletion_failures: list[str] = []
    deleted_stack_ids: list[str] = []
    expected_scenario_stack_name = _scenario_stack_name(Path(getattr(pty, "run_dir", "")), scenario)
    for resource in resources:
        stack_id = _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        if not stack_id:
            continue
        expected_stack_name = _string_from_mapping(resource, "resource_name", "resourceName", "stack_name", "stackName")
        if expected_stack_name != expected_scenario_stack_name:
            deletion_failures.append(
                f"{stack_id} has unexpected test-owned stack name {expected_stack_name or '<unknown>'}; "
                f"expected {expected_scenario_stack_name}"
            )
            continue
        state = _fresh_ros_stack_state(pty, stack_id)
        if _ros_stack_deleted(state):
            continue

        actual_stack_name = str(state.get("stack_name") or "")
        if not expected_stack_name:
            deletion_failures.append(f"{stack_id} has no observed stack name in cleanup ledger")
            continue
        if actual_stack_name != expected_stack_name:
            deletion_failures.append(
                f"{stack_id} has unexpected stack name {actual_stack_name or '<unknown>'}; "
                f"expected observed name {expected_stack_name}"
            )
            continue

        try:
            _delete_ros_stack(
                stack_id=stack_id,
                region_id=str(state.get("region_id") or _region_for_stack(pty, stack_id)),
                redaction_env=getattr(pty, "env", {}),
            )
            final_state = _wait_for_ros_stack_deleted(pty=pty, stack_id=stack_id, timeout=args.final_teardown_timeout)
            if _ros_stack_deleted(final_state):
                deleted_stack_ids.append(stack_id)
            else:
                deletion_failures.append(f"{stack_id} final status is {final_state.get('status') or '<unknown>'}")
        except Exception as exc:
            deletion_failures.append(f"{stack_id}: {type(exc).__name__}: {exc}")

    for failure in deletion_failures:
        notes.append(f"final teardown failed: {_compact_text(failure, max_chars=1000)}")

    checks["teardown: observed ROS stacks deleted"] = not deletion_failures
    if deleted_stack_ids:
        notes.append(f"final teardown deleted ROS stacks: {', '.join(deleted_stack_ids)}")


def _apply_acceptance_checks(
    scenario: str,
    args: argparse.Namespace,
    pty: Any,
    checks: dict[str, bool],
) -> None:
    raw_transcript = str(getattr(pty, "transcript", ""))
    transcript = _normalize_transcript(raw_transcript)
    events = list(getattr(pty, "events", []))
    _add_acceptance_check(checks, "PTY transcript captured", bool(transcript.strip()))
    _add_acceptance_check(
        checks,
        "no terminal error in PTY transcript",
        not _has_any_pattern(transcript, TERMINAL_ERROR_PATTERNS),
    )

    if scenario == "scenario1":
        normal_answer = _suffix_after_sendline_text(raw_transcript, events, args.normal_followup_prompt)
        _add_acceptance_check(
            checks,
            "candidate selection was shown",
            _has_any_pattern(transcript, CANDIDATE_SELECTION_PATTERNS),
        )
        _add_acceptance_check(checks, "pipeline completed", _has_any_pattern(transcript, PIPELINE_COMPLETED_PATTERNS))
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
        _add_acceptance_check(
            checks,
            "normal follow-up answered created VSwitch",
            _has_vswitch_answer_evidence(normal_answer),
        )
    elif scenario == "image-initial":
        _add_acceptance_check(checks, "initial image fixture was pasted", _has_image_fixture_event(events, "initial"))
        _add_acceptance_check(
            checks,
            "candidate selection was shown",
            _has_any_pattern(transcript, CANDIDATE_SELECTION_PATTERNS),
        )
        _add_acceptance_check(checks, "pipeline completed", _has_any_pattern(transcript, PIPELINE_COMPLETED_PATTERNS))
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
    elif scenario == "image-ask-waiting-resume":
        after_answer = _suffix_after_image_fixture(raw_transcript, events, "ask-first-answer")
        _add_acceptance_check(
            checks,
            "ask user question was replayed after resume",
            _count_pattern(transcript, ASK_USER_QUESTION_HEADING_PATTERNS) >= 2,
        )
        _add_acceptance_check(checks, "resume used --continue", bool(_resume_spawns(events)))
        _add_acceptance_check(
            checks,
            "ask answer image fixture was pasted",
            _has_image_fixture_event(events, "ask-first-answer"),
        )
        _add_acceptance_check(
            checks,
            "ask image answer advanced pipeline after resume",
            _has_any_pattern(after_answer or transcript, CANDIDATE_SELECTION_PATTERNS + PIPELINE_COMPLETED_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
    elif scenario == "image-selection-waiting-resume":
        _add_acceptance_check(checks, "initial image fixture was pasted", _has_image_fixture_event(events, "initial"))
        _add_acceptance_check(
            checks,
            "candidate selection was replayed after resume",
            _count_pattern(transcript, CANDIDATE_SELECTION_PATTERNS) >= 2,
        )
        _add_acceptance_check(checks, "resume used --continue", bool(_resume_spawns(events)))
        _add_acceptance_check(
            checks,
            "pipeline completed after resume",
            _has_any_pattern(transcript, PIPELINE_COMPLETED_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
    elif scenario == "image-normal-handoff":
        normal_answer = _suffix_after_image_fixture(raw_transcript, events, "normal-followup")
        _add_acceptance_check(
            checks,
            "candidate selection was shown",
            _has_any_pattern(transcript, CANDIDATE_SELECTION_PATTERNS),
        )
        _add_acceptance_check(checks, "pipeline completed", _has_any_pattern(transcript, PIPELINE_COMPLETED_PATTERNS))
        _add_acceptance_check(
            checks,
            "normal follow-up image fixture was pasted",
            _has_image_fixture_event(events, "normal-followup"),
        )
        _add_acceptance_check(
            checks,
            "normal image follow-up answered created VSwitch",
            _has_vswitch_answer_evidence(normal_answer),
        )
    elif scenario == "image-interrupt":
        after_rollback = _suffix_after_image_fixture(raw_transcript, events, "rollback-interrupt")
        _add_acceptance_check(
            checks,
            "rollback image fixture was pasted",
            _has_image_fixture_event(events, "rollback-interrupt"),
        )
        _add_acceptance_check(
            checks,
            "rollback reached evaluate_candidates step",
            _has_any_pattern(transcript, EVALUATE_CANDIDATES_HEADING_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "rollback image produced post-interrupt pipeline progress",
            _has_any_pattern(after_rollback, POST_ROLLBACK_PROGRESS_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "post-rollback target is security group",
            _has_security_group_target_evidence(after_rollback),
        )
        _add_acceptance_check(
            checks,
            "post-rollback target is not VSwitch",
            not _has_positive_vswitch_target_evidence(after_rollback),
        )
    elif scenario == "ask-waiting":
        after_answer = _normalize_transcript(
            _last_event_suffix(raw_transcript, events, event_type="sendline", text=args.ask_answer)
        )
        _add_acceptance_check(checks, "ask user question was shown", "Ask user question" in transcript)
        _add_acceptance_check(
            checks,
            "ask answer advanced pipeline",
            _has_any_pattern(after_answer or transcript, CANDIDATE_SELECTION_PATTERNS + PIPELINE_COMPLETED_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
    elif scenario == "selection-waiting-resume":
        continue_spawns = _resume_spawns(events)
        _add_acceptance_check(
            checks,
            "candidate selection was replayed after resume",
            _count_pattern(transcript, CANDIDATE_SELECTION_PATTERNS) >= 2,
        )
        _add_acceptance_check(checks, "resume used --continue", bool(continue_spawns))
        _add_acceptance_check(
            checks,
            "pipeline completed after resume",
            _has_any_pattern(transcript, PIPELINE_COMPLETED_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
    elif scenario == "rollback-step3":
        after_rollback = _suffix_after_sendline_text(raw_transcript, events, args.rollback_prompt)
        _add_acceptance_check(
            checks,
            "rollback reached evaluate_candidates step",
            _has_any_pattern(transcript, EVALUATE_CANDIDATES_HEADING_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "rollback produced post-interrupt pipeline progress",
            _has_any_pattern(after_rollback, POST_ROLLBACK_PROGRESS_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "post-rollback target is security group",
            _has_security_group_target_evidence(after_rollback),
        )
        _add_acceptance_check(
            checks,
            "post-rollback target is not VSwitch",
            not _has_positive_vswitch_target_evidence(after_rollback),
        )
    elif scenario == "rollback-step2":
        after_rollback = _suffix_after_sendline_text(raw_transcript, events, args.rollback_prompt)
        _add_acceptance_check(
            checks,
            "rollback reached architecture_planning step",
            _has_any_pattern(transcript, ARCHITECTURE_PLANNING_HEADING_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "rollback produced post-interrupt pipeline progress",
            _has_any_pattern(after_rollback, POST_ROLLBACK_PROGRESS_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "post-rollback target is security group",
            _has_security_group_target_evidence(after_rollback),
        )
        _add_acceptance_check(
            checks,
            "post-rollback target is not VSwitch",
            not _has_positive_vswitch_target_evidence(after_rollback),
        )
    elif scenario == "rollback-step4-selection":
        after_rollback = _suffix_after_sendline_text(raw_transcript, events, args.rollback_prompt)
        _add_acceptance_check(
            checks,
            "rollback reached candidate selection step",
            _has_any_pattern(transcript, CANDIDATE_SELECTION_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "rollback produced post-interrupt pipeline progress",
            _has_any_pattern(after_rollback, POST_ROLLBACK_PROGRESS_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "post-rollback target is security group",
            _has_security_group_target_evidence(after_rollback),
        )
        _add_acceptance_check(
            checks,
            "post-rollback target is not VSwitch",
            not _has_positive_vswitch_target_evidence(after_rollback),
        )
    elif scenario == "evaluate-resume":
        after_continue = _normalize_transcript(
            _last_event_suffix(
                raw_transcript,
                events,
                event_type="sendline",
                text=args.evaluate_resume_continue_prompt,
            )
        )
        _add_acceptance_check(
            checks,
            "evaluate_candidates was shown before resume",
            _has_any_pattern(transcript, EVALUATE_CANDIDATES_HEADING_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "evaluate_candidates was replayed after resume",
            _count_pattern(transcript, EVALUATE_CANDIDATES_HEADING_PATTERNS) >= 2,
        )
        _add_acceptance_check(checks, "resume used --continue", bool(_resume_spawns(events)))
        _add_acceptance_check(
            checks,
            "resume continue input was sent",
            _has_sendline_event(events, args.evaluate_resume_continue_prompt),
        )
        _add_acceptance_check(
            checks,
            "pipeline advanced after resume continue",
            _has_any_pattern(after_continue or transcript, CANDIDATE_SELECTION_PATTERNS + PIPELINE_COMPLETED_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
    elif scenario == "ask-waiting-resume":
        after_answer = _normalize_transcript(
            _last_event_suffix(raw_transcript, events, event_type="sendline", text=args.ask_answer)
        )
        _add_acceptance_check(
            checks,
            "ask user question was replayed after resume",
            _count_pattern(transcript, ASK_USER_QUESTION_HEADING_PATTERNS) >= 2,
        )
        _add_acceptance_check(checks, "resume used --continue", bool(_resume_spawns(events)))
        _add_acceptance_check(
            checks,
            "ask answer advanced pipeline after resume",
            _has_any_pattern(after_answer or transcript, CANDIDATE_SELECTION_PATTERNS + PIPELINE_COMPLETED_PATTERNS),
        )
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
    elif scenario == "selection-invalid-then-valid":
        _add_acceptance_check(
            checks,
            "invalid selection input was sent",
            _event_index(events, "select-invalid-candidate") is not None,
        )
        _add_acceptance_check(
            checks,
            "valid selection input was sent after invalid input",
            _event_before(events, "select-invalid-candidate", "select-default-candidate"),
        )
        _add_acceptance_check(checks, "pipeline completed", _has_any_pattern(transcript, PIPELINE_COMPLETED_PATTERNS))
        _add_acceptance_check(
            checks,
            "VSwitch evidence found in PTY transcript",
            _has_vswitch_business_evidence(transcript),
        )
    elif scenario in {"rollback-step5-cleanup", "rollback-step5-cleanup-recovery"}:
        _add_acceptance_check(
            checks,
            "deploying step was reached",
            _has_any_pattern(transcript, DEPLOYING_STEP_PATTERNS + FIRST_STACK_CREATED_PATTERNS),
        )
        _add_acceptance_check(checks, "cleanup started", _has_any_pattern(transcript, CLEANUP_STARTED_PATTERNS))
        _apply_cleanup_acceptance_checks(
            scenario=scenario,
            transcript=transcript,
            events=events,
            pty=pty,
            checks=checks,
        )
    _apply_stack_creating_acceptance_checks(scenario, pty, checks)


def _select_default_candidate(pty: ReplPty, args: argparse.Namespace) -> None:
    if args.selection_prompt:
        pty.send(f"{args.selection_prompt}\r", label="select-default-candidate")
    else:
        pty.send("\r", label="select-default-candidate")


def _expect_initial_prompt(pty: ReplPty, args: argparse.Namespace) -> None:
    pty.expect_any(REPL_PROMPT_PATTERNS, description="initial prompt", timeout=args.timeout)
    pty.expect_any(REPL_INPUT_READY_PATTERNS, description="prompt input ready", timeout=args.timeout)


def _expect_candidate_selection(pty: ReplPty, args: argparse.Namespace, *, description: str) -> None:
    pty.expect_any(CANDIDATE_SELECTION_PATTERNS, description=description, timeout=args.stream_timeout)
    pty.expect_optional(
        CANDIDATE_SELECTION_READY_PATTERNS,
        description="candidate selection controls ready",
        timeout=args.candidate_selection_ready_timeout,
    )


def _expect_raw_input_ready(pty: ReplPty, args: argparse.Namespace, *, description: str) -> None:
    pty.expect_any(REPL_INPUT_READY_PATTERNS, description=description, timeout=args.timeout)


def _expect_parallel_interrupt_ready(pty: ReplPty, args: argparse.Namespace) -> None:
    _expect_raw_input_ready(pty, args, description="parallel interrupt input ready")


def _wait_for_cleanup_completed_and_ready(pty: ReplPty, args: argparse.Namespace, first_stack_id: str) -> None:
    _wait_for_cleanup_resource_status(pty, first_stack_id, {"completed"}, timeout=args.stream_timeout)
    pty.expect_optional(
        CLEANUP_COMPLETED_PATTERNS,
        description="cleanup completed",
        timeout=min(args.timeout, 5.0),
    )
    _expect_raw_input_ready(pty, args, description="post-cleanup prompt input ready")


def _finish_vswitch_pipeline_after_possible_selection(
    pty: ReplPty,
    args: argparse.Namespace,
    checks: dict[str, bool],
    matched_pattern: str,
    *,
    selection_check: str,
    completion_check: str,
    completion_description: str,
) -> None:
    if matched_pattern in CANDIDATE_SELECTION_PATTERNS:
        pty.expect_optional(
            CANDIDATE_SELECTION_READY_PATTERNS,
            description="candidate selection controls ready after ask",
            timeout=args.candidate_selection_ready_timeout,
        )
        _select_default_candidate(pty, args)
        checks[selection_check] = True
        pty.expect_any(PIPELINE_COMPLETED_PATTERNS, description=completion_description, timeout=args.stream_timeout)
    checks[completion_check] = True


def _expect_post_rollback_security_group_target(
    pty: ReplPty,
    args: argparse.Namespace,
    checks: dict[str, bool],
) -> None:
    pty.expect_any(
        SECURITY_GROUP_MENTION_PATTERNS,
        description="post-rollback security group target visible",
        timeout=min(args.stream_timeout, 300.0),
    )
    checks["post-rollback security group target visible"] = True


def run_scenario1(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(_stack_creating_prompt(args.initial_prompt, pty.run_dir, scenario))
        pty.expect_any(PIPELINE_STARTED_PATTERNS, description="pipeline started", timeout=args.stream_timeout)
        checks["pipeline started"] = True
        _expect_candidate_selection(pty, args, description="candidate selection visible")
        checks["candidate selection became visible"] = True
        _select_default_candidate(pty, args)
        checks["candidate selection input sent"] = True
        pty.expect_any(
            PIPELINE_FULLY_COMPLETED_PATTERNS,
            description="pipeline fully completed",
            timeout=args.stream_timeout,
        )
        checks["pipeline completed"] = True
        _expect_raw_input_ready(pty, args, description="normal prompt input ready")
        checks["normal prompt input ready"] = True
        pty.sendline(args.normal_followup_prompt)
        pty.expect_any(
            VSWITCH_MENTION_PATTERNS,
            description="normal follow-up answered created VSwitch",
            timeout=min(args.stream_timeout, 120.0),
        )
        checks["normal follow-up answered created VSwitch"] = True
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_ask_waiting(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(args.ask_prompt)
        pty.expect_any(ASK_PATTERNS, description="ask question visible", timeout=args.stream_timeout)
        checks["ask question became visible"] = True
        pty.sendline(_stack_creating_prompt(args.ask_answer, pty.run_dir, scenario))
        checks["ask answer sent"] = True
        matched = pty.expect_any(
            CANDIDATE_SELECTION_PATTERNS + PIPELINE_COMPLETED_PATTERNS,
            description="pipeline continued after ask",
            timeout=args.stream_timeout,
        )
        checks["pipeline continued beyond ask"] = True
        _finish_vswitch_pipeline_after_possible_selection(
            pty,
            args,
            checks,
            matched,
            selection_check="candidate selection input sent after ask",
            completion_check="pipeline completed after ask",
            completion_description="pipeline completed after ask",
        )
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_image_initial(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        _submit_image_fixture(pty, "initial", caption=_stack_name_constraint(pty.run_dir, scenario))
        checks["initial image fixture pasted"] = True
        pty.expect_any(PIPELINE_STARTED_PATTERNS, description="pipeline started", timeout=args.stream_timeout)
        checks["pipeline started"] = True
        _expect_candidate_selection(pty, args, description="candidate selection visible")
        checks["candidate selection became visible"] = True
        _select_default_candidate(pty, args)
        checks["candidate selection input sent"] = True
        pty.expect_any(
            PIPELINE_COMPLETED_PATTERNS,
            description="pipeline completed after image initial",
            timeout=args.stream_timeout,
        )
        checks["pipeline completed after image initial"] = True
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_image_ask_waiting_resume(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(args.ask_prompt)
        pty.expect_any(ASK_PATTERNS, description="ask question visible before kill", timeout=args.stream_timeout)
        checks["ask question became visible before kill"] = True
        pty.terminate(force=True)
        checks["first process killed"] = True
        pty.spawn(extra_args=["--continue"])
        pty.expect_any(ASK_PATTERNS, description="ask question replayed", timeout=args.stream_timeout)
        checks["ask question replayed"] = True
        _expect_raw_input_ready(pty, args, description="ask image answer input ready after resume")
        checks["ask image answer input ready after resume"] = True
        _submit_image_fixture(pty, "ask-first-answer", caption=_stack_name_constraint(pty.run_dir, scenario))
        checks["ask first answer image fixture pasted after resume"] = True
        if pty.expect_optional(
            ASK_PATTERNS,
            description="second ask question after image answer",
            timeout=min(args.timeout, 30.0),
        ):
            _expect_raw_input_ready(pty, args, description="second ask image answer input ready")
            _submit_image_fixture(pty, "ask-second-answer", caption=_stack_name_constraint(pty.run_dir, scenario))
            checks["ask second answer image fixture pasted"] = True
        matched = pty.expect_any(
            CANDIDATE_SELECTION_PATTERNS + PIPELINE_COMPLETED_PATTERNS,
            description="pipeline continued after ask image resume",
            timeout=args.stream_timeout,
        )
        checks["pipeline continued beyond ask image after resume"] = True
        _finish_vswitch_pipeline_after_possible_selection(
            pty,
            args,
            checks,
            matched,
            selection_check="candidate selection input sent after ask image resume",
            completion_check="pipeline completed after ask image resume",
            completion_description="pipeline completed after ask image resume",
        )
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_image_selection_waiting_resume(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        _submit_image_fixture(pty, "initial", caption=_stack_name_constraint(pty.run_dir, scenario))
        checks["initial image fixture pasted"] = True
        _expect_candidate_selection(pty, args, description="candidate selection visible before image resume kill")
        checks["candidate selection became visible before kill"] = True
        pty.terminate(force=True)
        checks["first process killed"] = True
        pty.spawn(extra_args=["--continue"])
        _expect_candidate_selection(pty, args, description="candidate selection replayed after image resume")
        checks["candidate selection replayed after resume"] = True
        _select_default_candidate(pty, args)
        checks["candidate selection input sent after resume"] = True
        pty.expect_any(
            PIPELINE_COMPLETED_PATTERNS,
            description="pipeline completed after image selection resume",
            timeout=args.stream_timeout,
        )
        checks["pipeline completed after image selection resume"] = True
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_image_normal_handoff(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(_stack_creating_prompt(args.initial_prompt, pty.run_dir, scenario))
        pty.expect_any(PIPELINE_STARTED_PATTERNS, description="pipeline started", timeout=args.stream_timeout)
        checks["pipeline started"] = True
        _expect_candidate_selection(pty, args, description="candidate selection visible")
        checks["candidate selection became visible"] = True
        _select_default_candidate(pty, args)
        checks["candidate selection input sent"] = True
        pty.expect_any(
            PIPELINE_FULLY_COMPLETED_PATTERNS,
            description="pipeline fully completed",
            timeout=args.stream_timeout,
        )
        checks["pipeline completed"] = True
        _expect_raw_input_ready(pty, args, description="normal prompt input ready")
        checks["normal prompt input ready"] = True
        _submit_image_fixture(pty, "normal-followup")
        checks["normal follow-up image fixture pasted"] = True
        pty.expect_any(
            VSWITCH_MENTION_PATTERNS,
            description="normal image follow-up answered created VSwitch",
            timeout=min(args.stream_timeout, 120.0),
        )
        checks["normal image follow-up answered created VSwitch"] = True
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_image_interrupt(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(args.initial_prompt)
        pty.expect_any(
            CANDIDATE_EVALUATION_PATTERNS,
            description="candidate evaluation visible",
            timeout=args.stream_timeout,
        )
        checks["candidate evaluation reached"] = True
        _expect_parallel_interrupt_ready(pty, args)
        checks["parallel interrupt input ready"] = True
        pty.send("\x1b", label="send-esc")
        checks["esc sent"] = True
        pty.expect_any(
            REPL_INPUT_READY_PATTERNS, description="parallel interrupt text input ready", timeout=args.timeout
        )
        checks["parallel interrupt text input ready"] = True
        _submit_image_fixture(pty, "rollback-interrupt")
        checks["rollback interrupt image fixture pasted"] = True
        pty.expect_any(
            POST_ROLLBACK_PROGRESS_PATTERNS,
            description="post-rollback pipeline progress visible",
            timeout=args.stream_timeout,
        )
        checks["post-rollback pipeline progress visible"] = True
        _expect_post_rollback_security_group_target(pty, args, checks)
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_selection_waiting_resume(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(_stack_creating_prompt(args.initial_prompt, pty.run_dir, scenario))
        _expect_candidate_selection(pty, args, description="candidate selection visible")
        checks["candidate selection became visible before kill"] = True
        pty.terminate(force=True)
        checks["first process killed"] = True
        pty.spawn(extra_args=["--continue"])
        _expect_candidate_selection(pty, args, description="candidate selection replayed")
        checks["candidate selection replayed"] = True
        _select_default_candidate(pty, args)
        checks["candidate selection input sent after resume"] = True
        pty.expect_any(
            PIPELINE_COMPLETED_PATTERNS, description="pipeline completed after resume", timeout=args.stream_timeout
        )
        checks["pipeline completed after resume"] = True
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_ask_waiting_resume(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(args.ask_prompt)
        pty.expect_any(ASK_PATTERNS, description="ask question visible before kill", timeout=args.stream_timeout)
        checks["ask question became visible before kill"] = True
        pty.terminate(force=True)
        checks["first process killed"] = True
        pty.spawn(extra_args=["--continue"])
        pty.expect_any(ASK_PATTERNS, description="ask question replayed", timeout=args.stream_timeout)
        checks["ask question replayed"] = True
        _expect_raw_input_ready(pty, args, description="ask answer input ready after resume")
        checks["ask answer input ready after resume"] = True
        pty.sendline(_stack_creating_prompt(args.ask_answer, pty.run_dir, scenario))
        checks["ask answer sent after resume"] = True
        matched = pty.expect_any(
            CANDIDATE_SELECTION_PATTERNS + PIPELINE_COMPLETED_PATTERNS,
            description="pipeline continued after ask resume",
            timeout=args.stream_timeout,
        )
        checks["pipeline continued beyond ask after resume"] = True
        _finish_vswitch_pipeline_after_possible_selection(
            pty,
            args,
            checks,
            matched,
            selection_check="candidate selection input sent after ask resume",
            completion_check="pipeline completed after ask resume",
            completion_description="pipeline completed after ask resume",
        )
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_evaluate_resume(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(_stack_creating_prompt(args.initial_prompt, pty.run_dir, scenario))
        pty.expect_any(
            CANDIDATE_EVALUATION_PATTERNS, description="candidate evaluation visible", timeout=args.stream_timeout
        )
        checks["candidate evaluation reached before kill"] = True
        _expect_parallel_interrupt_ready(pty, args)
        checks["parallel interrupt input ready before kill"] = True
        pty.terminate(force=True)
        checks["first process killed"] = True
        pty.spawn(extra_args=["--continue"])
        pty.expect_any(
            EVALUATE_CANDIDATES_HEADING_PATTERNS,
            description="candidate evaluation replayed after resume",
            timeout=args.stream_timeout,
        )
        checks["candidate evaluation replayed after resume"] = True
        _expect_raw_input_ready(pty, args, description="evaluate resume prompt input ready")
        checks["evaluate resume prompt input ready"] = True
        pty.sendline(args.evaluate_resume_continue_prompt)
        checks["resume continue input sent"] = True
        _expect_candidate_selection(pty, args, description="candidate selection visible after resume continue")
        checks["candidate selection became visible after resume continue"] = True
        _select_default_candidate(pty, args)
        checks["candidate selection input sent after resume"] = True
        pty.expect_any(
            PIPELINE_COMPLETED_PATTERNS,
            description="pipeline completed after evaluate resume",
            timeout=args.stream_timeout,
        )
        checks["pipeline completed after evaluate resume"] = True
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_selection_invalid_then_valid(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(_stack_creating_prompt(args.initial_prompt, pty.run_dir, scenario))
        _expect_candidate_selection(pty, args, description="candidate selection visible")
        checks["candidate selection became visible"] = True
        pty.send(args.invalid_selection_prompt, label="select-invalid-candidate")
        checks["invalid selection input sent"] = True
        _select_default_candidate(pty, args)
        checks["valid selection input sent after invalid input"] = True
        pty.expect_any(PIPELINE_COMPLETED_PATTERNS, description="pipeline completed", timeout=args.stream_timeout)
        checks["pipeline completed"] = True
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_rollback_step2(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(args.initial_prompt)
        pty.expect_any(
            ARCHITECTURE_PLANNING_PATTERNS,
            description="architecture planning visible",
            timeout=args.stream_timeout,
        )
        checks["architecture planning reached"] = True
        pty.send("\x1b", label="send-esc")
        checks["esc sent"] = True
        pty.expect_any(INTERRUPT_INPUT_PATTERNS, description="interrupt input visible", timeout=args.timeout)
        checks["interrupt input visible"] = True
        _expect_raw_input_ready(pty, args, description="interrupt prompt input ready")
        checks["interrupt prompt input ready"] = True
        pty.sendline(args.rollback_prompt)
        checks["rollback prompt sent"] = True
        pty.expect_any(
            POST_ROLLBACK_PROGRESS_PATTERNS,
            description="post-rollback pipeline progress visible",
            timeout=args.stream_timeout,
        )
        checks["post-rollback pipeline progress visible"] = True
        _expect_post_rollback_security_group_target(pty, args, checks)
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_rollback_step3(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(args.initial_prompt)
        pty.expect_any(
            CANDIDATE_EVALUATION_PATTERNS,
            description="candidate evaluation visible",
            timeout=args.stream_timeout,
        )
        checks["candidate evaluation reached"] = True
        _expect_parallel_interrupt_ready(pty, args)
        checks["parallel interrupt input ready"] = True
        pty.send("\x1b", label="send-esc")
        checks["esc sent"] = True
        pty.expect_any(
            REPL_INPUT_READY_PATTERNS, description="parallel interrupt text input ready", timeout=args.timeout
        )
        checks["parallel interrupt text input ready"] = True
        pty.sendline(args.rollback_prompt)
        checks["rollback prompt sent"] = True
        pty.expect_any(
            POST_ROLLBACK_PROGRESS_PATTERNS,
            description="post-rollback pipeline progress visible",
            timeout=args.stream_timeout,
        )
        checks["post-rollback pipeline progress visible"] = True
        _expect_post_rollback_security_group_target(pty, args, checks)
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_rollback_step4_selection(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        pty.sendline(args.initial_prompt)
        _expect_candidate_selection(pty, args, description="candidate selection visible")
        checks["candidate selection reached"] = True
        _expect_raw_input_ready(pty, args, description="candidate selection input ready")
        checks["candidate selection input ready"] = True
        pty.send("\x1b", label="send-esc")
        checks["esc sent"] = True
        _expect_raw_input_ready(pty, args, description="candidate selection interrupt text input ready")
        checks["candidate selection interrupt text input ready"] = True
        pty.sendline(args.rollback_prompt)
        checks["rollback prompt sent"] = True
        pty.expect_any(
            POST_ROLLBACK_PROGRESS_PATTERNS,
            description="post-rollback pipeline progress visible",
            timeout=args.stream_timeout,
        )
        checks["post-rollback pipeline progress visible"] = True
        _expect_post_rollback_security_group_target(pty, args, checks)
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


def run_rollback_step5_cleanup(args: argparse.Namespace, scenario: str) -> int:
    return _run_rollback_step5_cleanup(args, scenario, kill_during_cleanup=False)


def run_rollback_step5_cleanup_recovery(args: argparse.Namespace, scenario: str) -> int:
    return _run_rollback_step5_cleanup(args, scenario, kill_during_cleanup=True)


def _run_rollback_step5_cleanup(
    args: argparse.Namespace,
    scenario: str,
    *,
    kill_during_cleanup: bool,
) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        _expect_initial_prompt(pty, args)
        _ensure_cleanup_network_target(args, pty.run_dir)
        checks["cleanup network target prepared"] = True
        pty.sendline(_cleanup_pipeline_prompt(args, pty.run_dir))
        _expect_candidate_selection(pty, args, description="initial candidate selection visible")
        checks["initial reached step4 selection"] = True

        _select_default_candidate(pty, args)
        checks["initial candidate selected"] = True
        pty.expect_any(
            CREATE_STACK_STARTED_PATTERNS,
            description="first stack create started",
            timeout=args.stream_timeout,
        )
        first_stack_id = _wait_for_latest_observed_stack_id(pty, exclude=set(), timeout=args.stream_timeout)
        pty.cleanup_first_stack_id = first_stack_id
        checks["first rollback stack observed before rollback"] = bool(first_stack_id)

        pty.send("\x1b", label="send-esc")
        checks["esc sent during deploying"] = True
        _expect_raw_input_ready(pty, args, description="deploying interrupt input ready")
        checks["deploying interrupt input ready"] = True
        pty.sendline(_cleanup_rollback_prompt(args, pty.run_dir))
        checks["rollback prompt sent"] = True
        _expect_candidate_selection(pty, args, description="post-rollback candidate selection visible")
        checks["post-rollback candidate selection visible"] = True

        cleanup_stack_ids = _wait_for_cleanup_target_stack_ids(pty, exclude=set(), timeout=args.timeout)
        checks["rollback cleanup ledger includes first stack"] = first_stack_id in cleanup_stack_ids
        checks["rollback cleanup target stacks observed"] = bool(cleanup_stack_ids)

        _select_default_candidate(pty, args)
        checks["post-rollback candidate selected"] = True
        pty.expect_any(
            PIPELINE_FULLY_COMPLETED_PATTERNS,
            description="pipeline completed after second deployment",
            timeout=args.stream_timeout,
        )
        checks["pipeline completed after second deployment"] = True

        second_stack_id = _latest_observed_stack_id(pty, exclude=set(cleanup_stack_ids) | {first_stack_id})
        pty.cleanup_second_stack_id = second_stack_id or ""
        checks["second stack created after rollback"] = bool(second_stack_id)
        checks["second stack differs from first rollback stack"] = (
            bool(second_stack_id) and second_stack_id != first_stack_id
        )

        cleanup_stack_ids = _cleanup_target_stack_ids(
            pty,
            exclude={stack_id for stack_id in [second_stack_id] if stack_id},
        )
        checks["rollback cleanup ledger includes first stack"] = first_stack_id in cleanup_stack_ids
        checks["rollback cleanup target stacks observed"] = bool(cleanup_stack_ids)
        checks["cleanup snapshot does not target second stack"] = (
            bool(second_stack_id) and _cleanup_resource_for_stack(pty, second_stack_id) is None
        )

        pty.sendline(args.normal_followup_prompt)
        if kill_during_cleanup:
            pty.expect_any(
                CLEANUP_STARTED_PATTERNS,
                description="cleanup started before kill",
                timeout=args.stream_timeout,
            )
            checks["cleanup started before kill"] = True
            pty.terminate(force=True)
            checks["cleanup process killed"] = True
            pty.spawn(extra_args=["--continue"])
            pty.expect_any(
                CLEANUP_RESUME_SUMMARY_PATTERNS,
                description="cleanup resume summary",
                timeout=args.stream_timeout,
            )
            if _cleanup_resource_completed(_cleanup_resource_for_stack(pty, first_stack_id)):
                checks["cleanup already completed after restart"] = True
            else:
                _expect_raw_input_ready(pty, args, description="cleanup resume prompt input ready")
                pty.sendline(args.cleanup_continue_prompt)
                checks["cleanup continue prompt sent after restart"] = True
        else:
            pty.expect_any(CLEANUP_STARTED_PATTERNS, description="cleanup started", timeout=args.stream_timeout)
            checks["cleanup started"] = True

        _wait_for_cleanup_completed_and_ready(pty, args, first_stack_id)
        checks["first rollback stack cleanup completed in ledger"] = _cleanup_resource_completed(
            _cleanup_resource_for_stack(pty, first_stack_id)
        )
        checks["rollback cleanup stacks completed in ledger"] = bool(cleanup_stack_ids) and all(
            _cleanup_resource_completed(_cleanup_resource_for_stack(pty, stack_id)) for stack_id in cleanup_stack_ids
        )
        pty.sendline("/exit")

    return _run_with_pty(args, scenario, callback)


_SCENARIOS: dict[str, Callable[[argparse.Namespace, str], int]] = {
    "scenario1": run_scenario1,
    "ask-waiting": run_ask_waiting,
    "ask-waiting-resume": run_ask_waiting_resume,
    "image-initial": run_image_initial,
    "image-ask-waiting-resume": run_image_ask_waiting_resume,
    "image-selection-waiting-resume": run_image_selection_waiting_resume,
    "image-normal-handoff": run_image_normal_handoff,
    "image-interrupt": run_image_interrupt,
    "evaluate-resume": run_evaluate_resume,
    "selection-invalid-then-valid": run_selection_invalid_then_valid,
    "selection-waiting-resume": run_selection_waiting_resume,
    "rollback-step2": run_rollback_step2,
    "rollback-step3": run_rollback_step3,
    "rollback-step4-selection": run_rollback_step4_selection,
    "rollback-step5-cleanup": run_rollback_step5_cleanup,
    "rollback-step5-cleanup-recovery": run_rollback_step5_cleanup_recovery,
}
_REAL_CLOUD_SCENARIOS = frozenset(_SCENARIOS)


if __name__ == "__main__":
    raise SystemExit(main())
