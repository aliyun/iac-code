from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from scripts.a2a.e2e.permission_wait import permission_wait_fixture_server as fixture_server
from scripts.a2a.e2e.permission_wait import run_permission_wait_restart as restart_runner
from scripts.a2a.e2e.permission_wait.run_permission_wait_restart import _BackgroundStream, _FixtureServer


class _UnrelatedHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"healthy"}')

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def unrelated_health_server():
    with ThreadingHTTPServer(("127.0.0.1", 0), _UnrelatedHealthHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def permission_fixture_server(tmp_path):
    return _FixtureServer(
        run_dir=tmp_path,
        port=0,
        repo_root=Path(__file__).resolve().parents[2],
        mode="normal",
        candidate_first=False,
        pipeline_step_id=None,
        handoff_first=False,
    )


@pytest.mark.integration
def test_fixture_start_binds_its_own_port_despite_unrelated_healthy_listener(
    permission_fixture_server, unrelated_health_server, monkeypatch
):
    server = permission_fixture_server
    occupied_port = unrelated_health_server.server_port
    server.port = occupied_port
    monkeypatch.setattr(restart_runner, "_free_port", lambda: occupied_port)
    try:
        server.start(2)

        assert server.port != occupied_port
        ready = json.loads((server.run_dir / "server-2.ready.json").read_text(encoding="utf-8"))
        assert ready["pid"] == server.process.pid or ready["parentPid"] == server.process.pid
        assert ready["port"] == server.port
        assert ready["nonce"]
        with urlopen(server.url + "/health", timeout=1) as response:
            assert json.load(response)["mode"] == "normal"
        with urlopen(server.url + "/.well-known/agent-card.json", timeout=1) as response:
            card = json.load(response)
        assert any(interface["url"].startswith(server.url + "/") for interface in card["supportedInterfaces"])
    finally:
        server.stop()


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="POSIX waiting launcher forwards termination to its child")
def test_fixture_readiness_accepts_interpreter_owned_by_a_waiting_launcher(permission_fixture_server, monkeypatch):
    server = permission_fixture_server
    launcher = server.run_dir / "waiting-launcher.py"
    result_path = server.run_dir / "launcher-result.json"
    launcher.write_text(
        """import json
import signal
import subprocess
import sys
from pathlib import Path

child = subprocess.Popen(sys.argv[2:])
signal.signal(signal.SIGTERM, lambda *_: child.terminate())
try:
    returncode = child.wait()
finally:
    if child.poll() is None:
        child.kill()
        child.wait()
Path(sys.argv[1]).write_text(json.dumps({"childReturnCode": returncode}), encoding="utf-8")
""",
        encoding="utf-8",
    )
    popen = subprocess.Popen

    def launch_interpreter(command, **kwargs):
        return popen([sys.executable, str(launcher), str(result_path), *command], **kwargs)

    monkeypatch.setattr(
        restart_runner,
        "subprocess",
        SimpleNamespace(Popen=launch_interpreter, TimeoutExpired=subprocess.TimeoutExpired),
    )
    try:
        server.start(1)
        ready = json.loads(server._ready_path.read_text(encoding="utf-8"))
        assert ready["pid"] != server.process.pid
        assert ready["parentPid"] == server.process.pid
        with urlopen(server.url + "/health", timeout=1) as response:
            assert json.load(response)["mode"] == "normal"
    finally:
        server.stop()
    assert isinstance(json.loads(result_path.read_text(encoding="utf-8"))["childReturnCode"], int)


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="POSIX fixed-port TIME_WAIT compatibility")
def test_fixture_legacy_fixed_port_can_restart_after_a_connection(permission_fixture_server):
    server = permission_fixture_server
    with socket.socket() as previous:
        previous.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        previous.bind(("127.0.0.1", 0))
        previous.listen()
        port = previous.getsockname()[1]
        server.port = port
        with socket.create_connection(("127.0.0.1", server.port), timeout=1) as client:
            accepted, _address = previous.accept()
            with accepted:
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""

    server._ready_path = server.run_dir / "fixed-port.ready.json"
    server._startup_nonce = "fixed-port-restart"
    command = [
        sys.executable,
        str(server.repo_root / "scripts/a2a/e2e/permission_wait/permission_wait_fixture_server.py"),
        "--port",
        str(server.port),
        "--ready-file",
        str(server._ready_path),
        "--ready-nonce",
        server._startup_nonce,
    ]
    for option in ("config-dir", "persistence-dir", "artifact-dir", "workspace", "execution-log"):
        command.extend(["--" + option, str(server.run_dir / option)])
    server._stdout = (server.run_dir / "fixed-port.stdout.log").open("w", encoding="utf-8")
    server._stderr = (server.run_dir / "fixed-port.stderr.log").open("w", encoding="utf-8")
    try:
        server.process = subprocess.Popen(command, cwd=server.repo_root, stdout=server._stdout, stderr=server._stderr)
        server._wait_healthy()
        ready = json.loads(server._ready_path.read_text(encoding="utf-8"))
        assert ready["port"] == port
        with urlopen(server.url + "/health", timeout=1) as response:
            assert json.load(response)["mode"] == "normal"
    finally:
        server.stop()


@pytest.mark.parametrize(
    "mismatch", ["nonce", "pid", "pid_bool", "pid_zero", "parent_pid_bool", "parent_pid_zero", "port", "port_bool"]
)
def test_fixture_readiness_rejects_unowned_receipt_even_when_health_is_200(
    permission_fixture_server, unrelated_health_server, monkeypatch, mismatch
):
    server = permission_fixture_server
    server.port = unrelated_health_server.server_port
    server._ready_path = server.run_dir / "server-2.ready.json"
    server._startup_nonce = "current-generation"
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as process:
        server.process = process
        receipt = {"nonce": server._startup_nonce, "pid": process.pid, "parentPid": os.getpid(), "port": server.port}
        receipt.update(
            {
                "nonce": {"nonce": "previous-generation", "pid": os.getpid(), "parentPid": process.pid},
                "pid": {"pid": os.getpid()},
                "pid_bool": {"pid": True},
                "pid_zero": {"pid": 0},
                "parent_pid_bool": {"parentPid": True},
                "parent_pid_zero": {"parentPid": 0},
                "port": {"port": 0},
                "port_bool": {"port": True},
            }[mismatch]
        )
        server._ready_path.write_text(json.dumps(receipt), encoding="utf-8")
        ticks = iter([0, 0, 21])
        monkeypatch.setattr(
            restart_runner, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)
        )
        try:
            with pytest.raises(TimeoutError, match="did not become healthy") as stopped:
                server._wait_healthy()
            if mismatch == "pid":
                identity = json.loads(str(stopped.value).split(": ", 1)[1])
                assert identity == {
                    "launcher_pid": process.pid,
                    "reported_pid": os.getpid(),
                    "reported_parent_pid": os.getpid(),
                }
                assert server._startup_nonce not in str(stopped.value)
        finally:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    "ready_options,valid",
    [
        ([], True),
        (["--ready-file", "ready.json"], False),
        (["--ready-nonce", "current-generation"], False),
        (["--ready-file", "ready.json", "--ready-nonce", ""], False),
    ],
)
def test_fixture_legacy_port_is_supported_and_readiness_flags_must_be_paired(
    tmp_path, monkeypatch, ready_options, valid
):
    arguments = ["fixture", "--port", "12345"]
    for option in ("config-dir", "persistence-dir", "artifact-dir", "workspace", "execution-log"):
        arguments.extend(["--" + option, str(tmp_path / option)])
    monkeypatch.setattr(sys, "argv", arguments + ready_options)
    if valid:
        fixture_server._parse_args()
    else:
        with pytest.raises(SystemExit) as stopped:
            fixture_server._parse_args()
        assert stopped.value.code == 2


def test_background_stream_observes_permission_published_with_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _BackgroundStream("http://unused.invalid", {}, timeout=1)
    permission = {"kind": "permission", "inputId": "input-final"}
    snapshot = stream.snapshot

    def publish_after_snapshot():
        observed = snapshot()
        if not stream.done.is_set():
            with stream._lock:
                stream.events.append({"metadata": {"iac_code": {"input": permission}}})
            stream.done.set()
        return observed

    # The producer publishes its final event and completion between the
    # consumer's snapshot and its completion check.
    monkeypatch.setattr(stream, "snapshot", publish_after_snapshot)
    assert stream.wait_for_permission(1) == permission


def test_background_stream_still_rejects_completion_without_permission() -> None:
    stream = _BackgroundStream("http://unused.invalid", {}, timeout=1)
    stream.done.set()
    with pytest.raises(RuntimeError, match="ended before permission boundary"):
        stream.wait_for_permission(1)


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_agui_staged_backup_generation_fence_retries_after_shared_publication(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "run_agui_generation_fence.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(tmp_path / "agui-staged-backup-generation-fence"),
            "--timeout",
            "25",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=110,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["scenario"] == "agui-staged-backup-generation-fence"
    assert result["notReadyObserved"] is True
    assert result["aguiStateRestored"] is True
    assert result["oldSharedGeneration"] < result["expectedGeneration"]
    assert result["restoredGeneration"] >= result["expectedGeneration"]
    assert result["p1ReceiptPreserved"] is True
    assert result["p2ResolvedOnce"] is True
    assert result["toolExecutions"] == 2
    assert result["publicGenerationAbsent"] is True
    assert result["controlledHandoffCommitted"] is True
    assert result["oldOwnerAliveAtNotReady"] is True


@pytest.mark.integration
@pytest.mark.timeout(90)
def test_staged_backup_generation_fence_retries_after_shared_publication(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "run_permission_wait_restart.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(tmp_path / "staged-backup-generation-fence"),
            "--decision",
            "allow_once",
            "--staged-backup-generation-fence",
            "--timeout",
            "20",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=80,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["scenario"] == "staged-backup-generation-fence"
    assert result["notReadyObserved"] is True
    assert result["sameSandboxResumeWithoutSharedMarker"] is True
    assert result["oldSharedGeneration"] < result["expectedGeneration"]
    assert result["restoredGeneration"] >= result["expectedGeneration"]
    assert result["p1ReceiptPreserved"] is True
    assert result["p2ResolvedOnce"] is True
    assert result["toolExecutions"] == 2
    assert result["publicGenerationAbsent"] is True
    assert result["controlledHandoffCommitted"] is True
    assert result["oldOwnerAliveAtNotReady"] is True


@pytest.mark.integration
@pytest.mark.timeout(90)
@pytest.mark.parametrize(
    ("mode", "decision", "expected_executions"),
    [
        ("normal", "allow_once", 1),
        ("normal", "deny", 0),
        ("pipeline", "allow_once", 1),
        ("pipeline", "deny", 0),
    ],
)
def test_permission_wait_response_recovers_after_real_a2a_process_restart(
    tmp_path: Path,
    mode: str,
    decision: str,
    expected_executions: int,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "run_permission_wait_restart.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(tmp_path / "{}-{}".format(mode, decision)),
            "--decision",
            decision,
            "--mode",
            mode,
            "--timeout",
            "20",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=80,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["mode"] == mode
    assert result["decision"] == decision
    assert result["checkpointPhase"] == "RESOLVED"
    assert result["controlledHandoffCommitted"] is True
    assert result["toolExecutions"] == expected_executions
    assert result["duplicateAcknowledged"] is True
    assert result["conflictRejected"] is True
    assert result["durableWaitingPermissionRestored"] is True
    assert result["restoredPermissionProjectionPreserved"] is (mode == "pipeline")
    assert result["taskId"]
    assert result["contextId"]
    if mode == "normal":
        assert result["assistantFinalPublished"] is True
        assert result["terminalInputRequiredPublished"] is True
    else:
        assert result["pipelineCoordinatesPreserved"] is True
        assert result["pipelineJournalOrdered"] is True
        assert result["pipelineRollbackAbsent"] is True
        assert result["parentStreamEndedAtPermissionBoundary"] is True


@pytest.mark.integration
@pytest.mark.timeout(90)
def test_pipeline_permission_after_candidate_selection_recovers_on_new_response_stream(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "run_permission_wait_restart.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(tmp_path / "pipeline-candidate-first"),
            "--decision",
            "allow_once",
            "--mode",
            "pipeline",
            "--candidate-first",
            "--timeout",
            "20",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=80,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["candidateSelectionBeforePermission"] is True
    assert result["checkpointPhase"] == "RESOLVED"
    assert result["toolExecutions"] == 1
    assert result["pipelineJournalOrdered"] is True


@pytest.mark.integration
@pytest.mark.timeout(90)
@pytest.mark.parametrize("decision", ["allow_once", "deny"])
@pytest.mark.parametrize(
    "pipeline_step_id",
    [
        "solution_planning_and_selection",
        "materialize_selected_candidate",
        "deploying",
    ],
)
def test_each_solution_first_stage_permission_recovers_with_allow_or_deny(
    tmp_path: Path,
    pipeline_step_id: str,
    decision: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "run_permission_wait_restart.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(tmp_path / "{}-{}".format(pipeline_step_id, decision)),
            "--decision",
            decision,
            "--mode",
            "pipeline",
            "--pipeline-step-id",
            pipeline_step_id,
            "--timeout",
            "20",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=80,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["pipelineStepId"] == pipeline_step_id
    assert result["decision"] == decision
    assert result["toolExecutions"] == (1 if decision == "allow_once" else 0)
    assert result["durableWaitingPermissionRestored"] is True
    assert result["restoredPermissionProjectionPreserved"] is True
    assert result["pipelineCoordinatesPreserved"] is True
    assert result["pipelineJournalOrdered"] is True
    assert result["pipelineRollbackAbsent"] is True


@pytest.mark.integration
@pytest.mark.timeout(90)
@pytest.mark.parametrize("decision", ["allow_once", "deny"])
def test_normal_chat_permission_after_solution_first_handoff_recovers_with_allow_or_deny(
    tmp_path: Path,
    decision: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "run_permission_wait_restart.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(tmp_path / "handoff-{}".format(decision)),
            "--decision",
            decision,
            "--mode",
            "pipeline",
            "--handoff-first",
            "--timeout",
            "20",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=80,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["handoffFirst"] is True
    assert result["decision"] == decision
    assert result["toolExecutions"] == (1 if decision == "allow_once" else 0)
    assert result["normalHandoffPublished"] is True
    assert result["normalPermissionAfterHandoff"] is True
    assert result["durableWaitingPermissionRestored"] is True
    assert result["assistantFinalPublished"] is True
