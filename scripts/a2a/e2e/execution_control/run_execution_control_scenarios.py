#!/usr/bin/env python3
"""Drive deterministic A2A execution-control scenarios through public endpoints."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TOKEN = "execution-control-e2e-token"
STACK_ID = "stack-execution-control-e2e-0001"
STACK_INSTANCES_OPERATION_ID = "stack-instances-operation-e2e-0001"
SCENARIO_MODES = {
    "warm-resume-pausing": ("normal", "pipeline"),
    "warm-resume-paused": ("normal", "pipeline"),
    "disconnect-timeout-after-operation-id": ("normal", "pipeline"),
    "disconnect-timeout-inflight-sync-call": ("pipeline",),
    "disconnect-timeout-backup-blocked": ("pipeline",),
    "natural-completion-while-pausing": ("normal",),
    "slow-termination-storage": ("normal",),
    "terminate-during-bootstrap": ("normal", "pipeline"),
    "terminate-during-bootstrap-disconnected": ("normal", "pipeline"),
    "disconnect-timeout-during-turn-backup": ("normal",),
    "legacy-cancel-idle": ("pipeline",),
    "slow-rollover-storage": ("normal",),
    "stack-instances-timeout-after-operation-id": ("normal",),
    "stack-instances-terminate-inflight": ("normal",),
    "recovery-during-normal-rollover": ("normal",),
}
REGRESSION_SCENARIOS = {
    "slow-termination-storage",
    "terminate-during-bootstrap",
    "terminate-during-bootstrap-disconnected",
    "disconnect-timeout-during-turn-backup",
    "legacy-cancel-idle",
    "slow-rollover-storage",
    "recovery-during-normal-rollover",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=tuple(SCENARIO_MODES), action="append", required=True)
    parser.add_argument("--mode", choices=("normal", "pipeline"), required=True)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--overall-timeout", type=float, default=35.0)
    return parser.parse_args()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _headers(*, json_body: bool = False) -> dict[str, str]:
    result = {"Authorization": "Bearer {}".format(TOKEN), "A2A-Version": "1.0"}
    if json_body:
        result["Content-Type"] = "application/json"
    return result


def _decode_line(line: bytes) -> dict[str, Any] | None:
    text = line.decode("utf-8", errors="replace").strip()
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first(events: list[dict[str, Any]], key: str) -> str | None:
    for event in events:
        for item in _walk(event):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _all_strings(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, str):
        result.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            result.extend(_all_strings(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_all_strings(child))
    return result


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_marker(control_dir: Path, name: str) -> None:
    _atomic_json(control_dir / name, {"createdAt": time.time(), "name": name})


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _wait_until(predicate: Callable[[], Any], *, timeout: float, description: str) -> Any:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except BaseException as exc:
            last_error = exc
        time.sleep(0.02)
    if last_error is not None:
        raise TimeoutError("timed out waiting for {}: {}".format(description, last_error)) from last_error
    raise TimeoutError("timed out waiting for {}".format(description))


class _BackgroundStream:
    def __init__(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        name: str,
        artifact_path: Path,
    ) -> None:
        self._url = url
        self._payload = payload
        self._timeout = timeout
        self.name = name
        self.artifact_path = artifact_path
        self._response: Any = None
        self._closed = False
        self.events: list[dict[str, Any]] = []
        self.error: BaseException | None = None
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="execution-control-{}-stream".format(name), daemon=True)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.touch()

    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.events)

    def close(self) -> None:
        self._closed = True
        response = self._response
        if response is not None:
            raw = getattr(getattr(response, "fp", None), "raw", None)
            stream_socket = getattr(raw, "_sock", None)
            if stream_socket is not None:
                try:
                    stream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream_socket.close()
        self._thread.join(timeout=2.0)

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("A2A stream did not finish")
        if self.error is not None and not self._closed:
            raise RuntimeError("A2A stream failed: {}".format(self.error)) from self.error

    def _run(self) -> None:
        request = Request(
            self._url.rstrip("/") + "/",
            data=json.dumps(self._payload, ensure_ascii=False).encode("utf-8"),
            headers=_headers(json_body=True),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                self._response = response
                for line in response:
                    event = _decode_line(line)
                    if event is not None:
                        with self._lock:
                            self.events.append(event)
                        _append_jsonl(self.artifact_path, event)
        except BaseException as exc:
            if not self._closed:
                self.error = exc
                _append_jsonl(self.artifact_path, {"streamError": "{}: {}".format(type(exc).__name__, exc)})
        finally:
            self._response = None
            self.done.set()


class _FixtureServer:
    def __init__(self, *, repo_root: Path, run_dir: Path, scenario: str, mode: str, timeout: float) -> None:
        self._run_dir = run_dir
        self._timeout = timeout
        self.port = _free_port()
        self.url = "http://127.0.0.1:{}".format(self.port)
        self._log_handle = (run_dir / "server.log").open("w", encoding="utf-8")
        self._started_at = time.time()
        self._stopped_at: float | None = None
        self._forced_kill = False
        self._stop_lock = threading.Lock()
        fixture = Path(__file__).with_name("execution_control_fixture_server.py")
        command = [
            sys.executable,
            str(fixture),
            "--port",
            str(self.port),
            "--run-dir",
            str(run_dir),
            "--config-dir",
            str(run_dir / "config"),
            "--persistence-dir",
            str(run_dir / "persistence"),
            "--artifact-dir",
            str(run_dir / "artifacts"),
            "--workspace",
            str(run_dir / "workspace"),
            "--staging-dir",
            str(run_dir / "staging"),
            "--backup-dir",
            str(run_dir / "shared-backup"),
            "--scenario",
            scenario,
            "--mode",
            mode,
        ]
        environment = os.environ.copy()
        source_path = str(repo_root / "src")
        environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
        self._process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=environment,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self) -> None:
        def healthy() -> bool:
            if self._process.poll() is not None:
                raise RuntimeError("fixture server exited with {}".format(self._process.returncode))
            try:
                status, body = _http_json("GET", self.url + "/health", timeout=0.5)
            except (URLError, TimeoutError):
                return False
            return status == 200 and body.get("status") == "healthy"

        _wait_until(healthy, timeout=self._timeout, description="fixture server health")

    def assert_running(self) -> None:
        return_code = self._process.poll()
        if return_code is not None:
            raise RuntimeError("fixture server exited with {}".format(return_code))

    def wait_for_idle_exit(self) -> None:
        assert self._process.wait(timeout=min(self._timeout, 5.0)) == 0

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped_at is not None:
                return
            for marker in (
                "release-tool",
                "release-provider",
                "release-sdk",
                "cloud-complete",
                "allow-shared",
                "release-bootstrap",
                "release-bootstrap-cleanup",
                "release-turn-backup",
                "release-storage",
                "release-background",
                "release-publication",
                "release-recovery",
            ):
                _write_marker(self._run_dir / "control", marker)
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._forced_kill = True
                    self._process.kill()
                    self._process.wait(timeout=5.0)
            self._stopped_at = time.time()
            self._log_handle.close()
            _atomic_json(self._run_dir / "server-lifecycle.json", self.lifecycle())

    def lifecycle(self) -> dict[str, Any]:
        return {
            "pid": self._process.pid,
            "startedAt": self._started_at,
            "stoppedAt": self._stopped_at,
            "returnCode": self._process.poll(),
            "forcedKill": self._forced_kill,
        }


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 2.0,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers=_headers(json_body=payload is not None), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            value = json.loads(body) if body else {}
            return int(response.status), value if isinstance(value, dict) else {"value": value}
    except HTTPError as exc:
        body = exc.read()
        try:
            value = json.loads(body) if body else {}
        except json.JSONDecodeError:
            value = {"error": body.decode("utf-8", errors="replace")}
        return int(exc.code), value if isinstance(value, dict) else {"value": value}


def _message_payload(workspace: Path) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": [{"text": "Run the deterministic execution-control fixture."}],
                "metadata": {"iac_code": {"cwd": str(workspace)}},
            },
            "configuration": {"acceptedOutputModes": ["text/plain"]},
        },
    }


def _subscribe_payload(task_id: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "SubscribeToTask",
        "params": {"id": task_id},
    }


class _Scenario:
    def __init__(self, *, run_dir: Path, server: _FixtureServer, scenario: str, mode: str, timeout: float) -> None:
        self.run_dir = run_dir
        self.server = server
        self.scenario = scenario
        self.mode = mode
        self.timeout = timeout
        self.control_dir = run_dir / "control"
        self.workspace = run_dir / "workspace"
        self.timeline: list[dict[str, Any]] = []
        self.controls: list[dict[str, Any]] = []
        self.task_id = ""
        self.context_id = ""
        self.execution_id = ""
        self.server_instance_id = ""
        self.pause_id = ""
        self.streams: list[_BackgroundStream] = []
        self._subscription_count = 0

    def run(self) -> dict[str, Any]:
        self._verify_agent_card()
        if self.scenario in REGRESSION_SCENARIOS:
            self._run_regression()
            self._verify_state_timeline()
            return self._summary("passed")
        initial = self._stream(
            _message_payload(self.workspace),
            name="initial",
        )
        initial.start()
        boundary = (
            "provider.awaiting_message_end" if self.scenario == "natural-completion-while-pausing" else "tool.started"
        )
        self._wait_log_event(
            self.run_dir / ("provider-lifecycle.jsonl" if boundary.startswith("provider") else "tool-lifecycle.jsonl"),
            boundary,
        )
        self.context_id = str(
            _wait_until(lambda: _first(initial.snapshot(), "contextId"), timeout=self.timeout, description="context id")
        )
        state = self._state()
        self.task_id = str(state["taskId"])
        self.execution_id = str(state["executionId"])
        self.server_instance_id = str(state["serverInstanceId"])
        self._assert_task_not_input_required()
        initial.close()

        dispatch = {
            "warm-resume-pausing": self._warm_resume_pausing,
            "warm-resume-paused": self._warm_resume_paused,
            "disconnect-timeout-after-operation-id": self._timeout_after_operation,
            "disconnect-timeout-inflight-sync-call": self._timeout_inflight_sync,
            "disconnect-timeout-backup-blocked": self._timeout_backup_blocked,
            "natural-completion-while-pausing": self._natural_completion,
            "stack-instances-timeout-after-operation-id": self._stack_instances_termination,
            "stack-instances-terminate-inflight": self._stack_instances_termination,
        }
        dispatch[self.scenario]()
        self._verify_state_timeline()
        return self._summary("passed")

    def _run_regression(self) -> None:
        initial = self._stream(_message_payload(self.workspace), name="initial")
        initial.start()
        boundary = {
            "slow-termination-storage": "permission.requested",
            "slow-rollover-storage": "provider.completed",
            "terminate-during-bootstrap": "bootstrap.started",
            "terminate-during-bootstrap-disconnected": "bootstrap.started",
            "disconnect-timeout-during-turn-backup": "turn-backup.started",
            "legacy-cancel-idle": "publication.blocked",
            "recovery-during-normal-rollover": "provider.completed",
        }[self.scenario]
        self._wait_log_event(
            self.run_dir
            / (
                "tool-lifecycle.jsonl"
                if boundary.startswith("permission")
                else "provider-lifecycle.jsonl"
                if boundary.startswith("provider")
                else "fixture-lifecycle.jsonl"
            ),
            boundary,
        )
        self.context_id = str(
            _wait_until(
                lambda: _first(initial.snapshot(), "contextId"),
                timeout=self.timeout,
                description="primary context id",
            )
        )
        state = self._state()
        self.task_id, self.execution_id = str(state["taskId"]), str(state["executionId"])
        self.server_instance_id = str(state["serverInstanceId"])
        if self.scenario.startswith("terminate-during-bootstrap"):
            if self.scenario.endswith("-disconnected"):
                initial.close()
            self._terminate_during_bootstrap()
            initial.join(min(self.timeout, 3.0))
        elif self.scenario == "disconnect-timeout-during-turn-backup":
            self._terminate_during_turn_backup(initial)
        elif self.scenario == "legacy-cancel-idle":
            self._legacy_cancel_idle(initial)
        else:
            initial.join(self.timeout)
            self._wait_state(lambda value: not value["streamAvailable"], "first normal turn finished")
            assert "input_required" in str(self._task()["result"]["status"]["state"]).lower()
            if self.scenario == "slow-termination-storage":
                self._slow_termination_storage()
            elif self.scenario == "recovery-during-normal-rollover":
                self._recovery_during_rollover()
            else:
                self._slow_rollover_storage()

    def _explicit_terminate(self) -> None:
        self._control(
            "/iac-code/execution/terminate",
            {
                "contextId": self.context_id,
                "expectedExecutionId": self.execution_id,
                "requestId": "explicit-termination",
                "connectionEpoch": 1,
                "reason": "explicit_terminate",
            },
        )

    def _assert_other_context_progress(self) -> None:
        assert not (self.control_dir / "release-storage").exists()
        status, health = _http_json("GET", self.server.url + "/health", timeout=1.0)
        assert status == 200 and health.get("status") == "healthy"
        primary = self._state()
        _write_marker(self.control_dir, "other-context")
        other = self._stream(_message_payload(self.workspace), name="other-context")
        started = time.monotonic()
        other.start()
        other.join(min(self.timeout, 2.0))
        other_context = _first(other.snapshot(), "contextId")
        assert other_context and other_context != self.context_id
        assert "ISOLATION_FIXTURE_FINAL" in json.dumps(other.snapshot())
        status, other_state = _http_json(
            "GET",
            self.server.url + "/iac-code/execution/state?" + urlencode({"contextId": other_context}),
            timeout=1.0,
        )
        assert status == 200 and other_state["phase"] == "running"
        assert not (self.control_dir / "release-storage").exists()
        lifecycle = _read_jsonl(self.run_dir / "fixture-lifecycle.jsonl")
        assert not any(value["event"].endswith("commit_finished") for value in lifecycle)
        _atomic_json(
            self.run_dir / "isolation-audit.json",
            {
                "primaryContextId": self.context_id,
                "otherContextId": other_context,
                "primaryPhase": primary["phase"],
                "otherCompletedBeforeStorageRelease": True,
                "otherElapsedSeconds": time.monotonic() - started,
            },
        )

    def _slow_termination_storage(self) -> None:
        _atomic_json(self.control_dir / "arm-termination-storage", {"contextId": self.context_id})
        self._explicit_terminate()
        write = self._wait_log_event(self.run_dir / "fixture-lifecycle.jsonl", "termination.commit_started")
        assert write["onEventLoop"] is False
        state = self._state()
        assert state["phase"] == "terminating" and not state["releaseReady"]
        self._assert_other_context_progress()
        _write_marker(self.control_dir, "release-storage")
        terminal = self._wait_state(lambda value: value["releaseReady"], "slow termination committed")
        self._assert_shared_backup(terminal, None, require_reason=False)
        self._assert_task_canceled(require_reason=False)
        assert not any(value["event"] == "tool.started" for value in self._tool_events())

    def _terminate_during_bootstrap(self) -> None:
        self._explicit_terminate()
        for boundary, marker in (
            ("bootstrap.started", "release-bootstrap"),
            ("bootstrap.cleanup_started", "release-bootstrap-cleanup"),
        ):
            self._wait_log_event(self.run_dir / "fixture-lifecycle.jsonl", boundary)
            state = self._state()
            assert state["phase"] == "terminating" and not state["releaseReady"]
            assert state["backup"]["status"] != "disabled"
            assert not (self.control_dir / marker).exists()
            _write_marker(self.control_dir, marker)
        self._wait_log_event(self.run_dir / "fixture-lifecycle.jsonl", "bootstrap.closed")
        local = self._wait_state(lambda value: value["phase"] == "terminated", "bootstrap local termination")
        assert not local["releaseReady"] and local["backup"]["status"] != "disabled"
        _write_marker(self.control_dir, "allow-shared")
        terminal = self._wait_state(lambda value: value["releaseReady"], "bootstrap shared backup")
        self._assert_shared_backup(terminal, None, require_reason=False)
        self._assert_task_canceled(require_reason=False)
        snapshots = list((self.run_dir / "shared-backup").rglob("a2a/task.json"))
        assert len(snapshots) == 1
        assert json.loads(snapshots[0].read_text(encoding="utf-8"))["state"] == "canceled"
        assert not self._provider_calls(), "bootstrap cancellation must not start an LLM request"

    def _terminate_during_turn_backup(self, initial: _BackgroundStream) -> None:
        _wait_until(
            lambda: snapshot
            if "ISOLATION_FIXTURE_FINAL" in json.dumps(snapshot := initial.snapshot())
            else None,
            timeout=self.timeout,
            description="final stream event before turn backup disconnect",
        )
        initial.close()
        self._pause(epoch=1, request_id="pause-turn-backup", timeout=5.0)
        state = self._wait_state(lambda value: value["phase"] == "terminating", "timeout during normal turn backup")
        assert not state["releaseReady"]
        status, health = _http_json("GET", self.server.url + "/health", timeout=1.0)
        assert status == 200 and health.get("status") == "healthy"
        _write_marker(self.control_dir, "release-turn-backup")
        terminal = self._wait_state(lambda value: value["releaseReady"], "completed turn final backup")
        assert terminal["executionStatus"] == "input-required"
        assert "input_required" in str(self._task()["result"]["status"]["state"]).lower()
        self._assert_shared_backup(terminal, None)
        snapshot = next((self.run_dir / "shared-backup").rglob("a2a/task.json"))
        assert json.loads(snapshot.read_text(encoding="utf-8"))["state"] == "input-required"
        recovery = self._capture_recovery("completed-turn")
        assert recovery["outputText"] == ["ISOLATION_FIXTURE_FINAL"]
        assert json.dumps(recovery["messages"]).count("ISOLATION_FIXTURE_FINAL") == 1
        assert len(self._provider_calls()) == 1

    def _legacy_cancel_idle(self, initial: _BackgroundStream) -> None:
        _wait_until(
            lambda: "LEGACY_CANCEL_FIRST_TOKEN" in json.dumps(initial.snapshot()),
            timeout=self.timeout,
            description="first pipeline text received before legacy cancel",
        )
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "CancelTask", "params": {"id": self.task_id}}
        _append_jsonl(self.run_dir / "requests.jsonl", {"name": "legacy-cancel", "payload": payload, "at": time.time()})
        status, result = _http_json("POST", self.server.url + "/", payload)
        assert status == 200 and "result" in result, result
        assert "cancel" in str(result["result"]["status"]["state"]).lower()
        initial.join(self.timeout)
        self._wait_log_event(self.run_dir / "provider-lifecycle.jsonl", "provider.closed")
        assert not self.controls, "legacy compatibility must not call execution control commands"
        self.server.wait_for_idle_exit()
        assert "a2a_execution_activity_ids" not in (self.run_dir / "server.log").read_text(encoding="utf-8")
        assert any(value["event"] == "idle.shutdown" for value in _read_jsonl(self.run_dir / "fixture-lifecycle.jsonl"))
        assert not (self.control_dir / "release-publication").exists()
        _atomic_json(self.run_dir / "task-final.json", result)

    def _slow_rollover_storage(self) -> None:
        self._wait_log_event(self.run_dir / "provider-lifecycle.jsonl", "background.started")
        before = self._state()
        _atomic_json(self.run_dir / "execution-state-before-rollover.json", self.timeline)
        self.timeline = []
        _write_marker(self.control_dir, "arm-rollover")
        payload = _message_payload(self.workspace)
        payload["params"]["message"]["contextId"] = self.context_id
        second = self._stream(payload, name="second-turn")
        second.start()
        self._wait_log_event(self.run_dir / "fixture-lifecycle.jsonl", "rollover.commit_started")
        state = self._state()
        assert state["executionId"] != before["executionId"]
        self.task_id, self.execution_id = str(state["taskId"]), str(state["executionId"])
        assert not second.done.is_set()
        self._assert_other_context_progress()
        assert not any(value["event"] == "background.finished" for value in self._provider_events())
        _write_marker(self.control_dir, "release-storage")
        second.join(self.timeout)
        assert "ISOLATION_FIXTURE_FINAL" in json.dumps(second.snapshot())
        self._wait_state(lambda value: not value["streamAvailable"], "second normal turn finished")
        assert not self.controls, "normal rollover must not call execution control commands"
        _write_marker(self.control_dir, "release-background")

    def _recovery_during_rollover(self) -> None:
        self._wait_log_event(self.run_dir / "provider-lifecycle.jsonl", "background.started")
        old_execution_id, old_task_id = self.execution_id, self.task_id
        query = urlencode({"contextId": self.context_id, "executionId": old_execution_id})
        _atomic_json(self.run_dir / "execution-state-before-rollover.json", self.timeline)
        _write_marker(self.control_dir, "arm-recovery")
        with ThreadPoolExecutor(max_workers=1) as pool:
            recovery_request = pool.submit(
                _http_json,
                "GET",
                self.server.url + "/iac-code/session/recovery?" + query,
                timeout=self.timeout,
            )
            try:
                self._wait_log_event(self.run_dir / "fixture-lifecycle.jsonl", "recovery.read_started")
                assert not recovery_request.done()
                payload = _message_payload(self.workspace)
                payload["params"]["message"]["contextId"] = self.context_id
                second = self._stream(payload, name="second-turn")
                second.start()
                second.join(self.timeout)
                assert "RECOVERY_ROLLOVER_TURN_2" in json.dumps(second.snapshot())
                rollover = self._wait_log_event(self.run_dir / "fixture-lifecycle.jsonl", "recovery.rollover_completed")
                assert rollover["oldExecutionId"] == old_execution_id
                self.timeline = []
                state = self._state()
                self.execution_id, self.task_id = str(state["executionId"]), str(state["taskId"])
                assert self.execution_id == rollover["executionId"] != old_execution_id
                assert self.task_id == rollover["taskId"] != old_task_id
                assert not recovery_request.done()
                assert not any(value["event"] == "background.finished" for value in self._provider_events())
                _write_marker(self.control_dir, "release-recovery")
                status, response = recovery_request.result(timeout=min(self.timeout, 3.0))
                _atomic_json(self.run_dir / "recovery-conflict.json", {"status": status, "response": response})
                assert status == 409, response
            finally:
                _write_marker(self.control_dir, "release-recovery")
        self._wait_state(lambda value: not value["streamAvailable"], "new turn finished after recovery conflict")
        recovery = self._capture_recovery("after-rollover")
        assert recovery["executionId"] == recovery["executionControl"]["executionId"] == self.execution_id
        assert recovery["taskId"] == recovery["task"]["id"] == recovery["executionControl"]["taskId"] == self.task_id
        assert recovery["outputText"] == ["RECOVERY_ROLLOVER_TURN_2"]
        transcript = json.dumps(recovery["messages"])
        assert transcript.count("RECOVERY_ROLLOVER_TURN_1") == 1
        assert transcript.count("RECOVERY_ROLLOVER_TURN_2") == 1

        def new_turn_shared() -> bool:
            return any(
                value.get("task_id") == self.task_id and value.get("state") == "input-required"
                for path in (self.run_dir / "shared-backup").rglob("a2a/task.json")
                if isinstance(value := json.loads(path.read_text(encoding="utf-8")), dict)
            )

        _wait_until(new_turn_shared, timeout=self.timeout, description="new turn shared backup")
        assert len(self._provider_calls()) == 2
        assert not self.controls, "recovery rollover must use normal A2A requests"
        _write_marker(self.control_dir, "release-background")
        self._wait_log_event(self.run_dir / "provider-lifecycle.jsonl", "background.finished")

    def _provider_events(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.run_dir / "provider-lifecycle.jsonl")

    def _verify_agent_card(self) -> None:
        status, card = _http_json("GET", self.server.url + "/.well-known/agent-card.json")
        assert status == 200, card
        serialized = json.dumps(card, sort_keys=True)
        for value in (
            "urn:iac-code:a2a:execution-control:v1",
            "/iac-code/execution/pause",
            "/iac-code/execution/state",
            "/iac-code/execution/resume",
            "/iac-code/execution/terminate",
            "/iac-code/session/recovery",
        ):
            assert value in serialized, "agent card is missing {}".format(value)
        _atomic_json(self.run_dir / "agent-card.json", card)

    def _state(self) -> dict[str, Any]:
        self.server.assert_running()
        query = urlencode({"contextId": self.context_id})
        status, state = _http_json("GET", self.server.url + "/iac-code/execution/state?" + query)
        assert status == 200, state
        self.timeline.append({"observedAt": time.time(), **state})
        _atomic_json(self.run_dir / "execution-state-timeline.json", self.timeline)
        return state

    def _wait_state(self, predicate: Callable[[dict[str, Any]], bool], description: str) -> dict[str, Any]:
        return _wait_until(
            lambda: state if predicate(state := self._state()) else None,
            timeout=self.timeout,
            description=description,
        )

    def _control(self, endpoint: str, payload: dict[str, Any], expected: set[int] = {200, 202}) -> dict[str, Any]:
        started = time.monotonic()
        status, body = _http_json("POST", self.server.url + endpoint, payload, timeout=2.0)
        elapsed = time.monotonic() - started
        record = {
            "endpoint": endpoint,
            "payload": payload,
            "status": status,
            "elapsedSeconds": elapsed,
            "response": body,
        }
        self.controls.append(record)
        _atomic_json(self.run_dir / "control-requests.json", self.controls)
        assert elapsed < 2.0, "{} waited {:.3f}s".format(endpoint, elapsed)
        assert status in expected, record
        return body

    def _pause(self, *, epoch: int, request_id: str, timeout: float) -> dict[str, Any]:
        payload = {
            "contextId": self.context_id,
            "taskId": self.task_id,
            "expectedExecutionId": self.execution_id,
            "requestId": request_id,
            "connectionEpoch": epoch,
            "reason": "client_disconnected",
            "reconnectTimeoutSeconds": timeout,
        }
        response = self._control("/iac-code/execution/pause", payload)
        duplicate = self._control("/iac-code/execution/pause", payload)
        self._assert_same_operation(response, duplicate)
        self.pause_id = str(response["pauseId"])
        self._assert_task_not_input_required()
        before = self._state()
        stale_payload = {**payload, "requestId": "{}-stale".format(request_id), "connectionEpoch": epoch - 1}
        self._control("/iac-code/execution/pause", stale_payload, expected={409})
        after = self._state()
        assert (after["revision"], after["phase"]) == (before["revision"], before["phase"])
        return response

    def _resume(self, *, epoch: int, request_id: str) -> dict[str, Any]:
        payload = {
            "contextId": self.context_id,
            "executionId": self.execution_id,
            "pauseId": self.pause_id,
            "requestId": request_id,
            "connectionEpoch": epoch,
        }
        response = self._control(
            "/iac-code/execution/resume",
            payload,
        )
        duplicate = self._control("/iac-code/execution/resume", payload)
        self._assert_same_operation(response, duplicate)
        return response

    def _terminate(self, *, epoch: int, request_id: str) -> dict[str, Any]:
        payload = {
            "contextId": self.context_id,
            "expectedExecutionId": self.execution_id,
            "pauseId": self.pause_id,
            "requestId": request_id,
            "connectionEpoch": epoch,
            "reason": "disconnect_timeout",
        }
        response = self._control(
            "/iac-code/execution/terminate",
            payload,
        )
        duplicate = self._control("/iac-code/execution/terminate", payload)
        self._assert_same_operation(response, duplicate)
        return response

    def _subscribe(self) -> _BackgroundStream:
        self._subscription_count += 1
        stream = self._stream(
            _subscribe_payload(self.task_id),
            name="subscribe-{}".format(self._subscription_count),
        )
        stream.start()
        return stream

    def _stream(self, payload: dict[str, Any], *, name: str) -> _BackgroundStream:
        _append_jsonl(self.run_dir / "requests.jsonl", {"name": name, "payload": payload, "at": time.time()})
        stream = _BackgroundStream(
            self.server.url,
            payload,
            timeout=max(self.timeout, 60.0),
            name=name,
            artifact_path=self.run_dir / "{}.events.jsonl".format(name),
        )
        self.streams.append(stream)
        return stream

    def close_streams(self) -> None:
        errors: list[str] = []
        for stream in self.streams:
            try:
                stream.close()
            except BaseException as exc:
                errors.append("{}: {}".format(stream.name, exc))
        if errors:
            raise RuntimeError("stream cleanup failed: {}".format("; ".join(errors)))

    def write_audits(self) -> None:
        _atomic_json(self.run_dir / "execution-state-timeline.json", self.timeline)
        _atomic_json(self.run_dir / "control-requests.json", self.controls)
        for path in (
            self.run_dir / "requests.jsonl",
            self.run_dir / "provider-lifecycle.jsonl",
            self.run_dir / "tool-lifecycle.jsonl",
        ):
            path.touch(exist_ok=True)
        _atomic_json(
            self.run_dir / "stream-summaries.json",
            [
                {
                    "name": stream.name,
                    "artifact": stream.artifact_path.name,
                    "eventCount": len(stream.snapshot()),
                    "done": stream.done.is_set(),
                    "error": None
                    if stream.error is None
                    else "{}: {}".format(type(stream.error).__name__, stream.error),
                }
                for stream in self.streams
            ],
        )
        observations = [
            {
                "observedAt": state.get("observedAt"),
                "phase": state.get("phase"),
                "releaseReady": state.get("releaseReady"),
                "backup": state.get("backup"),
            }
            for state in self.timeline
        ]
        shared_root = self.run_dir / "shared-backup"
        shared_files = sorted(str(path.relative_to(shared_root)) for path in shared_root.rglob("*") if path.is_file())
        _atomic_json(
            self.run_dir / "backup-audit.json",
            {
                "observations": observations,
                "final": observations[-1] if observations else None,
                "sharedFiles": shared_files,
            },
        )

    def _warm_resume_pausing(self) -> None:
        pause = self._pause(epoch=1, request_id="pause-pausing", timeout=4.0)
        assert pause["phase"] == "pausing" and pause["pauseComplete"] is False
        assert any(blocker.get("kind") == "tool" for blocker in pause.get("blockers", []))
        subscription = self._subscribe()
        self._resume(epoch=2, request_id="resume-pausing")
        self._wait_state(lambda value: value["phase"] == "running", "running after pausing resume")
        time.sleep(4.25)
        assert self._state()["phase"] == "running"
        _write_marker(self.control_dir, "release-tool")
        subscription.join(self.timeout)
        self._assert_normal_completion(subscription.snapshot())

    def _warm_resume_paused(self) -> None:
        self._pause(epoch=1, request_id="pause-until-paused", timeout=10.0)
        _write_marker(self.control_dir, "release-tool")
        paused = self._wait_state(
            lambda value: value["phase"] == "paused" and value["pauseComplete"] is True,
            "fully paused execution",
        )
        assert not paused.get("blockers")
        calls_before = len(self._provider_calls())
        time.sleep(0.5)
        assert len(self._provider_calls()) == calls_before
        paused_recovery = self._capture_recovery("paused")
        assert "DURABLE_FIXTURE_RESULT" in json.dumps(paused_recovery, ensure_ascii=False)
        subscription = self._subscribe()
        self._resume(epoch=2, request_id="resume-paused")
        self._wait_state(lambda value: value["phase"] == "running", "running after paused resume")
        subscription.join(self.timeout)
        self._assert_normal_completion(subscription.snapshot())

    def _timeout_after_operation(self) -> None:
        self._wait_log_event(self.run_dir / "tool-lifecycle.jsonl", "polling.started")
        self._pause(epoch=1, request_id="pause-operation", timeout=1.0)
        terminal = self._wait_state(
            lambda value: (
                value.get("terminationReason") == "disconnect_timeout"
                and value["phase"] == "terminated"
                and value.get("releaseReady") is True
            ),
            "deadline termination and shared backup",
        )
        self._assert_shared_backup(terminal, STACK_ID)
        self._assert_task_canceled()
        assert not (self.control_dir / "cloud-complete").exists()
        assert len([v for v in self._tool_events() if v.get("event") == "sdk.started"]) == 1
        assert len([v for v in self._tool_events() if v.get("event") == "sdk.returned"]) == 1
        assert len([v for v in self._tool_events() if v.get("event") == "polling.cancelled"]) == 1
        operation_files = list((self.run_dir / "shared-backup").rglob("external-operations.json"))
        assert len(operation_files) == 1
        assert (
            json.loads(operation_files[0].read_text(encoding="utf-8"))["operations"] == terminal["externalOperations"]
        )
        assert len(self._provider_calls()) == 1

    def _stack_instances_termination(self) -> None:
        inflight = self.scenario == "stack-instances-terminate-inflight"
        boundary = "sdk.started" if inflight else "polling.started"
        self._wait_log_event(self.run_dir / "tool-lifecycle.jsonl", boundary)
        if inflight:
            self._explicit_terminate()
            draining = self._wait_state(lambda value: value["phase"] == "terminating", "stack instances SDK draining")
            assert not draining["releaseReady"]
            assert not draining["externalOperations"]
            assert not any(value["event"] == "sdk.returned" for value in self._tool_events())
            status, health = _http_json("GET", self.server.url + "/health", timeout=1.0)
            assert status == 200 and health.get("status") == "healthy"
            _write_marker(self.control_dir, "release-sdk")
        else:
            self._pause(epoch=1, request_id="pause-stack-instances", timeout=1.0)
        local = self._wait_state(lambda value: value["phase"] == "terminated", "stack instances local termination")
        assert local["terminationReason"] == ("explicit_terminate" if inflight else "disconnect_timeout")
        assert not local["releaseReady"] and local["backup"]["status"] == "pending"
        assert not list((self.run_dir / "shared-backup").rglob("external-operations.json"))
        assert local["externalOperations"] == [
            {
                "product": "ros",
                "action": "CreateStackInstances",
                "outcome": "accepted",
                "resourceType": "stack-group-operation",
                "resourceId": STACK_INSTANCES_OPERATION_ID,
                "regionId": "cn-hangzhou",
                "toolUseId": "fixture-long-tool-1",
            }
        ]
        _write_marker(self.control_dir, "allow-shared")
        terminal = self._wait_state(lambda value: value["releaseReady"], "stack instances shared backup")
        self._assert_shared_backup(terminal, STACK_INSTANCES_OPERATION_ID, require_reason=not inflight)
        self._assert_task_canceled(require_reason=not inflight)
        operations = list((self.run_dir / "shared-backup").rglob("external-operations.json"))
        assert len(operations) == 1
        assert json.loads(operations[0].read_text(encoding="utf-8"))["operations"] == terminal["externalOperations"]
        events = self._tool_events()
        assert len([value for value in events if value["event"] == "sdk.started"]) == 1
        assert len([value for value in events if value["event"] == "sdk.returned"]) == 1
        assert len([value for value in events if value["event"] == "tool.cancelled"]) == 1
        assert len([value for value in events if value["event"] == "polling.started"]) == (0 if inflight else 1)
        assert len(self._provider_calls()) == 1
        self._capture_recovery("terminated")

    def _timeout_inflight_sync(self) -> None:
        self._wait_log_event(self.run_dir / "tool-lifecycle.jsonl", "sdk.started")
        self._pause(epoch=1, request_id="pause-sync", timeout=1.0)
        draining = self._wait_state(lambda value: value["phase"] == "terminating", "sync call draining")
        assert draining.get("releaseReady") is False
        status, health = _http_json("GET", self.server.url + "/health", timeout=1.0)
        assert status == 200 and health.get("status") == "healthy"
        assert not any(operation.get("resourceId") == STACK_ID for operation in draining.get("externalOperations", []))
        _write_marker(self.control_dir, "release-sdk")
        terminal = self._wait_state(
            lambda value: value["phase"] == "terminated" and value.get("releaseReady") is True,
            "sync late-result termination",
        )
        self._assert_shared_backup(terminal, STACK_ID)
        assert len([value for value in self._tool_events() if value.get("event") == "sdk.started"]) == 1
        assert len([value for value in self._tool_events() if value.get("event") == "operation.recorded"]) == 1
        assert len(self._provider_calls()) == 1

    def _timeout_backup_blocked(self) -> None:
        self._pause(epoch=1, request_id="pause-backup-blocked", timeout=1.0)
        blocked = self._wait_state(
            lambda value: (
                value["phase"] == "terminated"
                and value.get("backup", {}).get("status") == "blocked"
                and value.get("releaseReady") is False
            ),
            "blocked shared backup",
        )
        generation = blocked["backup"]["generation"]
        commit_id = blocked["backup"]["commitId"]
        status, health = _http_json("GET", self.server.url + "/health", timeout=1.0)
        assert status == 200 and health.get("status") == "healthy"
        counts_before = self._counts()
        _write_marker(self.control_dir, "allow-shared")
        self._terminate(epoch=2, request_id="retry-backup-finalization")
        terminal = self._wait_state(
            lambda value: (
                value["phase"] == "terminated"
                and value.get("backup", {}).get("status") == "shared_committed"
                and value.get("releaseReady") is True
            ),
            "shared backup retry",
        )
        assert terminal["backup"]["generation"] == generation
        assert terminal["backup"]["commitId"] == commit_id
        assert self._counts() == counts_before
        self._assert_shared_backup(terminal, None)

    def _natural_completion(self) -> None:
        self._pause(epoch=1, request_id="pause-natural", timeout=10.0)
        _write_marker(self.control_dir, "release-provider")
        completed = self._wait_state(
            lambda value: (
                value.get("executionStatus") not in {None, "working"}
                and value.get("executionStatus") != "canceled"
                and value.get("streamAvailable") is False
            ),
            "natural completion while pausing",
        )
        assert completed.get("terminationReason") is None
        subscription = self._subscribe()
        subscription.join(min(self.timeout, 3.0))
        recovery = self._capture_recovery("natural-completion")
        transcript = json.dumps(recovery["messages"], ensure_ascii=False)
        assert transcript.count("NATURAL_FIXTURE_FINAL") == 1
        assert recovery["outputText"] == ["NATURAL_FIXTURE_FINAL"]
        self._resume(epoch=2, request_id="resume-natural")
        final = self._wait_state(lambda value: value["phase"] == "running", "natural completion hold release")
        assert final.get("executionStatus") == completed.get("executionStatus")
        assert final.get("streamAvailable") is False
        assert len(self._provider_calls()) == 1

    def _capture_recovery(self, suffix: str) -> dict[str, Any]:
        if self.mode == "normal":
            query = urlencode({"contextId": self.context_id, "executionId": self.execution_id})
            status, recovery = _http_json("GET", self.server.url + "/iac-code/session/recovery?" + query)
        else:
            query = urlencode({"contextId": self.context_id})
            status, recovery = _http_json("GET", self.server.url + "/iac-code/pipeline/state?" + query)
        assert status == 200, recovery
        _atomic_json(self.run_dir / "recovery-{}.json".format(suffix), recovery)
        return recovery

    def _assert_normal_completion(self, events: list[dict[str, Any]]) -> None:
        assert events, "reconnected stream did not receive any events"
        if self.mode == "pipeline":
            task = self._wait_task_terminal()
        else:
            self._wait_state(
                lambda value: (
                    value.get("streamAvailable") is False
                    and value.get("executionStatus") not in {None, "working", "canceled"}
                ),
                "completed normal turn",
            )
            task = self._task()
        state = self._state()
        assert state["phase"] == "running"
        assert state.get("terminationReason") is None
        assert state.get("backup", {}).get("status") == "not_requested"
        assert state["executionId"] == self.execution_id
        assert state["serverInstanceId"] == self.server_instance_id
        assert len([value for value in self._tool_events() if value.get("event") == "tool.started"]) == 1
        assert len(self._provider_calls()) == 2
        recovery = self._capture_recovery("completed")
        serialized = json.dumps([task, events, recovery], ensure_ascii=False)
        if self.mode == "normal":
            assert "EXECUTION_CONTROL_FIXTURE_FINAL" in serialized
        else:
            assert "completed" in serialized
        assert "DURABLE_FIXTURE_RESULT" in serialized
        if self.mode == "pipeline":
            pipeline_events = self._pipeline_events()
            sequences = [value["sequence"] for value in pipeline_events]
            assert sequences == list(range(sequences[0], sequences[-1] + 1))
            assert len(sequences) == len(set(sequences))
            assert len([value for value in pipeline_events if value.get("eventType") == "step_completed"]) == 1
            projects = self.run_dir / "config" / "projects"
            assert list(projects.rglob("pipeline/meta.yaml"))
            assert list(projects.rglob("pipeline/context.yaml"))
            assert list(projects.rglob("a2a/pipeline/a2a-events.jsonl"))
            assert list(projects.rglob("a2a/pipeline/a2a-snapshot.json"))

    def _assert_task_not_input_required(self) -> None:
        status = self._task()["result"]["status"]["state"]
        assert "input_required" not in str(status).lower()

    def _assert_task_canceled(self, *, require_reason: bool = True) -> None:
        task = self._wait_task_terminal()
        status = str(task["result"]["status"]["state"]).lower()
        assert "cancel" in status, task
        if require_reason:
            assert "disconnect_timeout" in json.dumps(task, ensure_ascii=False)
        if self.mode == "pipeline":
            assert not any(value.get("eventType") == "pipeline_handoff_ready" for value in self._pipeline_events())

    def _task(self) -> dict[str, Any]:
        status, body = _http_json(
            "POST",
            self.server.url + "/",
            {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "GetTask", "params": {"id": self.task_id}},
        )
        assert status == 200 and "result" in body, body
        return body

    def _wait_task_terminal(self) -> dict[str, Any]:
        def terminal() -> dict[str, Any] | None:
            task = self._task()
            state = str(task["result"]["status"]["state"]).lower()
            return task if any(value in state for value in ("completed", "failed", "canceled", "cancelled")) else None

        task = _wait_until(terminal, timeout=self.timeout, description="terminal A2A task")
        _atomic_json(self.run_dir / "task-final.json", task)
        return task

    def _assert_shared_backup(
        self,
        state: dict[str, Any],
        expected_operation: str | None,
        *,
        require_reason: bool = True,
    ) -> None:
        backup = state.get("backup", {})
        assert backup.get("status") == "shared_committed", backup
        assert backup.get("generation") and backup.get("commitId")
        shared_files = [path for path in (self.run_dir / "shared-backup").rglob("*") if path.is_file()]
        assert shared_files, "shared backup is empty"
        texts = []
        for path in shared_files:
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
        joined = "\n".join(texts)
        assert str(backup["commitId"]) in joined
        if require_reason:
            assert "disconnect_timeout" in joined
        if expected_operation is not None:
            assert any(
                operation.get("resourceId") == expected_operation for operation in state.get("externalOperations", [])
            )
            assert expected_operation in joined

    def _wait_log_event(self, path: Path, event: str) -> dict[str, Any]:
        def observed() -> dict[str, Any] | None:
            self.server.assert_running()
            return next((value for value in _read_jsonl(path) if value.get("event") == event), None)

        return _wait_until(
            observed,
            timeout=self.timeout,
            description=event,
        )

    def _provider_calls(self) -> list[dict[str, Any]]:
        return [
            value
            for value in _read_jsonl(self.run_dir / "provider-lifecycle.jsonl")
            if value.get("event") == "provider.called"
        ]

    def _tool_events(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.run_dir / "tool-lifecycle.jsonl")

    def _pipeline_events(self) -> list[dict[str, Any]]:
        projects = self.run_dir / "config" / "projects"
        events: list[dict[str, Any]] = []
        for path in projects.rglob("a2a/pipeline/a2a-events.jsonl"):
            events.extend(_read_jsonl(path))
        return events

    def _counts(self) -> dict[str, int]:
        return {
            "provider": len(self._provider_calls()),
            "tool_started": len([value for value in self._tool_events() if value.get("event") == "tool.started"]),
            "pipeline_step_completed": len(
                [value for value in self._pipeline_events() if value.get("eventType") == "step_completed"]
            ),
        }

    def _assert_same_operation(self, first: dict[str, Any], duplicate: dict[str, Any]) -> None:
        assert first["contextId"] == duplicate["contextId"] == self.context_id
        assert first["taskId"] == duplicate["taskId"] == self.task_id
        assert first["executionId"] == duplicate["executionId"] == self.execution_id
        assert first["revision"] == duplicate["revision"]
        assert first["connectionEpoch"] == duplicate["connectionEpoch"]

    def _verify_state_timeline(self) -> None:
        revisions = [value.get("revision") for value in self.timeline]
        persisted = [value.get("persistedRevision") for value in self.timeline]
        assert revisions == sorted(revisions)
        assert persisted == sorted(persisted)
        assert all(value.get("taskId") == self.task_id for value in self.timeline)
        assert all(value.get("contextId") == self.context_id for value in self.timeline)
        assert all(value.get("executionId") == self.execution_id for value in self.timeline)
        assert all(value.get("serverInstanceId") == self.server_instance_id for value in self.timeline)

    def _summary(self, status: str, error: str | None = None) -> dict[str, Any]:
        return {
            "status": status,
            "scenario": self.scenario,
            "mode": self.mode,
            "taskId": self.task_id or None,
            "contextId": self.context_id or None,
            "executionId": self.execution_id or None,
            "serverInstanceId": self.server_instance_id or None,
            "pauseId": self.pause_id or None,
            "providerCalls": len(self._provider_calls()),
            "toolEvents": len(self._tool_events()),
            "pipelineStepCompletions": len(
                [value for value in self._pipeline_events() if value.get("eventType") == "step_completed"]
            ),
            "controlRequests": len(self.controls),
            "stateObservations": len(self.timeline),
            "streams": [
                {"name": stream.name, "eventCount": len(stream.snapshot()), "done": stream.done.is_set()}
                for stream in self.streams
            ],
            "error": error,
        }


def _run_scenario(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    run_dir: Path,
    scenario_name: str,
) -> tuple[int, dict[str, Any]]:
    started = time.monotonic()
    run_dir.mkdir(parents=True)
    server = _FixtureServer(
        repo_root=repo_root,
        run_dir=run_dir,
        scenario=scenario_name,
        mode=args.mode,
        timeout=args.timeout,
    )
    scenario = _Scenario(
        run_dir=run_dir,
        server=server,
        scenario=scenario_name,
        mode=args.mode,
        timeout=args.timeout,
    )
    overall_expired = threading.Event()

    def expire() -> None:
        overall_expired.set()
        server.stop()

    overall_timer = threading.Timer(args.overall_timeout, expire)
    overall_timer.daemon = True
    overall_timer.start()
    exit_code = 0
    cleanup_errors: list[str] = []
    try:
        server.wait_ready()
        summary = scenario.run()
    except BaseException as exc:
        exit_code = 1
        error = (
            "TimeoutError: overall scenario timeout after {:.3f}s".format(args.overall_timeout)
            if overall_expired.is_set()
            else "{}: {}".format(type(exc).__name__, exc)
        )
        summary = scenario._summary("failed", error)
        (run_dir / "runner-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        overall_timer.cancel()
        for name, cleanup in (
            ("streams", scenario.close_streams),
            ("server", server.stop),
            ("audits", scenario.write_audits),
        ):
            try:
                cleanup()
            except BaseException as exc:
                cleanup_errors.append("{}: {}: {}".format(name, type(exc).__name__, exc))
    if cleanup_errors:
        exit_code = 1
        prior_error = summary.get("error")
        details = "cleanup failed: {}".format("; ".join(cleanup_errors))
        summary = scenario._summary("failed", "{}; {}".format(prior_error, details) if prior_error else details)
    summary["elapsedSeconds"] = round(time.monotonic() - started, 3)
    summary["server"] = server.lifecycle()
    _atomic_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code, summary


def main() -> int:
    args = _parse_args()
    if args.timeout <= 0 or args.overall_timeout <= 0:
        raise SystemExit("timeouts must be positive")
    scenario_names = list(args.scenario)
    for scenario_name in scenario_names:
        if args.mode not in SCENARIO_MODES[scenario_name]:
            raise SystemExit("{} does not support mode {}".format(scenario_name, args.mode))
    root_run_dir = args.run_dir.expanduser().resolve()
    if root_run_dir.exists():
        shutil.rmtree(root_run_dir)
    repo_root = Path(__file__).resolve().parents[4]
    if len(scenario_names) == 1:
        exit_code, _summary_payload = _run_scenario(
            args=args,
            repo_root=repo_root,
            run_dir=root_run_dir,
            scenario_name=scenario_names[0],
        )
        return exit_code

    root_run_dir.mkdir(parents=True)
    started = time.monotonic()
    summaries: list[dict[str, Any]] = []
    exit_code = 0
    for index, scenario_name in enumerate(scenario_names, start=1):
        child_name = "{:02d}-{}-{}".format(index, scenario_name, args.mode)
        child_exit_code, summary = _run_scenario(
            args=args,
            repo_root=repo_root,
            run_dir=root_run_dir / child_name,
            scenario_name=scenario_name,
        )
        summary["artifactDirectory"] = child_name
        summaries.append(summary)
        exit_code = max(exit_code, child_exit_code)
    aggregate = {
        "status": "passed" if exit_code == 0 else "failed",
        "mode": args.mode,
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "runs": summaries,
    }
    _atomic_json(root_run_dir / "summary.json", aggregate)
    print(json.dumps(aggregate, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
