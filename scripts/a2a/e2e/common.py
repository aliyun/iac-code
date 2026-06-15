#!/usr/bin/env python3
"""Shared helpers for headless A2A pipeline recovery E2E scripts."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

A2A_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(A2A_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(A2A_SCRIPTS_DIR))

from debugger import (  # noqa: E402
    A2A_VERSION_HEADERS,
    _a2a_task_identity,
    _extract_pipeline_envelope,
    _parse_sse_data_line,
    build_message_stream_payload,
)

DEFAULT_INITIAL_PROMPT = "选择一个已有vpc，创建一个vswitch"
DEFAULT_SELECTION_PROMPT = "你随便选一个方案。"
DEFAULT_NORMAL_FOLLOWUP_PROMPT = "你刚才创建了什么"
DEFAULT_RECOVERY_PROMPT = (
    "我刚才问了你哪些问题？请查看完整会话历史，只输出 [Pipeline Handoff Context] 注入上下文之后、"
    "当前这条消息之前的最后一条真实用户消息原文。排除空消息、以“请完成当前步骤”开头的 pipeline 控制消息、"
    "更早的方案选择消息（例如“你随便选一个方案。”），以及当前这条消息本身。不要解释。"
)
DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT = (
    "我刚才问了你哪些问题？请查看完整会话历史，只输出 [Pipeline Handoff Context] 注入上下文之后、"
    "当前这条消息之前的最后一条真实用户消息原文。排除空消息、内容等于“继续”的消息、"
    "以“请完成当前步骤”开头的 pipeline 控制消息、更早的方案选择消息（例如“你随便选一个方案。”），"
    "以及当前这条消息本身。不要解释。"
)
DEFAULT_EXPECTED_TEXT = DEFAULT_NORMAL_FOLLOWUP_PROMPT
RUN_LOG_ROOT_NAME = "iac-code-a2a-e2e-runs"
NORMAL_TURN_TERMINAL_STATES = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED"}


@dataclass
class StreamSummary:
    name: str
    prompt: str
    request_task_id: str = ""
    task_id: str = ""
    context_id: str = ""
    status_states: list[str] = field(default_factory=list)
    pipeline_event_types: list[str] = field(default_factory=list)
    last_input_required_step_id: str = ""
    normal_handoff_ready: bool = False
    text: str = ""
    event_count: int = 0

    @property
    def last_status_state(self) -> str:
        return self.status_states[-1] if self.status_states else ""

    @property
    def last_pipeline_event_type(self) -> str:
        return self.pipeline_event_types[-1] if self.pipeline_event_types else ""


class ManagedServer:
    def __init__(
        self,
        *,
        python_cmd: list[str],
        config_path: Path,
        process_cwd: str,
        allowed_cwd: str,
        env: dict[str, str],
        log_prefix: Path,
    ) -> None:
        self._python_cmd = list(python_cmd)
        self._config_path = config_path
        self._process_cwd = process_cwd
        self._allowed_cwd = allowed_cwd
        self._env = dict(env)
        self._env["PYTHONUTF8"] = "1"
        self._env["IAC_CODE_MODE"] = "pipeline"
        allowed = self._env.get("IACCODE_A2A_ALLOWED_CWDS", "")
        if allowed_cwd not in allowed.split(os.pathsep):
            self._env["IACCODE_A2A_ALLOWED_CWDS"] = os.pathsep.join(item for item in [allowed, allowed_cwd] if item)
        self._log_prefix = log_prefix
        self.process: subprocess.Popen[str] | None = None
        self._stdout_handle: Any | None = None
        self._stderr_handle: Any | None = None
        self._tee_threads: list[threading.Thread] = []

    def start(self) -> None:
        cmd = [*self._python_cmd, "-m", "iac_code.cli.main", "a2a", "--config", str(self._config_path)]
        self._stdout_handle = (self._log_prefix.with_suffix(".stdout.log")).open("w", encoding="utf-8")
        self._stderr_handle = (self._log_prefix.with_suffix(".stderr.log")).open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            cmd,
            cwd=self._process_cwd,
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        self._tee_threads = [
            _tee_stream(self.process.stdout, self._stdout_handle, self._env),
            _tee_stream(self.process.stderr, self._stderr_handle, self._env),
        ]

    def kill9(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self._signal_process_group(signal.SIGKILL)
        self.process.wait(timeout=20)
        self._close_logs()

    def terminate(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self._signal_process_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._signal_process_group(signal.SIGKILL)
                self.process.wait(timeout=15)
        self._close_logs()

    def _signal_process_group(self, signal_number: int) -> None:
        if self.process is None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal_number)
        except ProcessLookupError:
            return
        except OSError:
            self.process.send_signal(signal_number)

    def _close_logs(self) -> None:
        for thread in self._tee_threads:
            if thread.is_alive():
                thread.join(timeout=2)
        self._tee_threads = []
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None and not handle.closed:
                handle.flush()
                handle.close()


def stream_message(
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
) -> StreamSummary:
    payload = build_message_stream_payload(
        cwd=cwd,
        prompt=prompt,
        context_id=context_id,
        task_id=task_id,
        request_id=str(uuid.uuid4()),
        message_id=str(uuid.uuid4()),
    )
    _append_jsonl(run_dir / "requests.jsonl", {"name": name, "payload": payload, "at": _utc_now()}, redaction_env)
    request = Request(
        server_url.rstrip("/") + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
        method="POST",
    )
    summary = StreamSummary(name=name, prompt=prompt, request_task_id=task_id)
    try:
        with urlopen(request, timeout=timeout) as response:
            for line in response:
                parsed = _parse_sse_data_line(line)
                if parsed is None:
                    continue
                _append_jsonl(run_dir / f"{name}.events.jsonl", parsed, redaction_env)
                _apply_event(summary, parsed)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        redacted_body = _redact_sensitive_text(body, redaction_env)
        _append_jsonl(
            run_dir / f"{name}.events.jsonl",
            {"error": f"HTTP {exc.code}", "body": redacted_body},
            redaction_env,
        )
        raise RuntimeError(f"{name} failed with HTTP {exc.code}: {redacted_body[:500]}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        _append_jsonl(run_dir / f"{name}.events.jsonl", {"error": str(exc)}, redaction_env)
        raise RuntimeError(f"{name} stream failed: {exc}") from exc
    return summary


def run_llm_preflight(
    *,
    python_cmd: list[str],
    cwd: str,
    env: dict[str, str],
    timeout: float,
    run_dir: Path,
) -> dict[str, Any]:
    preflight_env = dict(env)
    preflight_env["PYTHONUTF8"] = "1"
    preflight_env["IAC_CODE_MODE"] = "normal"
    cmd = [*python_cmd, "-W", "ignore::RuntimeWarning", "-m", "iac_code.cli.main", "--prompt", "只回复 OK"]
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=preflight_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        output = _redact_sensitive_text(
            "\n".join(part for part in [result.stdout, result.stderr] if part),
            preflight_env,
        )
        summary = _compact_text(output) or f"exit code {result.returncode}"
        payload = {
            "ok": result.returncode == 0,
            "returnCode": result.returncode,
            "elapsedSeconds": round(elapsed, 3),
            "summary": summary,
            "stdout": _redact_sensitive_text(result.stdout, preflight_env),
            "stderr": _redact_sensitive_text(result.stderr, preflight_env),
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        output = _redact_sensitive_text("\n".join(part for part in [exc.stdout, exc.stderr] if part), preflight_env)
        payload = {
            "ok": False,
            "returnCode": None,
            "elapsedSeconds": round(elapsed, 3),
            "summary": f"timed out after {timeout:.0f}s" + (f": {_compact_text(output)}" if output else ""),
            "stdout": _redact_sensitive_text(exc.stdout or "", preflight_env),
            "stderr": _redact_sensitive_text(exc.stderr or "", preflight_env),
        }
    _write_json(run_dir / "preflight.json", payload)
    return payload


def fetch_pipeline_state(
    *,
    server_url: str,
    context_id: str,
    task_id: str,
    run_dir: Path,
    name: str,
    redaction_env: dict[str, str] | None = None,
) -> Any:
    query = urlencode({"contextId": context_id, "taskId": task_id})
    request = Request(server_url.rstrip("/") + f"/iac-code/pipeline/state?{query}", method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else None
    except Exception as exc:
        data = {"error": str(exc)}
    redacted = _redact_json_value(data, redaction_env)
    _write_json(run_dir / f"{name}.pipeline-state.json", redacted)
    return redacted


def wait_for_server(server_url: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urlopen(server_url.rstrip("/") + "/.well-known/agent-card.json", timeout=5) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"A2A server did not become ready within {timeout:.0f}s: {last_error}")


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
    if not isinstance(data, dict):
        return False
    return (
        (envelope.get("eventType") or envelope.get("event_type")) == "pipeline_handoff_ready"
        and data.get("action") == "switch_to_normal"
        and (data.get("targetMode") or data.get("target_mode")) == "normal"
    )


def _normal_turn_finished(summary: StreamSummary) -> bool:
    return any(state in NORMAL_TURN_TERMINAL_STATES for state in summary.status_states)


def _add_completed_snapshot_checks(
    checks: dict[str, bool],
    prefix: str,
    snapshot_response: Any,
    *,
    context_id: str,
    task_id: str,
) -> None:
    snapshot = snapshot_response.get("snapshot") if isinstance(snapshot_response, dict) else None
    handoff = snapshot.get("normalHandoff") if isinstance(snapshot, dict) else None
    checks[f"{prefix} found"] = isinstance(snapshot, dict)
    checks[f"{prefix} contextId matched"] = isinstance(snapshot, dict) and snapshot.get("contextId") == context_id
    checks[f"{prefix} taskId matched"] = isinstance(snapshot, dict) and snapshot.get("taskId") == task_id
    checks[f"{prefix} status completed"] = isinstance(snapshot, dict) and snapshot.get("status") == "completed"
    checks[f"{prefix} handoff to normal"] = (
        isinstance(handoff, dict)
        and handoff.get("action") == "switch_to_normal"
        and handoff.get("targetMode") == "normal"
    )


def _status_message_texts(payload: Any) -> list[str]:
    texts: list[str] = []
    if not isinstance(payload, dict):
        return texts

    def collect_from_message(message: Any) -> None:
        if not isinstance(message, dict):
            return
        parts = message.get("parts")
        if not isinstance(parts, list):
            return
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])

    status_update = payload.get("statusUpdate") or payload.get("status_update")
    if isinstance(status_update, dict):
        status = status_update.get("status")
        if isinstance(status, dict):
            collect_from_message(status.get("message"))

    result = payload.get("result")
    if isinstance(result, dict):
        _extend_unique(texts, _status_message_texts(result))
        task = result.get("task")
        if isinstance(task, dict):
            _extend_unique(texts, _status_message_texts(task))

    status = payload.get("status")
    if isinstance(status, dict):
        collect_from_message(status.get("message"))

    message = payload.get("message")
    if isinstance(message, dict) and message.get("role") == "ROLE_AGENT":
        collect_from_message(message)

    return texts


def _extend_unique(target: list[str], values: Iterable[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _write_server_config(
    run_dir: Path,
    *,
    host: str,
    port: int,
    auto_approve_permissions: bool,
) -> Path:
    path = run_dir / "a2a-server.yml"
    content = "\n".join(
        [
            "transport: http",
            f"host: {host}",
            f"port: {port}",
            f"persistence_dir: {run_dir / 'a2a-persistence'}",
            f"artifact_dir: {run_dir / 'a2a-artifacts'}",
            f"auto_approve_permissions: {'true' if auto_approve_permissions else 'false'}",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    return path


def _server_env(env: dict[str, str], *, provider: str, model: str, api_base: str) -> dict[str, str]:
    if provider:
        env["IAC_CODE_PROVIDER"] = provider
    if model:
        env["IAC_CODE_MODEL"] = model
    if api_base:
        env["IAC_CODE_BASE_URL"] = api_base
    return env


def _split_python_command(value: str) -> list[str]:
    parts = shlex.split(value)
    if not parts:
        raise ValueError("--python must not be empty")
    return parts


def _redact_sensitive_text(text: str, env: dict[str, str] | None) -> str:
    redacted = text
    for name, value in (env or {}).items():
        if not value or len(value) < 6:
            continue
        upper = name.upper()
        if any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            redacted = redacted.replace(value, "<redacted>")
    redacted = re.sub(r"(?i)(api[_ -]?key\\s*[:=]\\s*)[^\\s,'\"}]+", r"\\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(authorization\\s*[:=]\\s*)[^\\s,'\"}]+", r"\\1<redacted>", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-<redacted>", redacted)
    return redacted


def _redact_json_value(value: Any, env: dict[str, str] | None) -> Any:
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


def _compact_text(text: str, *, max_chars: int = 500) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def _new_run_dir(root: Path) -> Path:
    run_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return root / run_name


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _tee_stream(stream: Any, handle: Any, redaction_env: dict[str, str] | None) -> threading.Thread:
    if stream is None:
        return threading.Thread(target=lambda: None)

    def run() -> None:
        for line in stream:
            try:
                handle.write(_redact_sensitive_text(line, redaction_env))
                handle.flush()
            except ValueError:
                break

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _append_jsonl(path: Path, value: Any, redaction_env: dict[str, str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_redact_json_value(value, redaction_env), ensure_ascii=False, default=str) + "\n")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
