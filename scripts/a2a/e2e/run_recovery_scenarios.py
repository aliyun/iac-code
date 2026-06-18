#!/usr/bin/env python3
"""Run A2A pipeline session-recovery E2E scenarios.

The scenarios in this file intentionally drive the public A2A JSON-RPC HTTP
endpoint. They do not call pipeline internals directly. Each run starts a local
A2A server, records raw requests/SSE/pipeline snapshots, kills the server with
SIGKILL at a scenario-specific point, restarts it with the same persistence
directory, then validates recovery behavior.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

E2E_SCRIPTS_DIR = Path(__file__).resolve().parent
A2A_SCRIPTS_DIR = E2E_SCRIPTS_DIR.parent
for scripts_dir in (E2E_SCRIPTS_DIR, A2A_SCRIPTS_DIR):
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

from common import (  # noqa: E402
    DEFAULT_INITIAL_PROMPT,
    DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT,
    DEFAULT_RECOVERY_PROMPT,
    DEFAULT_SELECTION_PROMPT,
    RUN_LOG_ROOT_NAME,
    ManagedServer,
    StreamSummary,
    _add_completed_snapshot_checks,
    _append_jsonl,
    _compact_text,
    _free_port,
    _new_run_dir,
    _normal_turn_finished,
    _redact_json_value,
    _redact_sensitive_text,
    _server_env,
    _split_python_command,
    _status_message_texts,
    _utc_now,
    _write_json,
    _write_server_config,
    fetch_pipeline_state,
    run_llm_preflight,
    stream_message,
    wait_for_server,
)
from debugger import (  # noqa: E402
    A2A_VERSION_HEADERS,
    _a2a_task_identity,
    _extract_pipeline_envelope,
    _parse_sse_data_line,
    build_message_stream_payload,
    build_task_cancel_payload,
    build_task_get_payload,
)

ASK_TRIGGER_PROMPT = "我有个产品要上线"
ASK_FIRST_ANSWER = "我要创建云网络资源；本次只选择已有 VPC 创建一个 VSwitch，不部署 ECS、EIP、SLB 或 Nginx。"
ASK_SECOND_ANSWER = "选择一个已有 VPC，创建一个 VSwitch；地域、可用区和网段你按低成本默认值推荐。"
INTERVENING_ASK_ANSWER = "使用默认配置（可用区和网段自动规划），继续。"
ROLLBACK_PROMPT = "回退到 intent_parsing，选择一个已有vpc，创建一个安全组"
CONTINUE_PROMPT = "继续"

VSWITCH_MARKERS = ("ALIYUN::ECS::VSwitch", "VSwitchId", "vsw-", "VSwitch", "交换机")
SECURITY_GROUP_MARKERS = ("ALIYUN::ECS::SecurityGroup", "SecurityGroupId", "sg-", "安全组")
TERMINAL_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_INPUT_REQUIRED"}


@dataclass
class ScenarioRunResult:
    scenario: str
    run_dir: str
    server_url: str
    context_id: str
    pipeline_task_id: str
    passed: bool
    checks: dict[str, bool]
    abort_reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class EventMatch:
    description: str
    event: Any
    summary: StreamSummary


class BackgroundStream:
    def __init__(
        self,
        *,
        server_url: str,
        cwd: str,
        prompt: str,
        name: str,
        run_dir: Path,
        timeout: float,
        context_id: str = "",
        task_id: str = "",
        redaction_env: dict[str, str] | None = None,
    ) -> None:
        self.server_url = server_url
        self.cwd = cwd
        self.prompt = prompt
        self.name = name
        self.run_dir = run_dir
        self.timeout = timeout
        self.context_id = context_id
        self.task_id = task_id
        self.redaction_env = redaction_env
        self.summary = StreamSummary(name=name, prompt=prompt, request_task_id=task_id)
        self.events: list[Any] = []
        self.exception: BaseException | None = None
        self._condition = threading.Condition()
        self._done = False
        self._thread = threading.Thread(target=self._run, name=f"a2a-e2e-{name}", daemon=True)

    @property
    def done(self) -> bool:
        with self._condition:
            return self._done

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> StreamSummary:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError(f"{self.name} did not finish within {timeout}s")
        if self.exception is not None:
            raise RuntimeError(f"{self.name} failed: {self.exception}") from self.exception
        return self.summary

    def wait_for(
        self,
        predicate: Callable[[Any, StreamSummary], bool],
        *,
        description: str,
        timeout: float,
    ) -> EventMatch:
        deadline = time.monotonic() + timeout
        seen = 0
        with self._condition:
            while True:
                while seen < len(self.events):
                    event = self.events[seen]
                    seen += 1
                    if predicate(event, self.summary):
                        return EventMatch(description=description, event=event, summary=self.summary)
                if self._done:
                    if self.exception is not None:
                        message = f"{self.name} ended before {description}: {self.exception}"
                        raise RuntimeError(message) from self.exception
                    raise RuntimeError(f"{self.name} ended before {description}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for {description} in {self.name}")
                self._condition.wait(min(remaining, 1.0))

    def _run(self) -> None:
        payload = build_message_stream_payload(
            cwd=self.cwd,
            prompt=self.prompt,
            context_id=self.context_id,
            task_id=self.task_id,
            request_id=str(uuid.uuid4()),
            message_id=str(uuid.uuid4()),
        )
        _append_jsonl(
            self.run_dir / "requests.jsonl",
            {"name": self.name, "payload": payload, "at": _utc_now()},
            self.redaction_env,
        )
        request = Request(
            self.server_url.rstrip("/") + "/",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                for line in response:
                    parsed = _parse_sse_data_line(line)
                    if parsed is None:
                        continue
                    _append_jsonl(self.run_dir / f"{self.name}.events.jsonl", parsed, self.redaction_env)
                    with self._condition:
                        self.events.append(parsed)
                        _apply_event(self.summary, parsed)
                        self._condition.notify_all()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            redacted_body = _redact_sensitive_text(body, self.redaction_env)
            self.exception = RuntimeError(f"HTTP {exc.code}: {redacted_body[:500]}")
            _append_jsonl(
                self.run_dir / f"{self.name}.events.jsonl",
                {"error": f"HTTP {exc.code}", "body": redacted_body},
                self.redaction_env,
            )
        except (TimeoutError, URLError, OSError) as exc:
            self.exception = exc
            _append_jsonl(self.run_dir / f"{self.name}.events.jsonl", {"error": str(exc)}, self.redaction_env)
        finally:
            with self._condition:
                self._done = True
                self._condition.notify_all()


class ScenarioHarness:
    def __init__(self, args: argparse.Namespace, *, scenario: str) -> None:
        self.args = args
        self.scenario = scenario
        self.server_cwd = str(Path(args.server_cwd).expanduser().resolve())
        self.run_dir = _scenario_run_dir(args, scenario)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = Path(args.cwd).expanduser().resolve() if args.cwd else self.run_dir / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = str(self.workspace_dir)
        self.port = args.port if args.port else _free_port(args.host)
        self.server_url = f"http://{args.host}:{self.port}"
        self.config_path = _write_server_config(
            self.run_dir,
            host=args.host,
            port=self.port,
            auto_approve_permissions=not args.no_auto_approve_permissions,
        )
        self.server_env = _server_env(
            os.environ.copy(),
            provider=args.provider,
            model=args.model,
            api_base=args.api_base,
        )
        if args.deterministic:
            self.server_env["IAC_CODE_A2A_DETERMINISTIC_RECOVERY"] = "1"
            self.server_env["IAC_CODE_TEST_FAULT_INJECTION"] = "1"
            self.server_env["IAC_CODE_TEST_FAULT_INJECTION_MODE"] = "exit"
            if args.fault_at:
                self.server_env["IAC_CODE_TEST_CRASH_AT"] = args.fault_at
        self.server: ManagedServer | None = None
        self.server_index = 0
        self.context_id = ""
        self.pipeline_task_id = ""
        self.checks: dict[str, bool] = {}
        self.notes: list[str] = []
        self.summaries: dict[str, Any] = {}
        self.snapshots: dict[str, Any] = {}

    def preflight(self) -> None:
        if self.args.deterministic:
            self.checks["LLM preflight skipped for deterministic mode"] = True
            self.notes.append(
                "LLM preflight skipped because deterministic fault injection is enabled; "
                "post-restart execution still uses the configured pipeline/provider."
            )
            return
        if self.args.skip_preflight:
            self.notes.append("LLM preflight skipped")
            return
        preflight = run_llm_preflight(
            python_cmd=_split_python_command(self.args.python),
            cwd=self.server_cwd,
            env=self.server_env,
            timeout=self.args.preflight_timeout,
            run_dir=self.run_dir,
        )
        self.checks["LLM preflight succeeded"] = preflight["ok"] is True
        if not self.checks["LLM preflight succeeded"]:
            raise RuntimeError(f"LLM preflight failed: {preflight['summary']}")

    def start_server(self) -> None:
        self.server_index += 1
        self.server = ManagedServer(
            python_cmd=_split_python_command(self.args.python),
            config_path=self.config_path,
            process_cwd=self.server_cwd,
            allowed_cwd=self.cwd,
            env=self.server_env,
            log_prefix=self.run_dir / f"server-{self.server_index}",
        )
        self.server.start()
        wait_for_server(self.server_url, timeout=self.args.server_timeout)

    def kill9_and_restart(self) -> None:
        if self.server is None:
            raise RuntimeError("server is not running")
        self.server.kill9()
        self.start_server()

    def terminate(self) -> None:
        if self.server is not None and not self.args.leave_server_running:
            self.server.terminate()

    def stream(
        self,
        *,
        prompt: str,
        name: str,
        context_id: str | None = None,
        task_id: str | None = None,
    ) -> StreamSummary:
        summary = stream_message(
            server_url=self.server_url,
            cwd=self.cwd,
            prompt=prompt,
            context_id=self.context_id if context_id is None else context_id,
            task_id=self.pipeline_task_id if task_id is None else task_id,
            name=name,
            run_dir=self.run_dir,
            timeout=self.args.stream_timeout,
            redaction_env=self.server_env,
        )
        self._remember_identity(summary)
        self.summaries[name] = summary
        return summary

    def start_stream(
        self,
        *,
        prompt: str,
        name: str,
        context_id: str | None = None,
        task_id: str | None = None,
    ) -> BackgroundStream:
        stream = BackgroundStream(
            server_url=self.server_url,
            cwd=self.cwd,
            prompt=prompt,
            context_id=self.context_id if context_id is None else context_id,
            task_id=self.pipeline_task_id if task_id is None else task_id,
            name=name,
            run_dir=self.run_dir,
            timeout=self.args.stream_timeout,
            redaction_env=self.server_env,
        )
        stream.start()
        stream.wait_for(
            lambda _event, summary: bool(summary.context_id and summary.task_id),
            description="task identity",
            timeout=self.args.event_timeout,
        )
        self._remember_identity(stream.summary)
        self.summaries[name] = stream.summary
        return stream

    def fetch_state(self, name: str) -> Any:
        snapshot = fetch_pipeline_state(
            server_url=self.server_url,
            context_id=self.context_id,
            task_id=self.pipeline_task_id,
            run_dir=self.run_dir,
            name=name,
            redaction_env=self.server_env,
        )
        self.snapshots[name] = snapshot
        return snapshot

    def capture_task_snapshots(self, label: str) -> dict[str, Any]:
        if not self.context_id:
            raise RuntimeError("context id is unknown")
        if not self.pipeline_task_id:
            raise RuntimeError("pipeline task id is unknown")
        task_get = fetch_task(
            server_url=self.server_url,
            task_id=self.pipeline_task_id,
            run_dir=self.run_dir,
            name=label,
            redaction_env=self.server_env,
        )
        task_list = fetch_tasks(
            server_url=self.server_url,
            context_id=self.context_id,
            run_dir=self.run_dir,
            name=label,
            redaction_env=self.server_env,
        )
        self.snapshots[f"{label}.task-get"] = task_get
        self.snapshots[f"{label}.task-list"] = task_list
        return {"task_get": task_get, "task_list": task_list}

    def wait_for_server_exit(self, *, expected_returncode: int | None = None, timeout: float) -> int:
        if self.server is None or self.server.process is None:
            raise RuntimeError("server is not running")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            returncode = self.server.process.poll()
            if returncode is not None:
                if expected_returncode is not None and returncode != expected_returncode:
                    raise RuntimeError(
                        f"server exited with unexpected return code {returncode}; expected {expected_returncode}"
                    )
                self.server.terminate()
                return returncode
            time.sleep(0.25)
        self.notes.append("server did not exit after deterministic fault; sending SIGKILL for cleanup")
        self.server.kill9()
        raise RuntimeError(f"server did not exit within {timeout:.0f}s after deterministic fault")

    def disable_fault_injection(self) -> None:
        self.server_env.pop("IAC_CODE_TEST_FAULT_INJECTION", None)
        self.server_env.pop("IAC_CODE_TEST_FAULT_INJECTION_MODE", None)
        self.server_env.pop("IAC_CODE_TEST_CRASH_AT", None)
        self.notes.append("disabled fault injection before restart")

    def cancel_pipeline_task(self, name: str) -> Any:
        if not self.pipeline_task_id:
            raise RuntimeError("pipeline task id is unknown")
        payload = build_task_cancel_payload(task_id=self.pipeline_task_id, request_id=str(uuid.uuid4()))
        _append_jsonl(
            self.run_dir / "requests.jsonl",
            {"name": name, "payload": payload, "at": _utc_now()},
            self.server_env,
        )
        request = Request(
            self.server_url.rstrip("/") + "/",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else None
        except Exception as exc:
            data = {"error": str(exc)}
        redacted = _redact_json_value(data, self.server_env)
        _write_json(self.run_dir / f"{name}.cancel-response.json", redacted)
        return redacted

    def _remember_identity(self, summary: StreamSummary) -> None:
        if summary.context_id:
            if self.context_id and summary.context_id != self.context_id:
                self.checks[f"{summary.name} stayed in same context"] = False
            self.context_id = self.context_id or summary.context_id
        if summary.task_id and not self.pipeline_task_id:
            self.pipeline_task_id = summary.task_id

    def finish(self, *, passed: bool | None = None, abort_reason: str = "") -> int:
        if passed is None:
            passed = bool(self.checks) and all(self.checks.values())
        result = ScenarioRunResult(
            scenario=self.scenario,
            run_dir=str(self.run_dir),
            server_url=self.server_url,
            context_id=self.context_id,
            pipeline_task_id=self.pipeline_task_id,
            passed=passed,
            checks=self.checks,
            abort_reason=abort_reason,
            notes=self.notes,
        )
        payload = {
            **asdict(result),
            "streams": {name: asdict(summary) for name, summary in self.summaries.items()},
            "snapshots": self.snapshots,
        }
        _write_json(self.run_dir / "summary.json", payload)
        _print_result(result)
        return 0 if result.passed else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run A2A session recovery E2E scenarios.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(_SCENARIOS),
        help="Scenario to run. Can be repeated. Defaults to scenario1.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="A2A server port. 0 chooses a free port per scenario.")
    parser.add_argument(
        "--cwd",
        default="",
        help="Workspace cwd sent in A2A metadata. Defaults to <run-dir>/workspace.",
    )
    parser.add_argument("--server-cwd", default=str(Path.cwd()))
    parser.add_argument("--run-root", default=str(Path(tempfile.gettempdir()) / RUN_LOG_ROOT_NAME))
    parser.add_argument("--run-dir", default="", help="Explicit run dir. Only valid when running one scenario.")
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-base", default="")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help=(
            "Enable deterministic test fault injection. This fixes the crash point only; "
            "it does not mock the pipeline, LLM, tools, or cloud APIs after restart."
        ),
    )
    parser.add_argument(
        "--fault-at",
        default="",
        help="Named deterministic fault point, for example after_a2a_pipeline_snapshot_saved.",
    )
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--preflight-timeout", type=float, default=60.0)
    parser.add_argument("--server-timeout", type=float, default=45.0)
    parser.add_argument("--stream-timeout", type=float, default=1800.0)
    parser.add_argument("--event-timeout", type=float, default=240.0)
    parser.add_argument("--leave-server-running", action="store_true")
    parser.add_argument("--no-auto-approve-permissions", action="store_true")
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--selection-prompt", default=DEFAULT_SELECTION_PROMPT)
    parser.add_argument("--normal-followup-prompt", default=DEFAULT_NORMAL_FOLLOWUP_PROMPT)
    parser.add_argument("--recovery-prompt", default=DEFAULT_RECOVERY_PROMPT)
    parser.add_argument("--expected-text", default=DEFAULT_NORMAL_FOLLOWUP_PROMPT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scenarios = args.scenario or ["scenario1"]
    if args.run_dir and len(scenarios) != 1:
        raise SystemExit("--run-dir can only be used with a single --scenario")
    for scenario in scenarios:
        _validate_scenario_execution(args, scenario)
    results: list[int] = []
    for scenario in scenarios:
        runner = _SCENARIOS[scenario]
        results.append(runner(args, scenario))
    return 0 if all(code == 0 for code in results) else 1


def _run_with_harness(args: argparse.Namespace, scenario: str, callback: Callable[[ScenarioHarness], None]) -> int:
    harness = ScenarioHarness(args, scenario=scenario)
    try:
        harness.preflight()
        harness.start_server()
        callback(harness)
        return harness.finish()
    except Exception as exc:
        harness.notes.append(f"exception: {type(exc).__name__}: {exc}")
        return harness.finish(passed=False, abort_reason=str(exc))
    finally:
        harness.terminate()


def run_scenario1(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
        h.checks["initial stream reached input_required"] = _reached_input_required(initial)
        h.checks["initial input_required is step4 confirm_and_select"] = (
            initial.last_input_required_step_id == "confirm_and_select"
        )
        selection = h.stream(prompt=args.selection_prompt, name="02-select-candidate")
        h.checks["selection completed pipeline"] = _pipeline_completed(selection)
        h.checks["selection produced normal handoff"] = selection.normal_handoff_ready
        h.snapshots["after_pipeline"] = h.fetch_state("after-pipeline")
        _add_completed_snapshot_checks(
            h.checks,
            "after-pipeline state",
            h.snapshots["after_pipeline"],
            context_id=h.context_id,
            task_id=h.pipeline_task_id,
        )
        normal = h.stream(prompt=args.normal_followup_prompt, name="03-normal-followup", task_id="")
        h.checks["normal follow-up stayed in same context"] = normal.context_id == h.context_id
        h.checks["normal follow-up used a new task"] = bool(normal.task_id) and normal.task_id != h.pipeline_task_id
        h.checks["normal follow-up finished turn"] = _normal_turn_finished(normal)
        h.checks["normal follow-up produced text"] = bool(normal.text.strip())
        h.kill9_and_restart()
        h.snapshots["after_restart"] = h.fetch_state("after-restart")
        _add_completed_snapshot_checks(
            h.checks,
            "after-restart state",
            h.snapshots["after_restart"],
            context_id=h.context_id,
            task_id=h.pipeline_task_id,
        )
        recovery = h.stream(prompt=args.recovery_prompt, name="04-recovery-question", task_id="")
        h.checks["recovery stayed in same context"] = recovery.context_id == h.context_id
        h.checks["recovery used a new task"] = bool(recovery.task_id) and recovery.task_id not in {
            h.pipeline_task_id,
            normal.task_id,
        }
        h.checks["recovery finished turn"] = _normal_turn_finished(recovery)
        h.checks["recovery answer mentions previous question"] = args.expected_text in recovery.text
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_running_step(args: argparse.Namespace, scenario: str) -> int:
    step_id = _RUNNING_STEP_SCENARIOS[scenario]

    def callback(h: ScenarioHarness) -> None:
        if step_id == "deploying":
            initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
            initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
            h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"
            stream = h.start_stream(prompt=args.selection_prompt, name="02-select-candidate-running")
        else:
            stream = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        if step_id == "evaluate_candidates":
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                [stream],
                _candidate_started,
                description="candidate started in evaluate_candidates",
                timeout=args.event_timeout,
                name_prefix="initial-running",
            )
        else:
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                [stream],
                _step_started(step_id),
                description=f"step_started({step_id})",
                timeout=args.event_timeout,
                name_prefix="initial-running",
            )
        h.fetch_state("before-kill")
        h.kill9_and_restart()
        for observed_stream in observed_streams:
            _join_after_kill(observed_stream, h)
        snapshot = h.fetch_state("after-restart")
        h.checks["state endpoint returned snapshot after restart"] = _snapshot(snapshot) is not None
        h.checks["pipeline taskId persisted"] = _snapshot_value(snapshot, "taskId") == h.pipeline_task_id
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="03-continue-after-restart")
        _finish_pipeline_after_possible_input(h, resumed, args)
        h.checks["pipeline completed after recovery"] = _completed_snapshot_or_stream(h, resumed)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_normal_running(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        _complete_pipeline(h, args)
        normal = h.start_stream(prompt=args.normal_followup_prompt, name="03-normal-followup-running", task_id="")
        normal.wait_for(_normal_text_started, description="normal chat text started", timeout=args.event_timeout)
        h.kill9_and_restart()
        _join_after_kill(normal, h)
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="04-normal-continue", task_id="")
        h.checks["normal continue stayed in same context"] = resumed.context_id == h.context_id
        final = h.stream(prompt=DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT, name="05-normal-history-check", task_id="")
        h.checks["history check stayed in same context"] = final.context_id == h.context_id
        h.checks["history answer mentions previous question"] = args.normal_followup_prompt in final.text

    return _run_with_harness(args, scenario, callback)


def run_ask_waiting(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=ASK_TRIGGER_PROMPT, name="01-ask-trigger", context_id="", task_id="")
        h.checks["initial reached input_required"] = _reached_input_required(initial)
        h.checks["input_required is ask_user_question"] = (
            _latest_pending_kind(h.run_dir / "01-ask-trigger.events.jsonl") == "ask_user_question"
        )
        h.kill9_and_restart()
        snapshot = h.fetch_state("after-restart")
        h.checks["snapshot still waiting input"] = _snapshot_value(snapshot, "status") == "waiting_input"
        h.checks["pending input is ask_user_question"] = _pending_kind(snapshot) == "ask_user_question"
        answer = h.stream(prompt=ASK_FIRST_ANSWER, name="02-answer-first-ask", task_id="")
        _add_hydrated_task_checks(h, answer, "first ask answer")
        final_summary = answer
        if answer.last_input_required_step_id:
            second = h.stream(prompt=ASK_SECOND_ANSWER, name="03-answer-second-ask")
            _add_same_task_checks(h, second, "second ask answer")
            _finish_pipeline_after_possible_input(h, second, args)
            final_summary = second
        else:
            _finish_pipeline_after_possible_input(h, answer, args)
        h.checks["pipeline completed after ask recovery"] = _completed_snapshot_or_stream(h, final_summary)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_selection_waiting(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
        h.checks["initial reached step4 input_required"] = initial.last_input_required_step_id == "confirm_and_select"
        h.kill9_and_restart()
        snapshot = h.fetch_state("after-restart")
        h.checks["snapshot still waiting input"] = _snapshot_value(snapshot, "status") == "waiting_input"
        h.checks["pending input is confirm_and_select"] = _pending_step_id(snapshot) == "confirm_and_select"
        selection = h.stream(prompt=args.selection_prompt, name="02-select-after-restart", task_id="")
        _add_hydrated_task_checks(h, selection, "selection answer")
        h.checks["selection completed pipeline"] = _pipeline_completed(selection)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_rollback(args: argparse.Namespace, scenario: str) -> int:
    target_step = _ROLLBACK_SCENARIOS[scenario]

    def callback(h: ScenarioHarness) -> None:
        initial = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        observed_streams = _wait_for_with_intervening_ask_inputs(
            h,
            [initial],
            _candidate_started,
            description="candidate started before rollback",
            timeout=args.event_timeout,
            name_prefix="initial-running",
        )
        rollback = h.start_stream(prompt=ROLLBACK_PROMPT, name="02-rollback-interrupt")
        _wait_any(
            [*observed_streams, rollback],
            _event_type("rollback_completed"),
            description="rollback_completed",
            timeout=args.event_timeout,
        )
        streams_to_join = [*observed_streams, rollback]
        if target_step == "deploying":
            _wait_any(
                [*observed_streams, rollback],
                _input_required_step("confirm_and_select"),
                description="post-rollback input_required(confirm_and_select)",
                timeout=args.event_timeout,
            )
            selection = h.start_stream(prompt=args.selection_prompt, name="03-select-after-rollback")
            streams_to_join.append(selection)
            _wait_any(
                [selection],
                _step_started(target_step),
                description=f"post-rollback step_started({target_step})",
                timeout=args.event_timeout,
            )
        else:
            _wait_any(
                [*observed_streams, rollback],
                _step_started(target_step),
                description=f"post-rollback step_started({target_step})",
                timeout=args.event_timeout,
            )
        h.fetch_state("before-kill")
        h.kill9_and_restart()
        for stream in streams_to_join:
            _join_after_kill(stream, h)
        snapshot = h.fetch_state("after-restart")
        h.checks["state endpoint returned snapshot after rollback restart"] = _snapshot(snapshot) is not None
        resumed = h.stream(
            prompt=CONTINUE_PROMPT,
            name="04-continue-after-restart" if target_step == "deploying" else "03-continue-after-restart",
        )
        _finish_pipeline_after_possible_input(h, resumed, args)
        h.checks["pipeline completed after rollback recovery"] = _completed_snapshot_or_stream(h, resumed)
        final_state = h.fetch_state("after-rollback-completion")
        final_deploying = _final_deployment_evidence(final_state)
        h.checks["final deploying target is security group"] = _has_any_marker(
            final_deploying,
            SECURITY_GROUP_MARKERS,
        )
        h.checks["final deploying target is not VSwitch"] = not _has_any_marker(final_deploying, VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_cancel(args: argparse.Namespace, scenario: str) -> int:
    step_id = _CANCEL_SCENARIOS[scenario]

    def callback(h: ScenarioHarness) -> None:
        if step_id == "deploying":
            initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
            initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
            h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"
            stream = h.start_stream(prompt=args.selection_prompt, name="02-select-candidate-running")
        else:
            stream = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        if step_id == "evaluate_candidates":
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                [stream],
                _candidate_started,
                description="candidate started before cancel",
                timeout=args.event_timeout,
                name_prefix="initial-running",
            )
        else:
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                [stream],
                _step_started(step_id),
                description=f"step_started({step_id})",
                timeout=args.event_timeout,
                name_prefix="initial-running",
            )
        cancel_response = h.cancel_pipeline_task("cancel")
        h.checks["CancelTask returned response"] = isinstance(cancel_response, dict) and "error" not in cancel_response
        _wait_any_or_note(observed_streams, _status_state("TASK_STATE_CANCELED"), h, description="TASK_STATE_CANCELED")
        h.fetch_state("after-cancel")
        normal = h.stream(prompt=args.normal_followup_prompt, name="03-normal-after-cancel", task_id="")
        h.checks["normal chat after cancel stayed in same context"] = normal.context_id == h.context_id
        h.checks["normal chat after cancel finished"] = _normal_turn_finished(normal)
        h.kill9_and_restart()
        snapshot = h.fetch_state("after-restart")
        h.checks["snapshot remains canceled after restart"] = _snapshot_value(snapshot, "status") == "canceled"
        final = h.stream(prompt=args.recovery_prompt, name="04-normal-history-check", task_id="")
        h.checks["history answer after cancel mentions previous question"] = args.normal_followup_prompt in final.text

    return _run_with_harness(args, scenario, callback)


def run_fault_after_snapshot(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        if not args.deterministic:
            raise RuntimeError("fault-after-snapshot requires --deterministic")
        initial = BackgroundStream(
            server_url=h.server_url,
            cwd=h.cwd,
            prompt=args.initial_prompt,
            context_id="",
            task_id="",
            name="01-initial-fault",
            run_dir=h.run_dir,
            timeout=h.args.stream_timeout,
            redaction_env=h.server_env,
        )
        h.summaries[initial.name] = initial.summary
        initial.start()
        _join_after_kill(initial, h)
        exit_code = h.wait_for_server_exit(expected_returncode=97, timeout=min(20.0, h.args.event_timeout))
        h.checks["deterministic fault exited with code 97"] = exit_code == 97
        h.disable_fault_injection()
        h.start_server()
        discovered = fetch_tasks(
            server_url=h.server_url,
            context_id="",
            run_dir=h.run_dir,
            name="after-restart-discovery",
            redaction_env=h.server_env,
        )
        h.snapshots["after-restart-discovery.task-list"] = discovered
        task_identity = _latest_task_identity(discovered)
        h.checks["initial stream captured contextId"] = bool(task_identity.get("contextId"))
        h.checks["initial stream captured pipeline taskId"] = bool(task_identity.get("taskId"))
        if not h.checks["initial stream captured contextId"] or not h.checks["initial stream captured pipeline taskId"]:
            raise RuntimeError("could not discover persisted task identity after deterministic restart")
        h.context_id = str(task_identity["contextId"])
        h.pipeline_task_id = str(task_identity["taskId"])
        h.snapshots["after_restart"] = h.fetch_state("after-restart")
        after_restart = h.capture_task_snapshots("after-restart")
        h.checks["task_get_after_restart"] = _task_response_matches(
            after_restart["task_get"],
            task_id=h.pipeline_task_id,
            context_id=h.context_id,
        )
        h.checks["task_list_after_restart"] = _task_list_contains(
            after_restart["task_list"],
            task_id=h.pipeline_task_id,
            context_id=h.context_id,
        )
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="02-continue-after-restart", task_id="")
        _add_hydrated_task_checks(h, resumed, "continue")
        _finish_pipeline_after_possible_input(h, resumed, args)
        after_continue = h.capture_task_snapshots("after-continue")
        h.checks["task_get_after_continue_completed"] = _task_response_matches(
            after_continue["task_get"],
            task_id=h.pipeline_task_id,
            context_id=h.context_id,
        ) and _task_status_state(after_continue["task_get"]) == "TASK_STATE_COMPLETED"
        h.checks["task_list_after_continue_kept_recovered_task"] = _task_list_contains(
            after_continue["task_list"],
            task_id=h.pipeline_task_id,
            context_id=h.context_id,
        )
        h.checks["pipeline_completed"] = _completed_snapshot_or_stream(h, resumed)
        h.checks["created_vswitch"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def _complete_pipeline(h: ScenarioHarness, args: argparse.Namespace) -> None:
    initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
    initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
    h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"
    selection = h.stream(prompt=args.selection_prompt, name="02-select-candidate")
    h.checks["selection completed pipeline"] = _pipeline_completed(selection)
    h.checks["selection produced normal handoff"] = selection.normal_handoff_ready
    h.snapshots["after_pipeline"] = h.fetch_state("after-pipeline")


def _finish_pipeline_after_possible_input(h: ScenarioHarness, summary: StreamSummary, args: argparse.Namespace) -> None:
    current = summary
    for idx in range(1, 5):
        if _pipeline_completed(current):
            return
        if current.last_input_required_step_id == "confirm_and_select":
            current = h.stream(prompt=args.selection_prompt, name=f"select-after-resume-{idx}")
            continue
        if _reached_input_required(current):
            current = h.stream(prompt=CONTINUE_PROMPT, name=f"continue-after-input-{idx}")
            continue
        if current.last_status_state in {"TASK_STATE_FAILED", "TASK_STATE_CANCELED"}:
            return
        snapshot = h.fetch_state(f"post-resume-{idx}")
        if _snapshot_value(snapshot, "status") == "completed":
            return
        if (
            _snapshot_value(snapshot, "status") == "waiting_input"
            and _pending_step_id(snapshot) == "confirm_and_select"
        ):
            current = h.stream(prompt=args.selection_prompt, name=f"select-from-snapshot-{idx}")
            continue
        current = h.stream(prompt=CONTINUE_PROMPT, name=f"continue-loop-{idx}")


def _apply_event(summary: StreamSummary, payload: Any) -> None:
    summary.event_count += 1
    identity = _a2a_task_identity(payload)
    if identity is not None:
        summary.task_id = str(identity.get("taskId") or summary.task_id)
        summary.context_id = str(identity.get("contextId") or summary.context_id)
        state = identity.get("state")
        if state:
            summary.status_states.append(str(state))

    envelope = _extract_pipeline_envelope(payload)
    if envelope is not None:
        summary.task_id = str(envelope.get("taskId") or envelope.get("task_id") or summary.task_id)
        summary.context_id = str(envelope.get("contextId") or envelope.get("context_id") or summary.context_id)
        event_type = str(envelope.get("eventType") or envelope.get("event_type") or "")
        if event_type:
            summary.pipeline_event_types.append(event_type)
        if event_type == "input_required":
            step = envelope.get("step")
            if isinstance(step, dict):
                summary.last_input_required_step_id = str(step.get("id") or "")
        if _is_normal_handoff(envelope):
            summary.normal_handoff_ready = True

    for text in _status_message_texts(payload):
        summary.text += text


def _is_normal_handoff(envelope: dict[str, Any]) -> bool:
    data = envelope.get("data")
    return (
        isinstance(data, dict)
        and (envelope.get("eventType") or envelope.get("event_type")) == "pipeline_handoff_ready"
        and data.get("action") == "switch_to_normal"
        and (data.get("targetMode") or data.get("target_mode")) == "normal"
    )


def _step_started(step_id: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        envelope = _extract_pipeline_envelope(event)
        step = envelope.get("step") if isinstance(envelope, dict) else None
        return (
            isinstance(envelope, dict)
            and envelope.get("eventType") == "step_started"
            and isinstance(step, dict)
            and step.get("id") == step_id
        )

    return predicate


def _candidate_started(event: Any, _summary: StreamSummary) -> bool:
    envelope = _extract_pipeline_envelope(event)
    return isinstance(envelope, dict) and envelope.get("eventType") in {"candidate_started", "candidate_step_started"}


def _input_required_step(step_id: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        envelope = _extract_pipeline_envelope(event)
        step = envelope.get("step") if isinstance(envelope, dict) else None
        data = envelope.get("data") if isinstance(envelope, dict) else None
        data_step_id = data.get("stepId") if isinstance(data, dict) else None
        return (
            isinstance(envelope, dict)
            and envelope.get("eventType") == "input_required"
            and (
                (isinstance(step, dict) and step.get("id") == step_id)
                or data_step_id == step_id
            )
        )

    return predicate


def _event_type(event_type: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        envelope = _extract_pipeline_envelope(event)
        return isinstance(envelope, dict) and envelope.get("eventType") == event_type

    return predicate


def _status_state(state: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        identity = _a2a_task_identity(event)
        return isinstance(identity, dict) and identity.get("state") == state

    return predicate


def _normal_text_started(event: Any, _summary: StreamSummary) -> bool:
    return bool(_status_message_texts(event))


def _wait_any(
    streams: Iterable[BackgroundStream],
    predicate: Callable[[Any, StreamSummary], bool],
    *,
    description: str,
    timeout: float,
) -> EventMatch:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        for stream in streams:
            try:
                return stream.wait_for(predicate, description=description, timeout=0.25)
            except TimeoutError:
                continue
            except RuntimeError as exc:
                last_error = str(exc)
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {description}; last_error={last_error}")


def _wait_for_with_intervening_ask_inputs(
    h: ScenarioHarness,
    streams: Iterable[BackgroundStream],
    predicate: Callable[[Any, StreamSummary], bool],
    *,
    description: str,
    timeout: float,
    name_prefix: str,
) -> list[BackgroundStream]:
    active_streams = list(streams)
    handled_finished_streams: set[int] = set()
    answered_count = 0
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        for stream in list(active_streams):
            try:
                stream.wait_for(predicate, description=description, timeout=0.25)
                return active_streams
            except TimeoutError:
                continue
            except RuntimeError as exc:
                last_error = str(exc)
                stream_id = id(stream)
                if stream_id in handled_finished_streams:
                    continue
                handled_finished_streams.add(stream_id)
                if _latest_input_required_kind_from_events(stream.events) != "ask_user_question":
                    continue
                answered_count += 1
                if answered_count > 4:
                    raise RuntimeError(f"too many intervening ask_user_question inputs before {description}") from exc
                h.notes.append(
                    f"answered intervening ask_user_question while waiting for {description}: {stream.name}"
                )
                answer = h.start_stream(
                    prompt=INTERVENING_ASK_ANSWER,
                    name=f"{name_prefix}-answer-ask-{answered_count}",
                )
                active_streams.append(answer)
                break
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {description}; last_error={last_error}")


def _wait_any_or_note(
    streams: Iterable[BackgroundStream],
    predicate: Callable[[Any, StreamSummary], bool],
    h: ScenarioHarness,
    *,
    description: str,
) -> None:
    try:
        _wait_any(streams, predicate, description=description, timeout=min(30.0, h.args.event_timeout))
    except Exception as exc:
        h.notes.append(f"did not observe {description}: {exc}")


def _wait_or_note(
    stream: BackgroundStream,
    predicate: Callable[[Any, StreamSummary], bool],
    h: ScenarioHarness,
    *,
    description: str,
) -> None:
    try:
        stream.wait_for(predicate, description=description, timeout=min(30.0, h.args.event_timeout))
    except Exception as exc:
        h.notes.append(f"did not observe {description}: {exc}")


def _answer_intervening_ask_inputs(
    h: ScenarioHarness,
    summary: StreamSummary,
    *,
    name_prefix: str,
) -> StreamSummary:
    current = summary
    for idx in range(1, 5):
        if _pipeline_completed(current) or current.last_input_required_step_id == "confirm_and_select":
            return current
        if not _reached_input_required(current):
            return current
        kind = _latest_pending_kind(h.run_dir / f"{current.name}.events.jsonl")
        if kind != "ask_user_question":
            return current
        h.notes.append(f"answered intervening ask_user_question before step4 selection: {current.name}")
        current = h.stream(prompt=INTERVENING_ASK_ANSWER, name=f"{name_prefix}-answer-ask-{idx}")
    return current


def _join_after_kill(stream: BackgroundStream, h: ScenarioHarness) -> None:
    try:
        stream.join(timeout=10)
    except Exception as exc:
        h.notes.append(f"{stream.name} ended after kill with: {type(exc).__name__}: {exc}")


def _reached_input_required(summary: StreamSummary) -> bool:
    return "TASK_STATE_INPUT_REQUIRED" in summary.status_states or "input_required" in summary.pipeline_event_types


def _pipeline_completed(summary: StreamSummary) -> bool:
    return summary.last_status_state == "TASK_STATE_COMPLETED" or "pipeline_completed" in summary.pipeline_event_types


def _add_same_task_checks(h: ScenarioHarness, summary: StreamSummary, prefix: str) -> None:
    h.checks[f"{prefix} requested recovered taskId"] = summary.request_task_id == h.pipeline_task_id
    h.checks[f"{prefix} stayed in recovered context"] = summary.context_id == h.context_id
    h.checks[f"{prefix} streamed recovered taskId"] = summary.task_id == h.pipeline_task_id


def _add_hydrated_task_checks(h: ScenarioHarness, summary: StreamSummary, prefix: str) -> None:
    h.checks[f"{prefix} omitted taskId"] = summary.request_task_id == ""
    h.checks[f"{prefix} stayed in recovered context"] = summary.context_id == h.context_id
    h.checks[f"{prefix} hydrated recovered taskId"] = summary.task_id == h.pipeline_task_id


def _completed_snapshot_or_stream(h: ScenarioHarness, summary: StreamSummary) -> bool:
    if _pipeline_completed(summary):
        return True
    snapshot = h.fetch_state("completion-check")
    return _snapshot_value(snapshot, "status") == "completed"


def _jsonrpc_post(
    *,
    server_url: str,
    payload: dict[str, Any],
    run_dir: Path,
    name: str,
    suffix: str,
    redaction_env: dict[str, str] | None = None,
) -> Any:
    _append_jsonl(
        run_dir / "requests.jsonl",
        {"name": name, "payload": payload, "at": _utc_now()},
        redaction_env,
    )
    artifact: dict[str, Any] = {"request": _redact_json_value(payload, redaction_env)}
    request = Request(
        server_url.rstrip("/") + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else None
            artifact["http_status"] = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = {"body": _redact_sensitive_text(raw, redaction_env)}
        artifact["http_status"] = exc.code
        artifact["error"] = f"HTTP {exc.code}"
    except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
        data = {"error": str(exc)}
        artifact["http_status"] = 0
        artifact["error"] = str(exc)
    artifact["response"] = _redact_json_value(data, redaction_env)
    _write_json(run_dir / f"{name}.{suffix}.json", artifact)
    return artifact


def fetch_task(
    server_url: str,
    task_id: str,
    run_dir: Path,
    name: str,
    redaction_env: dict[str, str] | None,
    history_length: int | None = None,
) -> Any:
    payload = build_task_get_payload(task_id=task_id, history_length=history_length, request_id=str(uuid.uuid4()))
    return _jsonrpc_post(
        server_url=server_url,
        payload=payload,
        run_dir=run_dir,
        name=name,
        suffix="task-get",
        redaction_env=redaction_env,
    )


def fetch_tasks(
    server_url: str,
    context_id: str,
    run_dir: Path,
    name: str,
    redaction_env: dict[str, str] | None,
) -> Any:
    params: dict[str, Any] = {"includeArtifacts": False}
    if context_id:
        params["contextId"] = context_id
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "ListTasks",
        "params": params,
    }
    return _jsonrpc_post(
        server_url=server_url,
        payload=payload,
        run_dir=run_dir,
        name=name,
        suffix="task-list",
        redaction_env=redaction_env,
    )


def _snapshot(response: Any) -> dict[str, Any] | None:
    snapshot = response.get("snapshot") if isinstance(response, dict) else None
    return snapshot if isinstance(snapshot, dict) else None


def _jsonrpc_result(response: Any) -> Any:
    if not isinstance(response, dict):
        return None
    payload = response.get("response")
    if not isinstance(payload, dict):
        return None
    return payload.get("result")


def _task_response_matches(response: Any, *, task_id: str, context_id: str) -> bool:
    result = _jsonrpc_result(response)
    identity = _a2a_task_identity(result)
    return (
        isinstance(identity, dict)
        and identity.get("taskId") == task_id
        and identity.get("contextId") == context_id
    )


def _task_status_state(response: Any) -> str:
    result = _jsonrpc_result(response)
    status = result.get("status") if isinstance(result, dict) else None
    state = status.get("state") if isinstance(status, dict) else None
    return state if isinstance(state, str) else ""


def _task_list_contains(response: Any, *, task_id: str, context_id: str) -> bool:
    result = _jsonrpc_result(response)
    tasks = result.get("tasks") if isinstance(result, dict) else None
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        identity = _a2a_task_identity(task)
        if (
            isinstance(identity, dict)
            and identity.get("taskId") == task_id
            and identity.get("contextId") == context_id
        ):
            return True
    return False


def _latest_task_identity(response: Any) -> dict[str, Any]:
    result = _jsonrpc_result(response)
    tasks = result.get("tasks") if isinstance(result, dict) else None
    if not isinstance(tasks, list):
        return {}
    for task in tasks:
        identity = _a2a_task_identity(task)
        if isinstance(identity, dict) and identity.get("taskId") and identity.get("contextId"):
            return identity
    return {}


def _snapshot_value(response: Any, key: str) -> Any:
    snapshot = _snapshot(response)
    return snapshot.get(key) if snapshot is not None else None


def _step_evidence(response: Any, step_id: str) -> str:
    snapshot = _snapshot(response)
    steps = snapshot.get("steps") if isinstance(snapshot, dict) else None
    if not isinstance(steps, list):
        return ""
    matches = [
        step
        for step in steps
        if isinstance(step, dict)
        and step.get("id") == step_id
        and step.get("status") in {"completed", "working", "waiting_input"}
    ]
    if not matches:
        return ""
    return json.dumps(matches[-1], ensure_ascii=False, default=str)


def _final_deployment_evidence(response: Any) -> str:
    evidence: dict[str, Any] = {"deploying_step": _step_evidence(response, "deploying")}
    handoff_context = _handoff_context(response)
    if isinstance(handoff_context, dict):
        evidence["deployment"] = handoff_context.get("deployment")
    return json.dumps(evidence, ensure_ascii=False, default=str)


def _handoff_context(response: Any) -> dict[str, Any] | None:
    snapshot = _snapshot(response)
    handoff = snapshot.get("normalHandoff") if isinstance(snapshot, dict) else None
    if not isinstance(handoff, dict):
        return None
    summary = handoff.get("summary")
    data = handoff.get("data")
    if not isinstance(summary, str) and isinstance(data, dict):
        summary = data.get("summary")
    if not isinstance(summary, str):
        return None
    marker = "Included context:\n"
    start = summary.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = summary.find("\n\nUse this context", start)
    raw_context = summary[start:] if end < 0 else summary[start:end]
    try:
        value = json.loads(raw_context)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _pending_kind(response: Any) -> str:
    snapshot = _snapshot(response)
    pending = snapshot.get("pendingInput") if isinstance(snapshot, dict) else None
    return str(pending.get("kind") or "") if isinstance(pending, dict) else ""


def _pending_step_id(response: Any) -> str:
    snapshot = _snapshot(response)
    pending = snapshot.get("pendingInput") if isinstance(snapshot, dict) else None
    step = pending.get("step") if isinstance(pending, dict) else None
    return str(step.get("id") or "") if isinstance(step, dict) else ""


def _latest_pending_kind(path: Path) -> str:
    kind = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return kind
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        envelope = _extract_pipeline_envelope(value)
        if isinstance(envelope, dict) and envelope.get("eventType") == "input_required":
            data = envelope.get("data")
            if isinstance(data, dict):
                kind = str(data.get("kind") or "")
    return kind


def _latest_input_required_kind_from_events(events: Iterable[Any]) -> str:
    kind = ""
    for value in events:
        envelope = _extract_pipeline_envelope(value)
        if isinstance(envelope, dict) and envelope.get("eventType") == "input_required":
            data = envelope.get("data")
            if isinstance(data, dict):
                kind = str(data.get("kind") or "")
    return kind


def _all_evidence(h: ScenarioHarness) -> str:
    return json.dumps(
        {
            "summaries": {name: asdict(summary) for name, summary in h.summaries.items()},
            "snapshots": h.snapshots,
        },
        ensure_ascii=False,
        default=str,
    )


def _has_any_marker(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def _scenario_run_dir(args: argparse.Namespace, scenario: str) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser()
    root = Path(args.run_root).expanduser()
    return _new_run_dir(root / scenario)


def _print_result(result: ScenarioRunResult) -> None:
    print(f"\nA2A session recovery scenario: {result.scenario}")
    print(f"run_dir: {result.run_dir}")
    print(f"server_url: {result.server_url}")
    print(f"context_id: {result.context_id}")
    print(f"pipeline_task_id: {result.pipeline_task_id}")
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


def _validate_scenario_execution(args: argparse.Namespace, scenario: str) -> None:
    if scenario == "fault-after-snapshot" and not args.deterministic:
        raise SystemExit("scenario fault-after-snapshot requires --deterministic")
    if scenario in _REAL_CLOUD_SCENARIOS and not args.allow_real_cloud:
        raise SystemExit(
            "refusing to run provider/tool/cloud recovery scenario without --allow-real-cloud: " + scenario
        )


_RUNNING_STEP_SCENARIOS = {
    "step1-running": "intent_parsing",
    "step2-running": "architecture_planning",
    "step3-running": "evaluate_candidates",
    "step4-running": "confirm_and_select",
    "step5-running": "deploying",
}
_ROLLBACK_SCENARIOS = {
    "rollback-step1": "intent_parsing",
    "rollback-step2": "architecture_planning",
    "rollback-step3": "evaluate_candidates",
    "rollback-step4": "confirm_and_select",
    "rollback-step5": "deploying",
}
_CANCEL_SCENARIOS = {
    "cancel-step1": "intent_parsing",
    "cancel-step2": "architecture_planning",
    "cancel-step3": "evaluate_candidates",
    "cancel-step4": "confirm_and_select",
    "cancel-step5": "deploying",
}
_REAL_CLOUD_SCENARIOS = {
    "fault-after-snapshot",
    "scenario1",
    "normal-running",
    "ask-waiting",
    "selection-waiting",
    *_RUNNING_STEP_SCENARIOS,
    *_ROLLBACK_SCENARIOS,
    *_CANCEL_SCENARIOS,
}
_SCENARIOS: dict[str, Callable[[argparse.Namespace, str], int]] = {
    "scenario1": run_scenario1,
    "normal-running": run_normal_running,
    "ask-waiting": run_ask_waiting,
    "selection-waiting": run_selection_waiting,
    "fault-after-snapshot": run_fault_after_snapshot,
    **{name: run_running_step for name in _RUNNING_STEP_SCENARIOS},
    **{name: run_rollback for name in _ROLLBACK_SCENARIOS},
    **{name: run_cancel for name in _CANCEL_SCENARIOS},
}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--kill-server-pid":
        os.kill(int(sys.argv[2]), signal.SIGKILL)
        raise SystemExit(0)
    raise SystemExit(main())
