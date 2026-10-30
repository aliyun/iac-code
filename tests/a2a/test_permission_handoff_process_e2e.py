"""Permission handoff failures and competing owners through real HTTP workers."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.a2a.test_permission_prompt_e2e import Server, _permission, _reply

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for permission handoff fixture")


def document(root):
    return json.loads((root / "shared/a2a/execution-control/context-1.json").read_text(encoding="utf-8"))


def worker(root, name, fault=None, *, resident_timeout=300):
    # Only the original worker's short deadline injects the handoff fault.
    # Contenders must not start a second residence-timeout fault while replying.
    return Server(
        root,
        config_dir=str(root / name / "config"),
        persistence_dir=str(root / "shared/a2a"),
        backup_dir=str(root / "shared/backup"),
        observation_dir=str(root / "shared"),
        tool_count=1,
        resident_timeout=resident_timeout,
        fault=fault,
        strict_permission_pause=True,
    )


class WorkerPermissionProbe:
    def __init__(self, root, name):
        self.root, self.name = root, name

    def inspect(self):
        environment = {
            **os.environ,
            "IAC_CODE_CONFIG_DIR": str(self.root / self.name / "config"),
            "A2A_PERSISTENCE_DIR": str(self.root / "shared/a2a"),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "iac_code.a2a.permission_pause",
                "--persistence-root",
                str(self.root / "shared/a2a"),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        document = json.loads(result.stdout)
        assert document["schemaVersion"] == 2
        assert result.returncode == {"ready": 0, "not_applicable": 0, "pending": 1, "error": 2}[document["status"]]
        return document


def count_lines(path):
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def assert_success(events, text):
    output = json.dumps(events)
    assert not any("error" in event for event in events), events
    assert "TASK_STATE_FAILED" not in output, events
    assert "active in another process" not in output, events
    assert text in output, events


@pytest.mark.parametrize("fault", ["runtime-close", "backup-failure"])
def test_incomplete_handoff_rejects_cold_worker_and_local_reply_can_retry(tmp_path, fault):
    (tmp_path / "workspace").mkdir()
    original = worker(tmp_path, "original", fault, resident_timeout=0.2)
    replacement = worker(tmp_path, "replacement")
    marker = "runtime-close-started" if fault == "runtime-close" else "backup-failed"
    release = "release-runtime-close" if fault == "runtime-close" else "release-backup"
    try:
        original.start()
        permission = _permission(original.send("original"))
        wait_for(lambda: (tmp_path / "shared" / marker).exists())
        if fault == "runtime-close":
            wait_for(lambda: (tmp_path / "shared/runtime-close-wait-expired").exists())
        before = document(tmp_path)
        assert before["inputHandoffReady"] is False
        assert (before.get("inputHandoff") or {}).get("phase") != "COMMITTED"
        replacement.start()
        rejected = replacement.send(_reply(permission), task_id=permission["requestTaskId"])
        assert "completed approved batch" not in json.dumps(rejected)
        assert count_lines(tmp_path / "shared/executions.jsonl") == 0
        assert document(tmp_path)["executionId"] == before["executionId"]
        assert document(tmp_path)["inputHandoffReady"] is False

        (tmp_path / "shared" / release).touch()
        deadline = time.monotonic() + 15
        while True:
            resumed = original.send(_reply(permission), task_id=permission["requestTaskId"])
            if not any(
                event.get("error", {}).get("message") == "A2A context runtime is closing for permission handoff"
                for event in resumed
            ):
                break
            # Releasing the gate starts asynchronous close cleanup. The exact
            # busy response must remain non-executing until that cleanup ends.
            assert count_lines(tmp_path / "shared/executions.jsonl") == 0
            assert time.monotonic() < deadline, resumed
            time.sleep(0.02)
        assert_success(resumed, "completed approved batch")
        assert count_lines(tmp_path / "shared/executions.jsonl") == 1
        assert_success(original.send("new prompt"), "handled new prompt")
        assert count_lines(tmp_path / "shared/model-inputs.jsonl") == 3
        for server in (original, replacement):
            server.send(_reply(permission), task_id=permission["requestTaskId"])
        assert count_lines(tmp_path / "shared/executions.jsonl") == 1
        assert count_lines(tmp_path / "shared/model-inputs.jsonl") == 3
    finally:
        (tmp_path / "shared" / release).touch()
        for server in (replacement, original):
            server.stop()
            server.client.close()


def test_crash_after_shared_backup_before_handoff_commit_cannot_authorize_takeover(tmp_path):
    (tmp_path / "workspace").mkdir()
    original = worker(tmp_path, "original", "crash-before-commit", resident_timeout=0.2)
    replacement = worker(tmp_path, "replacement")
    try:
        original.start()
        permission = _permission(original.send("original"))
        wait_for(lambda: (tmp_path / "shared/crash-before-commit").exists())
        wait_for(lambda: original.process.exitcode == 77)
        before = document(tmp_path)
        assert before["inputHandoff"]["phase"] == "PREPARING"
        assert before["inputHandoffReady"] is False
        assert list((tmp_path / "shared/backup").rglob("session.jsonl")), "The shared session backup must exist"
        replacement.start()
        rejected = replacement.send(_reply(permission), task_id=permission["requestTaskId"])
        assert "completed approved batch" not in json.dumps(rejected)
        assert count_lines(tmp_path / "shared/executions.jsonl") == 0
        assert count_lines(tmp_path / "shared/model-inputs.jsonl") == 1
        after = document(tmp_path)
        assert after["executionId"] == before["executionId"]
        assert after["inputHandoff"]["phase"] == "PREPARING"
        assert after["inputHandoffReady"] is False
    finally:
        for server in (replacement, original):
            server.stop()
            server.client.close()


def test_two_http_workers_claim_once_and_old_owner_cannot_overwrite_replacement(tmp_path):
    (tmp_path / "workspace").mkdir()
    original = worker(tmp_path, "original", resident_timeout=0.2)
    contenders = [worker(tmp_path, name, "permission-claim-race") for name in ("worker-b", "worker-c")]
    try:
        original.start()
        permission = _permission(original.send("original"))
        wait_for(lambda: (document(tmp_path).get("inputHandoff") or {}).get("phase") == "COMMITTED")
        before = document(tmp_path)
        assert before["inputHandoff"]["backupReceipt"]["sharedCommitted"] is True
        assert WorkerPermissionProbe(tmp_path, "original").inspect()["status"] == "ready"
        for server in contenders:
            server.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(server.send, _reply(permission), task_id=permission["requestTaskId"])
                for server in contenders
            ]
            wait_for(lambda: (tmp_path / "shared/tool-started").exists())
            replacement = document(tmp_path)
            assert replacement["executionId"] != before["executionId"]
            assert replacement["phase"] == "running"
            assert count_lines(tmp_path / "shared/executions.jsonl") == 1
            assert WorkerPermissionProbe(tmp_path, "worker-b").inspect()["status"] == "pending"

            # Trigger real cleanup on the retired worker rather than calling
            # a private persistence method. Its original execution is fenced.
            original.client.post(
                "/iac-code/execution/terminate",
                json={
                    "contextId": "context-1",
                    "taskId": permission["requestTaskId"],
                    "expectedExecutionId": before["executionId"],
                    "requestId": "stale-owner-termination",
                    "connectionEpoch": before["connectionEpoch"],
                    "reason": "disconnect",
                },
            )
            assert document(tmp_path)["executionId"] == replacement["executionId"]
            assert document(tmp_path)["serverInstanceId"] == replacement["serverInstanceId"]
            assert document(tmp_path)["phase"] == "running"
            assert count_lines(tmp_path / "shared/executions.jsonl") == 1
            loser = futures[1].result(timeout=15)
            errors = [event["error"] for event in loser if "error" in event]
            assert len(errors) == 1, loser
            assert errors[0]["data"]["reason"] == "LIVE_OWNER", errors
            assert errors[0]["data"]["executionResumed"] is False, errors
            assert document(tmp_path)["executionId"] == replacement["executionId"]
            (tmp_path / "shared/release-tool").touch()
            results = [future.result(timeout=15) for future in futures]
        winners = [index for index, events in enumerate(results) if "completed approved batch" in json.dumps(events)]
        assert winners == [0], results
        assert_success(results[winners[0]], "completed approved batch")
        assert count_lines(tmp_path / "shared/executions.jsonl") == 1
        assert count_lines(tmp_path / "shared/model-inputs.jsonl") == 2
        winner = contenders[winners[0]]
        assert_success(winner.send("new prompt"), "handled new prompt")
        for server in [original, *contenders]:
            server.send(_reply(permission), task_id=permission["requestTaskId"])
        assert count_lines(tmp_path / "shared/executions.jsonl") == 1
        assert count_lines(tmp_path / "shared/model-inputs.jsonl") == 3
    finally:
        (tmp_path / "shared/release-tool").touch()
        for server in [*contenders, original]:
            server.stop()
            server.client.close()
