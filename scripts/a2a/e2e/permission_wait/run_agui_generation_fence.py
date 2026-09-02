#!/usr/bin/env python3
"""Run the AG-UI to A2A staged-backup generation-fence scenario."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from run_permission_wait_restart import (
    _FixtureServer,
    _free_port,
    _permission_checkpoint_for_input,
    _single_json,
    _tool_execution_lines,
)

THREAD_ID = "thread-agui-generation-fence"
ROS_INVOCATION_ID = "ros-invocation-agui-generation-fence"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def _decode_sse_line(line: bytes) -> dict[str, Any] | None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text.startswith("data:"):
        return None
    try:
        value = json.loads(text[5:].strip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _agui_request(url: str, payload: dict[str, Any], *, timeout: float) -> list[dict[str, Any]]:
    request = Request(
        url.rstrip("/") + "/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    try:
        with urlopen(request, timeout=timeout) as response:
            for line in response:
                event = _decode_sse_line(line)
                if event is not None:
                    events.append(event)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError("AG-UI request failed with HTTP {}: {}".format(exc.code, body)) from exc
    if not events:
        raise AssertionError("AG-UI request returned no SSE events")
    return events


def _run_payload(
    workspace: Path,
    *,
    run_id: str,
    prompt: str | None = None,
    resume: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "threadId": THREAD_ID,
        "runId": run_id,
        "state": {},
        "messages": (
            []
            if resume is not None
            else [{"id": "message-{}".format(run_id), "role": "user", "content": prompt or ""}]
        ),
        "tools": [],
        "context": [],
        "forwardedProps": {
            "iacCode": {
                "schemaVersion": 1,
                "rosInvocationId": ROS_INVOCATION_ID,
                "cwd": str(workspace),
            }
        },
        **({"resume": resume} if resume is not None else {}),
    }


def _resolved_permission(interrupt_id: str) -> list[dict[str, Any]]:
    return [
        {
            "interruptId": interrupt_id,
            "status": "resolved",
            "payload": {"decision": "allow_once"},
        }
    ]


def _terminal_interrupt(events: list[dict[str, Any]]) -> str:
    terminal = events[-1]
    outcome = terminal.get("outcome")
    interrupts = outcome.get("interrupts") if isinstance(outcome, dict) else None
    if terminal.get("type") != "RUN_FINISHED" or not isinstance(interrupts, list) or len(interrupts) != 1:
        raise AssertionError("AG-UI run did not finish with exactly one interrupt")
    interrupt_id = interrupts[0].get("id") if isinstance(interrupts[0], dict) else None
    if not isinstance(interrupt_id, str) or not interrupt_id:
        raise AssertionError("AG-UI interrupt has no stable id")
    return interrupt_id


def _thread_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "threads" / "{}.json".format(THREAD_ID)
    if not path.is_file():
        raise AssertionError("AG-UI thread state was not persisted")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("AG-UI thread state is not an object")
    return value


def _pending_permission(state_dir: Path, interrupt_id: str) -> dict[str, Any]:
    state = _thread_state(state_dir)
    execution = state.get("execution")
    pending = execution.get("pending") if isinstance(execution, dict) else None
    entry = pending.get(interrupt_id) if isinstance(pending, dict) else None
    value = entry.get("value") if isinstance(entry, dict) else None
    if not isinstance(value, dict) or value.get("inputId") != interrupt_id:
        raise AssertionError("AG-UI durable state lost the pending permission")
    return value


class _AguiServer:
    def __init__(self, *, run_dir: Path, repo_root: Path, port: int, state_dir: Path, workspace: Path) -> None:
        self.run_dir = run_dir
        self.repo_root = repo_root
        self.port = port
        self.state_dir = state_dir
        self.workspace = workspace
        self.process: subprocess.Popen[str] | None = None
        self._stdout = None
        self._stderr = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:{}".format(self.port)

    def start(self, *, generation: int, a2a_url: str) -> None:
        self._stdout = (self.run_dir / "agui-{}.stdout.log".format(generation)).open("w", encoding="utf-8")
        self._stderr = (self.run_dir / "agui-{}.stderr.log".format(generation)).open("w", encoding="utf-8")
        env = os.environ.copy()
        src = str(self.repo_root / "src")
        env["PYTHONPATH"] = os.pathsep.join(part for part in (src, env.get("PYTHONPATH", "")) if part)
        env["IAC_CODE_AGUI_ALLOWED_CWDS"] = str(self.workspace)
        env["IAC_CODE_CONFIG_DIR"] = str(self.run_dir / "config")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "iac_code.cli.main",
                "agui",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--a2a-url",
                a2a_url,
                "--state-dir",
                str(self.state_dir),
                "--interrupt-ttl",
                "120",
            ],
            cwd=self.repo_root,
            env=env,
            stdout=self._stdout,
            stderr=self._stderr,
            text=True,
        )
        self._wait_healthy()

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError("AG-UI server exited with code {}".format(self.process.returncode))
            try:
                with urlopen(self.url + "/health", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (TimeoutError, URLError, OSError):
                time.sleep(0.05)
        raise TimeoutError("AG-UI server did not become healthy")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        for handle in (self._stdout, self._stderr):
            if handle is not None and not handle.closed:
                handle.close()
        self.process = None


def run_scenario(*, run_dir: Path, timeout: float) -> dict[str, Any]:
    from iac_code.services.session_backup import BACKUP_ENV_VAR, BACKUP_STATE_FILENAME, SessionBackupService
    from iac_code.services.session_backup_staging import SessionBackupStagingWorker
    from iac_code.services.session_storage import SessionStorage

    run_dir = run_dir.expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    workspace = run_dir / "workspace"
    workspace.mkdir()
    config_dir = run_dir / "config"
    persistence_dir = run_dir / "a2a-state"
    staging_dir = run_dir / "staging"
    backup_dir = run_dir / "shared-backup"
    agui_state_dir = run_dir / "agui-state"
    execution_log = run_dir / "tool-executions.log"
    repo_root = Path(__file__).resolve().parents[4]
    a2a = _FixtureServer(
        run_dir=run_dir,
        port=_free_port(),
        repo_root=repo_root,
        mode="normal",
        candidate_first=False,
        pipeline_step_id=None,
        handoff_first=False,
        sequential_permissions=True,
        defer_staging_publisher=True,
        staging_dir=staging_dir,
        backup_dir=backup_dir,
    )
    agui = _AguiServer(
        run_dir=run_dir,
        repo_root=repo_root,
        port=_free_port(),
        state_dir=agui_state_dir,
        workspace=workspace,
    )
    try:
        a2a.start(1)
        agui.start(generation=1, a2a_url=a2a.url)
        first_events = _agui_request(
            agui.url,
            _run_payload(workspace, run_id="run-1", prompt="request two deterministic writes"),
            timeout=timeout,
        )
        permission_1_id = _terminal_interrupt(first_events)
        permission_1 = _pending_permission(agui_state_dir, permission_1_id)
        _context_path, context_snapshot = _single_json("contexts/*.json", persistence_dir)
        session_id = context_snapshot.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise AssertionError("A2A context snapshot lost its session id")

        worker = SessionBackupStagingWorker(staging_dir, backup_dir)
        if worker.run_once() != 1:
            raise AssertionError("P1 staged backup was not published as the old shared generation")
        shared_markers = sorted(backup_dir.rglob("{}/{}".format(session_id, BACKUP_STATE_FILENAME)))
        if len(shared_markers) != 1:
            raise AssertionError("shared P1 backup marker was not created")
        shared_marker = shared_markers[0]
        old_shared_generation = int(json.loads(shared_marker.read_text(encoding="utf-8"))["generation"])

        hidden_marker = shared_marker.with_name(".backup-state.agui-e2e-hidden.json")
        os.replace(shared_marker, hidden_marker)
        try:
            second_events = _agui_request(
                agui.url,
                _run_payload(
                    workspace,
                    run_id="run-2",
                    resume=_resolved_permission(permission_1_id),
                ),
                timeout=timeout,
            )
        finally:
            os.replace(hidden_marker, shared_marker)
        permission_2_id = _terminal_interrupt(second_events)
        if permission_2_id == permission_1_id:
            raise AssertionError("successor permission reused the P1 interrupt id")
        permission_2 = _pending_permission(agui_state_dir, permission_2_id)
        if permission_2.get("requestTaskId") != permission_1.get("requestTaskId"):
            raise AssertionError("sequential AG-UI permissions changed A2A task identity")
        if _tool_execution_lines(execution_log):
            raise AssertionError("sequential tools executed before both permissions were resolved")

        _task_path, task_snapshot = _single_json("tasks/*.json", persistence_dir)
        expected_generation = task_snapshot.get("expected_permission_backup_generation")
        if not isinstance(expected_generation, int) or expected_generation <= old_shared_generation:
            raise AssertionError("A2A Task snapshot did not advance to the P2 staged generation")
        public_before_restart = json.dumps(first_events + second_events, ensure_ascii=False)
        if (
            "expected_permission_backup_generation" in public_before_restart
            or "minimum_generation" in public_before_restart
        ):
            raise AssertionError("internal backup generation leaked into the public AG-UI stream")
        staged_snapshot = next(
            (path for path in staging_dir.rglob("{}_v{}".format(session_id, expected_generation)) if path.is_dir()),
            None,
        )
        if staged_snapshot is None:
            raise AssertionError("P2 staged snapshot is unavailable")

        agui.stop()
        a2a.stop()
        primary_sessions = [path for path in config_dir.rglob(session_id) if path.is_dir()]
        if len(primary_sessions) != 1:
            raise AssertionError("expected exactly one primary session before sandbox replacement")
        shutil.rmtree(primary_sessions[0])

        previous_backup_dir = os.environ.get(BACKUP_ENV_VAR)
        os.environ[BACKUP_ENV_VAR] = str(backup_dir)
        try:
            restore_result = SessionBackupService(
                session_storage=SessionStorage(config_dir / "projects")
            ).restore_session(str(workspace), session_id)
        finally:
            if previous_backup_dir is None:
                os.environ.pop(BACKUP_ENV_VAR, None)
            else:
                os.environ[BACKUP_ENV_VAR] = previous_backup_dir
        if not restore_result.restored:
            raise AssertionError("replacement Sandbox did not restore the old shared P1 backup")
        restored_old_state = json.loads(
            (Path(restore_result.destination) / BACKUP_STATE_FILENAME).read_text(encoding="utf-8")
        )
        if int(restored_old_state["generation"]) != old_shared_generation:
            raise AssertionError("replacement Sandbox did not start from the expected old generation")

        a2a.staging_dir = run_dir / "replacement-staging"
        a2a.start(2)
        agui.start(generation=2, a2a_url=a2a.url)
        not_ready_events = _agui_request(
            agui.url,
            _run_payload(
                workspace,
                run_id="run-3",
                resume=_resolved_permission(permission_2_id),
            ),
            timeout=timeout,
        )
        terminal_error = not_ready_events[-1]
        if terminal_error.get("type") != "RUN_ERROR" or terminal_error.get("code") != "SESSION_BACKUP_NOT_READY":
            raise AssertionError("cold AG-UI P2 Resume did not return SESSION_BACKUP_NOT_READY")
        if "Retry after 3 seconds" not in str(terminal_error.get("message")):
            raise AssertionError("AG-UI not-ready error did not tell the caller when to retry")
        _pending_permission(agui_state_dir, permission_2_id)
        if _tool_execution_lines(execution_log):
            raise AssertionError("not-ready AG-UI P2 Resume executed a tool")
        _unchanged_path, unchanged_p2 = _permission_checkpoint_for_input(staged_snapshot, permission_2_id)
        unchanged_decision = unchanged_p2.get("decision")
        if (
            unchanged_p2.get("phase") != "WAITING"
            or not isinstance(unchanged_decision, dict)
            or unchanged_decision.get("status") != "none"
            or unchanged_decision.get("value") is not None
        ):
            raise AssertionError("not-ready AG-UI P2 Resume claimed or consumed the staged checkpoint")

        if worker.run_once() < 1:
            raise AssertionError("P2 staged backup was not published to shared storage")
        shared_state = json.loads(shared_marker.read_text(encoding="utf-8"))
        if int(shared_state["generation"]) < expected_generation:
            raise AssertionError("shared backup did not reach the P2 generation")

        recovered_events = _agui_request(
            agui.url,
            _run_payload(
                workspace,
                run_id="run-4",
                resume=_resolved_permission(permission_2_id),
            ),
            timeout=timeout,
        )
        if recovered_events[-1].get("type") != "RUN_FINISHED" or recovered_events[-1].get("outcome") != {
            "type": "success"
        }:
            raise AssertionError("AG-UI P2 retry did not finish successfully")
        if _tool_execution_lines(execution_log) != ["executed", "executed-2"]:
            raise AssertionError("AG-UI P2 retry did not execute both sequential tools exactly once")

        primary_sessions = [path for path in config_dir.rglob(session_id) if path.is_dir()]
        if len(primary_sessions) != 1:
            raise AssertionError("shared P2 backup was not restored into the new sandbox")
        local_state = json.loads((primary_sessions[0] / BACKUP_STATE_FILENAME).read_text(encoding="utf-8"))
        if int(local_state["generation"]) < expected_generation:
            raise AssertionError("restored local session did not meet the Task generation fence")
        _p1_path, p1_checkpoint = _permission_checkpoint_for_input(primary_sessions[0], permission_1_id)
        _p2_path, p2_checkpoint = _permission_checkpoint_for_input(primary_sessions[0], permission_2_id)
        if p1_checkpoint.get("phase") != "RESOLVED" or p1_checkpoint.get("ack", {}).get(
            "nextBoundaryId"
        ) != p2_checkpoint.get("boundaryId"):
            raise AssertionError("restored session lost the P1 receipt to P2 successor link")
        if p2_checkpoint.get("phase") != "RESOLVED":
            raise AssertionError("AG-UI P2 retry did not resolve the restored checkpoint")

        public_payload = json.dumps(
            first_events + second_events + not_ready_events + recovered_events,
            ensure_ascii=False,
        )
        if "expected_permission_backup_generation" in public_payload or "minimum_generation" in public_payload:
            raise AssertionError("internal backup generation leaked into AG-UI events")
        return {
            "passed": True,
            "scenario": "agui-staged-backup-generation-fence",
            "threadId": THREAD_ID,
            "taskId": permission_2["requestTaskId"],
            "oldSharedGeneration": old_shared_generation,
            "expectedGeneration": expected_generation,
            "restoredGeneration": int(local_state["generation"]),
            "notReadyObserved": True,
            "aguiStateRestored": True,
            "p1ReceiptPreserved": True,
            "p2ResolvedOnce": True,
            "toolExecutions": 2,
            "publicGenerationAbsent": True,
        }
    finally:
        agui.stop()
        a2a.stop()


def main() -> int:
    args = _parse_args()
    result = run_scenario(run_dir=args.run_dir, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
