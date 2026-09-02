#!/usr/bin/env python3
"""Run the deterministic permission-wait process-restart A2A scenario."""

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
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PERMISSION_QUERY_PREFIX = "IAC_CODE_PERMISSION:"
SELLING_STAGE_IDS = (
    "solution_planning_and_selection",
    "materialize_selected_candidate",
    "deploying",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--decision", choices=("allow_once", "deny"), required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--mode", choices=("normal", "pipeline"), default="normal")
    parser.add_argument("--candidate-first", action="store_true")
    parser.add_argument("--pipeline-step-id", choices=SELLING_STAGE_IDS)
    parser.add_argument("--handoff-first", action="store_true")
    parser.add_argument("--staged-backup-generation-fence", action="store_true")
    return parser.parse_args()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _decode_response_line(line: bytes) -> dict[str, Any] | None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _stream_request(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    on_event: Any = None,
) -> list[dict[str, Any]]:
    request = Request(
        url.rstrip("/") + "/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    try:
        with urlopen(request, timeout=timeout) as response:
            for line in response:
                event = _decode_response_line(line)
                if event is not None:
                    events.append(event)
                    if on_event is not None:
                        on_event(event)
    except HTTPError as exc:
        body = exc.read()
        event = _decode_response_line(body)
        if event is not None:
            events.append(event)
        else:
            raise RuntimeError("A2A request failed with HTTP {}".format(exc.code)) from exc
    return events


class _BackgroundStream:
    def __init__(self, url: str, payload: dict[str, Any], *, timeout: float) -> None:
        self.url = url
        self.payload = payload
        self.timeout = timeout
        self.events: list[dict[str, Any]] = []
        self.error: BaseException | None = None
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="permission-wait-pipeline-stream", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.events)

    def wait_for_permission(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            inputs = [value for value in _iac_code_values(self.snapshot(), "input") if isinstance(value, dict)]
            permission = next((value for value in inputs if value.get("kind") == "permission"), None)
            if permission is not None:
                return permission
            if self.done.is_set():
                raise RuntimeError("pipeline stream ended before permission boundary") from self.error
            time.sleep(0.02)
        raise TimeoutError("timed out waiting for pipeline permission boundary")

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("pipeline stream did not close after server shutdown")

    def _run(self) -> None:
        def capture(event: dict[str, Any]) -> None:
            with self._lock:
                self.events.append(event)

        try:
            _stream_request(self.url, self.payload, timeout=self.timeout, on_event=capture)
        except BaseException as exc:
            self.error = exc
        finally:
            self.done.set()


def _message_payload(
    *,
    workspace: Path,
    prompt: str,
    context_id: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": prompt}],
        "metadata": {"iac_code": {"cwd": str(workspace)}},
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "SendStreamingMessage",
        "params": {
            "message": message,
            "configuration": {"acceptedOutputModes": ["text/plain"]},
        },
    }


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _iac_code_values(events: list[dict[str, Any]], key: str) -> list[Any]:
    values: list[Any] = []
    for event in events:
        for item in _walk_dicts(event):
            metadata = item.get("metadata")
            if not isinstance(metadata, dict):
                continue
            iac_code = metadata.get("iac_code")
            if isinstance(iac_code, dict) and key in iac_code:
                values.append(iac_code[key])
    return values


def _unique_permissions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for value in _iac_code_values(events, "input"):
        if not isinstance(value, dict) or value.get("kind") != "permission":
            continue
        input_id = value.get("inputId")
        if isinstance(input_id, str) and input_id:
            unique[input_id] = value
    return list(unique.values())


def _first_identifier(events: list[dict[str, Any]], key: str) -> str | None:
    for event in events:
        for item in _walk_dicts(event):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _validate_structured_permission(permission: dict[str, Any], *, scenario: str) -> None:
    serialized = json.dumps(permission, ensure_ascii=False)
    if "never-publish-this" in serialized:
        raise AssertionError("permission projection exposed a fixture secret")
    if not isinstance(permission.get("target"), str) or not permission["target"]:
        raise AssertionError("permission projection lost its target")
    options = permission.get("options")
    option_ids = (
        {value.get("id") for value in options if isinstance(value, dict)} if isinstance(options, list) else set()
    )
    if option_ids != {"allow_once", "deny"}:
        raise AssertionError("permission projection lost its stable decision options")
    if scenario == "solution_planning_and_selection":
        operation = permission.get("operation")
        if not isinstance(operation, dict) or operation.get("action") != "CreateVSwitch":
            raise AssertionError("Step 1 permission lost its Aliyun operation")
        calls = operation.get("apiCalls")
        if not isinstance(calls, list) or [value.get("action") for value in calls] != ["CreateVSwitch"]:
            raise AssertionError("Step 1 permission lost its API sequence")
        parameters = permission.get("displayParameters")
        if not isinstance(parameters, dict) or "Password" not in json.dumps(parameters, ensure_ascii=False):
            raise AssertionError("Step 1 permission lost the redacted parameter shape")
        if "permission-step1-vswitch" not in permission["target"]:
            raise AssertionError("Step 1 permission lost its resource target")
    elif scenario == "materialize_selected_candidate":
        if permission.get("toolName") != "write_file" or "permission-step2.yml" not in permission["target"]:
            raise AssertionError("Step 2 permission lost its template target")
    elif scenario == "deploying":
        operation = permission.get("operation")
        calls = operation.get("apiCalls") if isinstance(operation, dict) else None
        if permission.get("toolName") != "ros_deploy" or not isinstance(calls, list):
            raise AssertionError("Step 3 permission lost its deploy operation")
        if [value.get("action") for value in calls] != ["CreateStack"]:
            raise AssertionError("Step 3 permission lost its CreateStack sequence")
    elif scenario == "handoff":
        operation = permission.get("operation")
        calls = operation.get("apiCalls") if isinstance(operation, dict) else None
        if permission.get("toolName") != "ros_stack" or not isinstance(calls, list):
            raise AssertionError("normal handoff permission lost its ROS operation")
        if [value.get("action") for value in calls] != ["UpdateStack"]:
            raise AssertionError("normal handoff permission lost its UpdateStack sequence")


def _event_text(events: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(item["text"]) for event in events for item in _walk_dicts(event) if isinstance(item.get("text"), str)
    )


def _task_states(events: list[dict[str, Any]]) -> list[str]:
    states: list[str] = []
    for event in events:
        for item in _walk_dicts(event):
            status = item.get("status")
            state = status.get("state") if isinstance(status, dict) else None
            if isinstance(state, str):
                states.append(state)
    return states


def _checkpoint_path(config_dir: Path) -> Path:
    matches = sorted(config_dir.rglob("permission-waits/pwb_*.json"))
    if len(matches) != 1:
        raise AssertionError("expected exactly one permission checkpoint, found {}".format(len(matches)))
    return matches[0]


def _read_checkpoint(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("permission checkpoint is not an object")
    return value


def _permission_response_query(permission: dict[str, Any], decision: str = "allow_once") -> str:
    response = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": permission["requestTaskId"],
        "contextId": permission["contextId"],
        "inputId": permission["inputId"],
        "toolUseId": permission["toolUseId"],
        "decision": decision,
    }
    return PERMISSION_QUERY_PREFIX + " " + json.dumps(response, separators=(",", ":"))


def _single_json(path_pattern: str, root: Path) -> tuple[Path, dict[str, Any]]:
    paths = sorted(root.glob(path_pattern))
    if len(paths) != 1:
        raise AssertionError("expected exactly one {!r} under {}, found {}".format(path_pattern, root, len(paths)))
    value = json.loads(paths[0].read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("{} is not a JSON object".format(paths[0]))
    return paths[0], value


def _permission_checkpoint_for_input(root: Path, input_id: str) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in root.rglob("permission-waits/pwb_*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("inputId") == input_id:
            matches.append((path, value))
    if not matches:
        raise AssertionError("permission checkpoint {} was not found under {}".format(input_id, root))
    return sorted(matches, key=lambda item: str(item[0]))[-1]


def _tool_execution_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class _FixtureServer:
    def __init__(
        self,
        *,
        run_dir: Path,
        port: int,
        repo_root: Path,
        mode: str,
        candidate_first: bool,
        pipeline_step_id: str | None,
        handoff_first: bool,
        sequential_permissions: bool = False,
        defer_staging_publisher: bool = False,
        staging_dir: Path | None = None,
        backup_dir: Path | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.port = port
        self.repo_root = repo_root
        self.mode = mode
        self.candidate_first = candidate_first
        self.pipeline_step_id = pipeline_step_id
        self.handoff_first = handoff_first
        self.sequential_permissions = sequential_permissions
        self.defer_staging_publisher = defer_staging_publisher
        self.staging_dir = staging_dir
        self.backup_dir = backup_dir
        self.process: subprocess.Popen[str] | None = None
        self._stdout = None
        self._stderr = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:{}".format(self.port)

    def start(self, generation: int) -> None:
        fixture = self.repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "permission_wait_fixture_server.py"
        self._stdout = (self.run_dir / "server-{}.stdout.log".format(generation)).open("w", encoding="utf-8")
        self._stderr = (self.run_dir / "server-{}.stderr.log".format(generation)).open("w", encoding="utf-8")
        env = os.environ.copy()
        src = str(self.repo_root / "src")
        env["PYTHONPATH"] = os.pathsep.join(part for part in (src, env.get("PYTHONPATH", "")) if part)
        command = [
            sys.executable,
            str(fixture),
            "--port",
            str(self.port),
            "--config-dir",
            str(self.run_dir / "config"),
            "--persistence-dir",
            str(self.run_dir / "a2a-state"),
            "--artifact-dir",
            str(self.run_dir / "artifacts"),
            "--workspace",
            str(self.run_dir / "workspace"),
            "--execution-log",
            str(self.run_dir / "tool-executions.log"),
            "--mode",
            self.mode,
        ]
        if self.candidate_first:
            command.append("--candidate-first")
        if self.pipeline_step_id:
            command.extend(("--pipeline-step-id", self.pipeline_step_id))
        if self.handoff_first:
            command.append("--handoff-first")
        if self.sequential_permissions:
            command.append("--sequential-permissions")
        if self.defer_staging_publisher:
            command.append("--defer-staging-publisher")
        if self.staging_dir is not None and self.backup_dir is not None:
            command.extend(("--staging-dir", str(self.staging_dir), "--backup-dir", str(self.backup_dir)))
        self.process = subprocess.Popen(
            command,
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
                raise RuntimeError("fixture A2A server exited with code {}".format(self.process.returncode))
            try:
                with urlopen(self.url + "/health", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (TimeoutError, URLError, OSError):
                time.sleep(0.05)
        raise TimeoutError("fixture A2A server did not become healthy")

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


def _pipeline_journal_events(config_dir: Path) -> list[dict[str, Any]]:
    matches = sorted(config_dir.rglob("a2a/pipeline/a2a-events.jsonl"))
    if len(matches) != 1:
        raise AssertionError("expected exactly one Pipeline journal, found {}".format(len(matches)))
    events: list[dict[str, Any]] = []
    for line in matches[0].read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if isinstance(value, dict):
            events.append(value)
    return events


def _pipeline_snapshot_permission(config_dir: Path, input_id: str) -> dict[str, Any]:
    matches = sorted(config_dir.rglob("a2a/pipeline/a2a-snapshot.json"))
    if len(matches) != 1:
        raise AssertionError("expected exactly one Pipeline snapshot, found {}".format(len(matches)))
    snapshot = json.loads(matches[0].read_text(encoding="utf-8"))
    display = snapshot.get("display") if isinstance(snapshot, dict) else None
    permissions = display.get("permissions") if isinstance(display, dict) else None
    if not isinstance(permissions, list):
        raise AssertionError("Pipeline snapshot does not contain permission display state")
    permission = next(
        (value for value in permissions if isinstance(value, dict) and value.get("inputId") == input_id),
        None,
    )
    if permission is None:
        raise AssertionError("Pipeline snapshot lost the waiting permission")
    return permission


def run_staged_backup_generation_fence(*, run_dir: Path, timeout: float) -> dict[str, Any]:
    """Exercise not-ready then retry across a real staged-backup A2A restart."""

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
    execution_log = run_dir / "tool-executions.log"
    repo_root = Path(__file__).resolve().parents[4]
    server = _FixtureServer(
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
    try:
        server.start(1)
        first_events = _stream_request(
            server.url,
            _message_payload(workspace=workspace, prompt="request two deterministic writes"),
            timeout=timeout,
        )
        first_permissions = _unique_permissions(first_events)
        if len(first_permissions) != 1:
            raise AssertionError("first turn did not expose exactly one permission")
        permission_1 = first_permissions[0]
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
        old_shared_state = json.loads(shared_marker.read_text(encoding="utf-8"))
        old_shared_generation = int(old_shared_state["generation"])

        # Removing only the shared marker makes any accidental shared-state
        # read observable while the local generation is already sufficient.
        hidden_marker = shared_marker.with_name(".backup-state.e2e-hidden.json")
        os.replace(shared_marker, hidden_marker)
        try:
            second_events = _stream_request(
                server.url,
                _message_payload(
                    workspace=workspace,
                    prompt=_permission_response_query(permission_1),
                    context_id=str(permission_1["contextId"]),
                    task_id=str(permission_1["requestTaskId"]),
                ),
                timeout=timeout,
            )
        finally:
            os.replace(hidden_marker, shared_marker)

        second_permissions = _unique_permissions(second_events)
        if len(second_permissions) != 1:
            raise AssertionError("P1 Resume did not expose exactly one successor permission")
        permission_2 = second_permissions[0]
        if permission_2["inputId"] == permission_1["inputId"]:
            raise AssertionError("successor permission reused the P1 input id")
        if _tool_execution_lines(execution_log):
            raise AssertionError("sequential tools executed before both permissions were resolved")

        _task_path, task_snapshot = _single_json("tasks/*.json", persistence_dir)
        expected_generation = task_snapshot.get("expected_permission_backup_generation")
        if not isinstance(expected_generation, int) or expected_generation <= old_shared_generation:
            raise AssertionError("A2A Task snapshot did not advance to the P2 staged generation")
        public_payload = json.dumps(first_events + second_events, ensure_ascii=False)
        if "expected_permission_backup_generation" in public_payload or "minimum_generation" in public_payload:
            raise AssertionError("internal backup generation leaked into the public A2A stream")
        staged_snapshot = next(
            (
                path
                for path in staging_dir.rglob("{}_v{}".format(session_id, expected_generation))
                if path.is_dir()
            ),
            None,
        )
        if staged_snapshot is None:
            raise AssertionError("P2 staged snapshot is unavailable")
        _p2_staged_path, p2_staged_checkpoint = _permission_checkpoint_for_input(
            staged_snapshot,
            str(permission_2["inputId"]),
        )
        if p2_staged_checkpoint.get("phase") != "WAITING":
            raise AssertionError("P2 staged checkpoint is not waiting")

        server.stop()
        primary_sessions = [path for path in config_dir.rglob(session_id) if path.is_dir()]
        if len(primary_sessions) != 1:
            raise AssertionError("expected exactly one primary session before sandbox replacement")
        shutil.rmtree(primary_sessions[0])

        # Reproduce a replacement Sandbox after its normal startup restore:
        # the local session is the old shared P1 generation while the newer P2
        # generation is still only staged.  Use the production restore service
        # instead of copying fixture files directly.
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

        # A replacement Sandbox has its own empty local staging area.  Keep
        # the original worker attached to the first Sandbox's staged snapshots
        # so only the shared publication can make generation N available.
        replacement_staging_dir = run_dir / "replacement-staging"
        server.staging_dir = replacement_staging_dir
        server.start(2)
        p2_query = _permission_response_query(permission_2)
        not_ready_events = _stream_request(
            server.url,
            _message_payload(
                workspace=workspace,
                prompt=p2_query,
                context_id=str(permission_2["contextId"]),
            ),
            timeout=timeout,
        )
        errors = [item["error"] for event in not_ready_events for item in _walk_dicts(event) if "error" in item]
        error = next(
            (
                value
                for value in errors
                if isinstance(value, dict)
                and isinstance(value.get("data"), dict)
                and value["data"].get("code") == "SESSION_BACKUP_NOT_READY"
            ),
            None,
        )
        if error is None or error["data"].get("retryable") is not True:
            raise AssertionError("cold P2 Resume did not return retryable SESSION_BACKUP_NOT_READY")
        if "Retry after 3 seconds" not in str(error.get("message")):
            raise AssertionError("not-ready error did not tell the caller when to retry")
        if _tool_execution_lines(execution_log):
            raise AssertionError("not-ready P2 Resume executed a tool")
        _unchanged_path, unchanged_p2 = _permission_checkpoint_for_input(
            staged_snapshot,
            str(permission_2["inputId"]),
        )
        unchanged_decision = unchanged_p2.get("decision")
        if (
            unchanged_p2.get("phase") != "WAITING"
            or not isinstance(unchanged_decision, dict)
            or unchanged_decision.get("status") != "none"
            or unchanged_decision.get("value") is not None
        ):
            raise AssertionError("not-ready P2 Resume claimed or consumed the staged checkpoint")

        published = worker.run_once()
        if published < 1:
            raise AssertionError("P2 staged backup was not published to shared storage")
        shared_state = json.loads(shared_marker.read_text(encoding="utf-8"))
        if int(shared_state["generation"]) < expected_generation:
            raise AssertionError("shared backup did not reach the P2 generation")

        recovered_events = _stream_request(
            server.url,
            _message_payload(
                workspace=workspace,
                prompt=p2_query,
                context_id=str(permission_2["contextId"]),
            ),
            timeout=timeout,
        )
        if not _iac_code_values(recovered_events, "permissionRecovered"):
            raise AssertionError("P2 retry did not continue through persisted Permission Wait recovery")
        if _tool_execution_lines(execution_log) != ["executed", "executed-2"]:
            raise AssertionError("P2 retry did not execute exactly once")

        primary_sessions = [path for path in config_dir.rglob(session_id) if path.is_dir()]
        if len(primary_sessions) != 1:
            raise AssertionError("shared P2 backup was not restored into the new sandbox")
        local_state = json.loads((primary_sessions[0] / BACKUP_STATE_FILENAME).read_text(encoding="utf-8"))
        if int(local_state["generation"]) < expected_generation:
            raise AssertionError("restored local session did not meet the Task generation fence")
        _p1_path, p1_checkpoint = _permission_checkpoint_for_input(
            primary_sessions[0],
            str(permission_1["inputId"]),
        )
        _p2_path, p2_checkpoint = _permission_checkpoint_for_input(
            primary_sessions[0],
            str(permission_2["inputId"]),
        )
        if p1_checkpoint.get("phase") != "RESOLVED" or p1_checkpoint.get("ack", {}).get(
            "nextBoundaryId"
        ) != p2_checkpoint.get("boundaryId"):
            raise AssertionError("restored session lost the P1 receipt to P2 successor link")
        if p2_checkpoint.get("phase") != "RESOLVED":
            raise AssertionError("P2 retry did not resolve the restored checkpoint")

        duplicate_events = _stream_request(
            server.url,
            _message_payload(
                workspace=workspace,
                prompt=p2_query,
                context_id=str(permission_2["contextId"]),
            ),
            timeout=timeout,
        )
        duplicate_acks = [
            value for value in _iac_code_values(duplicate_events, "inputReceived") if isinstance(value, dict)
        ]
        if not any(value.get("duplicate") is True for value in duplicate_acks):
            raise AssertionError("duplicate P2 retry was not acknowledged idempotently")
        if _tool_execution_lines(execution_log) != ["executed", "executed-2"]:
            raise AssertionError("duplicate P2 retry executed a tool again")

        return {
            "passed": True,
            "scenario": "staged-backup-generation-fence",
            "taskId": permission_2["requestTaskId"],
            "contextId": permission_2["contextId"],
            "oldSharedGeneration": old_shared_generation,
            "expectedGeneration": expected_generation,
            "restoredGeneration": int(local_state["generation"]),
            "notReadyObserved": True,
            "sameSandboxResumeWithoutSharedMarker": True,
            "p1ReceiptPreserved": True,
            "p2ResolvedOnce": True,
            "toolExecutions": 2,
            "publicGenerationAbsent": True,
        }
    finally:
        server.stop()


def run_scenario(
    *,
    run_dir: Path,
    decision: str,
    timeout: float,
    mode: str,
    candidate_first: bool = False,
    pipeline_step_id: str | None = None,
    handoff_first: bool = False,
) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    workspace = run_dir / "workspace"
    workspace.mkdir()
    repo_root = Path(__file__).resolve().parents[4]
    if candidate_first and mode != "pipeline":
        raise ValueError("candidate_first requires pipeline mode")
    if pipeline_step_id and mode != "pipeline":
        raise ValueError("pipeline_step_id requires pipeline mode")
    if handoff_first and mode != "pipeline":
        raise ValueError("handoff_first requires pipeline mode")
    if candidate_first and (pipeline_step_id or handoff_first):
        raise ValueError("candidate_first cannot be combined with selling stage or handoff fixtures")
    if pipeline_step_id and handoff_first:
        raise ValueError("pipeline_step_id cannot be combined with handoff_first")
    server = _FixtureServer(
        run_dir=run_dir,
        port=_free_port(),
        repo_root=repo_root,
        mode=mode,
        candidate_first=candidate_first,
        pipeline_step_id=pipeline_step_id,
        handoff_first=handoff_first,
    )
    checkpoint_path: Path | None = None
    background: _BackgroundStream | None = None
    try:
        server.start(1)
        initial_payload = _message_payload(workspace=workspace, prompt="request deterministic write")
        if handoff_first:
            handoff_events = _stream_request(server.url, initial_payload, timeout=timeout)
            if "pipeline_handoff_ready" not in json.dumps(handoff_events, ensure_ascii=False):
                raise AssertionError("Pipeline did not publish the normal-chat handoff")
            handoff_context_id = _first_identifier(handoff_events, "contextId")
            if handoff_context_id is None:
                raise AssertionError("Pipeline handoff lost its context correlation")
            permission_events = _stream_request(
                server.url,
                _message_payload(
                    workspace=workspace,
                    prompt="change the deployed stack in normal chat",
                    context_id=handoff_context_id,
                ),
                timeout=timeout,
            )
            initial_events = handoff_events + permission_events
            permissions = _unique_permissions(permission_events)
            if len(permissions) != 1:
                raise AssertionError("normal handoff did not expose exactly one permission boundary")
            permission = permissions[0]
        elif mode == "pipeline" and candidate_first:
            candidate_events = _stream_request(server.url, initial_payload, timeout=timeout)
            inputs = [value for value in _iac_code_values(candidate_events, "input") if isinstance(value, dict)]
            candidate = next((value for value in inputs if value.get("kind") == "candidate_selection"), None)
            if candidate is None:
                raise AssertionError("initial stream did not expose candidate selection")
            background = _BackgroundStream(
                server.url,
                _message_payload(
                    workspace=workspace,
                    prompt="0",
                    context_id=str(candidate["contextId"]),
                    task_id=str(candidate["requestTaskId"]),
                ),
                timeout=timeout,
            )
            background.start()
            permission = background.wait_for_permission(timeout)
            background.join(timeout)
            initial_events = candidate_events + background.snapshot()
            if background.error is not None:
                raise RuntimeError("top-level Pipeline stream failed at the permission boundary") from background.error
        elif mode == "pipeline":
            background = _BackgroundStream(server.url, initial_payload, timeout=timeout)
            background.start()
            permission = background.wait_for_permission(timeout)
            background.join(timeout)
            initial_events = background.snapshot()
            if background.error is not None:
                raise RuntimeError("top-level Pipeline stream failed at the permission boundary") from background.error
        else:
            initial_events = _stream_request(server.url, initial_payload, timeout=timeout)
            permissions = _unique_permissions(initial_events)
            if len(permissions) != 1:
                raise AssertionError("initial stream did not expose exactly one permission boundary")
            permission = permissions[0]
        if len(_unique_permissions(initial_events)) != 1:
            raise AssertionError("scenario emitted more than one permission boundary")
        scenario = "handoff" if handoff_first else pipeline_step_id or mode
        if scenario in {*SELLING_STAGE_IDS, "handoff"}:
            _validate_structured_permission(permission, scenario=scenario)
        checkpoint_path = _checkpoint_path(run_dir / "config")
        checkpoint_before = _read_checkpoint(checkpoint_path)
        if checkpoint_before.get("phase") != "WAITING":
            raise AssertionError("initial checkpoint phase is not WAITING")
        if checkpoint_before.get("taskId") != permission.get("requestTaskId"):
            raise AssertionError("permission task correlation differs from checkpoint")
        expected_class = "pipeline" if mode == "pipeline" and not handoff_first else "normal"
        if checkpoint_before.get("permissionClass") != expected_class:
            raise AssertionError("permission checkpoint class is incorrect")
        if expected_class == "pipeline":
            coordinates = checkpoint_before.get("pipelineCoordinates")
            if not isinstance(coordinates, dict) or not coordinates.get("step"):
                raise AssertionError("Pipeline permission checkpoint lost its step coordinates")
            if pipeline_step_id and coordinates["step"].get("id") != pipeline_step_id:
                raise AssertionError("Pipeline permission checkpoint points at the wrong selling stage")

        server.stop()
        if background is not None:
            background.join(10)
        checkpoint_after_stop = _read_checkpoint(checkpoint_path)
        if checkpoint_after_stop.get("phase") in {"CANCELED", "RESOLVED"}:
            raise AssertionError("server lifecycle shutdown consumed the permission boundary")

        server.start(2)
        checkpoint_after_restart = _read_checkpoint(checkpoint_path)
        if checkpoint_after_restart.get("phase") != "WAITING":
            raise AssertionError("restarted server did not retain the waiting permission checkpoint")
        projection_preserved = expected_class == "pipeline"
        if projection_preserved:
            restored_permission = _pipeline_snapshot_permission(
                run_dir / "config",
                str(permission["inputId"]),
            )
            for field in ("toolName", "target", "operation", "displayParameters", "options"):
                if restored_permission.get(field) != permission.get(field):
                    raise AssertionError("Pipeline snapshot changed restored permission field {!r}".format(field))
        response_data = {
            "schemaVersion": 1,
            "kind": "permission",
            "requestTaskId": permission["requestTaskId"],
            "contextId": permission["contextId"],
            "inputId": permission["inputId"],
            "toolUseId": permission["toolUseId"],
            "decision": decision,
        }
        query = PERMISSION_QUERY_PREFIX + " " + json.dumps(response_data, separators=(",", ":"))
        recovered_events = _stream_request(
            server.url,
            _message_payload(workspace=workspace, prompt=query, context_id=permission["contextId"]),
            timeout=timeout,
        )
        recovered = _iac_code_values(recovered_events, "permissionRecovered")
        if not recovered:
            raise AssertionError("restarted server did not report permissionRecovered")
        acknowledgements = [
            value for value in _iac_code_values(recovered_events, "inputReceived") if isinstance(value, dict)
        ]
        if not any(value.get("recovered") is True and value.get("duplicate") is False for value in acknowledgements):
            raise AssertionError("recovered response did not return the first-consumption acknowledgement")
        if "fixture recovery completed" not in _event_text(recovered_events):
            expected_output = (
                "fixture pipeline recovery completed" if mode == "pipeline" else "fixture recovery completed"
            )
            if expected_output not in _event_text(recovered_events):
                raise AssertionError("recovered continuation output is missing")
        normal_recovery_checks: dict[str, Any] = {}
        if expected_class == "normal":
            assistant_final = [
                value for value in _iac_code_values(recovered_events, "assistantFinal") if isinstance(value, dict)
            ]
            if not any(value.get("complete") is True for value in assistant_final):
                raise AssertionError("Normal recovery did not publish assistantFinal")
            states = _task_states(recovered_events)
            if not states or states[-1] != "TASK_STATE_INPUT_REQUIRED":
                raise AssertionError("Normal recovery did not publish the terminal INPUT_REQUIRED state")
            normal_recovery_checks = {
                "assistantFinalPublished": True,
                "terminalInputRequiredPublished": True,
            }

        checkpoint_resolved = _read_checkpoint(checkpoint_path)
        if checkpoint_resolved.get("phase") != "RESOLVED":
            raise AssertionError("recovered checkpoint was not compacted to RESOLVED")
        execution_log = run_dir / "tool-executions.log"
        expected_executions = 1 if decision == "allow_once" else 0
        executions_after_recovery = (
            len(execution_log.read_text(encoding="utf-8").splitlines()) if execution_log.exists() else 0
        )
        if executions_after_recovery != expected_executions:
            raise AssertionError("unexpected tool execution count after recovery")

        duplicate_events = _stream_request(
            server.url,
            _message_payload(workspace=workspace, prompt=query, context_id=permission["contextId"]),
            timeout=timeout,
        )
        duplicate_acks = [
            value for value in _iac_code_values(duplicate_events, "inputReceived") if isinstance(value, dict)
        ]
        if not any(value.get("duplicate") is True for value in duplicate_acks):
            raise AssertionError("same duplicate did not return the durable acknowledgement")

        conflicting = dict(response_data)
        conflicting["decision"] = "deny" if decision == "allow_once" else "allow_once"
        conflict_query = PERMISSION_QUERY_PREFIX + " " + json.dumps(conflicting, separators=(",", ":"))
        conflict_events = _stream_request(
            server.url,
            _message_payload(workspace=workspace, prompt=conflict_query, context_id=permission["contextId"]),
            timeout=timeout,
        )
        if "permission_resume_invalid" not in json.dumps(conflict_events, ensure_ascii=False):
            raise AssertionError("conflicting duplicate was not rejected")

        executions_final = len(execution_log.read_text(encoding="utf-8").splitlines()) if execution_log.exists() else 0
        if executions_final != expected_executions:
            raise AssertionError("duplicate response executed the tool again")
        task_ids = {
            str(item.get("taskId"))
            for event in recovered_events
            for item in _walk_dicts(event)
            if item.get("taskId") is not None
        }
        if permission["requestTaskId"] not in task_ids:
            raise AssertionError("recovered output did not remain on the original task")

        pipeline_checks: dict[str, Any] = {}
        if expected_class == "pipeline":
            journal = _pipeline_journal_events(run_dir / "config")
            event_types = [str(event.get("eventType")) for event in journal]
            if "permission_requested" not in event_types:
                raise AssertionError("Pipeline journal did not persist the permission request")
            if "step_completed" not in event_types or "pipeline_completed" not in event_types:
                raise AssertionError("Pipeline journal did not continue after permission recovery")
            if any(event_type.startswith("rollback_") for event_type in event_types):
                raise AssertionError("Pipeline permission recovery triggered rollback")
            permission_index = event_types.index("permission_requested")
            completed_target_indexes = [
                index
                for index, event in enumerate(journal)
                if event.get("eventType") == "step_completed"
                and isinstance(event.get("step"), dict)
                and event["step"].get("id") == (pipeline_step_id or "fixture_step")
            ]
            if not completed_target_indexes or permission_index > completed_target_indexes[0]:
                raise AssertionError("Pipeline journal ordering is invalid")
            pipeline_checks = {
                "pipelineCoordinatesPreserved": True,
                "pipelineJournalOrdered": True,
                "pipelineRollbackAbsent": True,
                "parentStreamEndedAtPermissionBoundary": True,
            }

        handoff_checks: dict[str, Any] = {}
        if handoff_first:
            journal = _pipeline_journal_events(run_dir / "config")
            event_types = [str(event.get("eventType")) for event in journal]
            if "pipeline_handoff_ready" not in event_types:
                raise AssertionError("normal-chat permission was not preceded by a durable handoff")
            handoff_checks = {
                "normalHandoffPublished": True,
                "normalPermissionAfterHandoff": True,
            }

        return {
            "passed": True,
            "mode": mode,
            "decision": decision,
            "taskId": permission["requestTaskId"],
            "contextId": permission["contextId"],
            "checkpointPhase": checkpoint_resolved["phase"],
            "toolExecutions": executions_final,
            "duplicateAcknowledged": True,
            "conflictRejected": True,
            "restoredPermissionProjectionPreserved": projection_preserved,
            "durableWaitingPermissionRestored": True,
            "candidateSelectionBeforePermission": candidate_first,
            "pipelineStepId": pipeline_step_id,
            "handoffFirst": handoff_first,
            **normal_recovery_checks,
            **pipeline_checks,
            **handoff_checks,
        }
    finally:
        server.stop()


def main() -> int:
    args = _parse_args()
    if args.staged_backup_generation_fence:
        result = run_staged_backup_generation_fence(run_dir=args.run_dir, timeout=args.timeout)
    else:
        result = run_scenario(
            run_dir=args.run_dir,
            decision=args.decision,
            timeout=args.timeout,
            mode=args.mode,
            candidate_first=args.candidate_first,
            pipeline_step_id=args.pipeline_step_id,
            handoff_first=args.handoff_first,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
