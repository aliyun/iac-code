from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from iac_code.config import get_config_dir
from scripts.a2a.e2e.resource_selector import run_live_resource_selector_restart as restart

_REAL_CONFIG = get_config_dir()  # Before the autouse fixture isolates the developer's HOME/config.
_LIVE_ENABLED = os.environ.get("IAC_CODE_A2A_RESOURCE_SELECTOR_RESTART_LIVE_E2E", "").lower() in {"1", "true", "yes"}
_MATRIX = [
    (mode, style, answer)
    for mode in ("normal", "pipeline")
    for style in restart.RESTART_STYLES
    for answer in ("selected", "canceled")
]


def test_restart_runner_requires_explicit_cloud_opt_in(tmp_path):
    with pytest.raises(SystemExit, match="2"):
        restart._parse_args(["--run-dir", str(tmp_path / "run")])


@pytest.mark.parametrize("invalid", ["missing-state", "old-execution", "wrong-task", "not-working"])
def test_restart_first_frame_rejects_unbound_execution(invalid):
    pending = {"contextId": "ctx-1", "requestTaskId": "task-1"}
    before = {"state": {"executionId": "old"}}
    snapshot = {
        "httpStatus": 200,
        "state": {
            "executionId": "new",
            "contextId": "ctx-1",
            "taskId": "task-1",
            "phase": "running",
            "executionStatus": "working",
        },
    }
    if invalid == "missing-state":
        snapshot["httpStatus"] = 404
    elif invalid == "old-execution":
        snapshot["state"]["executionId"] = "old"
    elif invalid == "wrong-task":
        snapshot["state"]["taskId"] = "other"
    else:
        snapshot["state"]["executionStatus"] = "input-required"
    with pytest.raises(AssertionError):
        restart._assert_resumed_execution(snapshot, before, pending)


@pytest.mark.parametrize("wire", ["sse", "json-error", "empty"])
def test_restart_answer_stream_observes_first_frame_and_rejects_non_sse(monkeypatch, tmp_path, wire):
    pending = {"contextId": "ctx-1", "requestTaskId": "task-1", "inputId": "input-1"}
    payload = {
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_WORKING"},
            }
        }
    }
    lines = ["data: " + json.dumps(payload) + "\n"] * 2 if wire == "sse" else []

    class Response:
        headers = {"Content-Type": "application/json" if wire == "json-error" else "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def __iter__(self):
            return iter(line.encode("utf-8") for line in lines)

        def read(self):
            return b'{"error":{"message":"active in another process"}}'

    requests = []

    def open_response(request, **_kwargs):
        requests.append(request)
        return Response()

    monkeypatch.setattr(restart, "urlopen", open_response)
    harness = SimpleNamespace(
        workspace=tmp_path,
        run_dir=tmp_path,
        env={},
        server_url="http://127.0.0.1:1234",
        args=SimpleNamespace(turn_timeout=10),
    )
    observations = []
    if wire == "sse":
        summary = restart._answer_stream(harness, pending, {"status": "selected"}, lambda: observations.append(1))
        assert summary.event_count == 2
        assert observations == [1]
    else:
        with pytest.raises(AssertionError, match="SSE"):
            restart._answer_stream(harness, pending, {"status": "selected"}, lambda: observations.append(1))
        assert not observations
    message = json.loads(requests[0].data)["params"]["message"]
    assert message["contextId"] == "ctx-1" and message["taskId"] == "task-1"
    assert requests[0].get_header("A2a-version") == restart.common.A2A_VERSION_HEADERS["A2A-Version"]


@pytest.mark.parametrize(("mode", "style", "answer"), _MATRIX)
@pytest.mark.parametrize("failure", [None, "start", "answer"])
def test_restart_flow_keeps_original_config_and_cleans_up(monkeypatch, tmp_path, mode, style, answer, failure):
    source = tmp_path / "source"
    source.mkdir()
    config_file = source / "settings.yml"
    config_file.write_text("model: fake-model\n", encoding="utf-8")
    pending = {
        "contextId": "ctx-1",
        "requestTaskId": "task-1",
        "inputId": "input-1",
        "toolUseId": "tool-1",
        "selector": {"id": "vpc.vpc"},
    }
    calls = []
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", "previous-config")
    monkeypatch.setattr(restart.live, "_refresh_source_cloud_credentials", lambda path: calls.append(("refresh", path)))
    monkeypatch.setattr(
        restart,
        "configuration_readiness",
        lambda **_kwargs: {
            "llm": {"ready": True},
            "cloud": {"ready": True},
        },
    )

    class Harness:
        def __init__(self, args, *, run_dir, config_dir, pipeline_name):
            assert config_dir == source
            assert pipeline_name == ("resource_selector_stage" if mode == "pipeline" else None)
            self.args, self.run_dir, self.env = args, run_dir, {}
            self.workspace = run_dir / "workspace"
            self.workspace.mkdir()
            self.server_index = 0
            self.lifecycle = []

        def start(self):
            self.server_index += 1
            self.server = SimpleNamespace(
                process=SimpleNamespace(pid=self.server_index), terminate=lambda: calls.append("terminate")
            )
            if failure == "start":
                raise RuntimeError("test start failure")

        def restart_after_crash(self):
            calls.append("kill")
            self.start()

        def stop(self):
            calls.append("stop")

        def stream(self, *, name, prompt, **_kwargs):
            return restart.common.StreamSummary(
                name=name,
                prompt=prompt,
                task_id="task-1",
                context_id="ctx-1",
                text=prompt,
                status_states=["TASK_STATE_INPUT_REQUIRED"],
            )

    async def cloud_query(_pending):
        return "vpc-fake", "fake", 1, ["vpc-fake"]

    def execution_state(harness, _context_id):
        return {
            "httpStatus": 200,
            "state": {
                "executionId": str(harness.server_index),
                "taskId": "task-1",
                "contextId": "ctx-1",
                "phase": "running",
                "executionStatus": "working",
            },
        }

    def answer_stream(harness, received, reply, check):
        if failure == "answer":
            raise RuntimeError("test answer failure")
        assert received == pending
        assert reply["status"] == answer
        check()
        data = {"inputId": "input-1", "status": answer}
        event = (
            {"eventType": "input_received", "data": data}
            if mode == "pipeline"
            else {"metadata": {"iac_code": {"inputReceived": data}}}
        )
        restart.common._append_jsonl(harness.run_dir / "answer.events.jsonl", event)
        return restart.common.StreamSummary(
            name="answer",
            prompt="",
            normal_handoff_ready=mode == "pipeline",
            status_states=["TASK_STATE_INPUT_REQUIRED"],
        )

    monkeypatch.setattr(restart.live, "_Harness", Harness)
    monkeypatch.setattr(restart.live, "_single_pending", lambda **_kwargs: pending)
    monkeypatch.setattr(restart.live, "_query_real_vpc", cloud_query)
    monkeypatch.setattr(restart, "_execution_state", execution_state)
    monkeypatch.setattr(restart, "_release_execution", lambda *_args: calls.append("release"))
    monkeypatch.setattr(restart, "_answer_stream", answer_stream)
    args = restart._parse_args(
        [
            "--allow-real-cloud",
            "--run-dir",
            str(tmp_path / "run"),
            "--source-config-dir",
            str(source),
            "--mode",
            mode,
            "--restart-style",
            style,
            "--answer",
            answer,
            "--model",
            "fake-model",
        ]
    )
    if failure:
        with pytest.raises(RuntimeError, match="test " + failure + " failure"):
            restart._run(args)
        assert json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))["passed"] is False
    else:
        result = restart._run(args)
        assert result["passed"] and result["configDirIsOriginal"] and result["newExecutionRegistered"]
        assert result["oldPid"] != result["newPid"]
        assert ("kill" in calls) is (style == "sigkill")
        assert ("release" in calls) is (style == "released")
    assert calls[-1] == "stop"
    assert os.environ["IAC_CODE_CONFIG_DIR"] == "previous-config"
    assert config_file.read_text(encoding="utf-8") == "model: fake-model\n"
    assert list(source.iterdir()) == [config_file]
    assert not (args.run_dir / ".runtime-config").exists()


@pytest.mark.integration
@pytest.mark.resource_selector_live
@pytest.mark.skipif(not _LIVE_ENABLED, reason="explicit live selector-restart E2E is disabled")
@pytest.mark.timeout(1200)
@pytest.mark.parametrize(("mode", "style", "answer"), _MATRIX)
def test_real_selector_answer_after_a2a_restart(tmp_path, mode, style, answer):
    repo = Path(__file__).resolve().parents[2]
    source = os.environ.get("IAC_CODE_A2A_RESOURCE_SELECTOR_LIVE_CONFIG_DIR", str(_REAL_CONFIG))
    runner = repo / "scripts/a2a/e2e/resource_selector/run_live_resource_selector_restart.py"
    command = [
        sys.executable,
        str(runner),
        "--allow-real-cloud",
        "--mode",
        mode,
        "--restart-style",
        style,
        "--answer",
        answer,
        "--run-dir",
        str(tmp_path / "run"),
        "--source-config-dir",
        source,
    ]
    completed = subprocess.run(
        command, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1180
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    for key in (
        "passed",
        "usedRealLlm",
        "usedRealCloudQuery",
        "configDirIsOriginal",
        "newExecutionRegistered",
        "nextTurnCompleted",
    ):
        assert result[key] is True
    assert result["mode"] == mode and result["restartStyle"] == style and result["answer"] == answer
    assert result["candidateCount"] > 0
    assert result["firstFrameExecutionStateHttpStatus"] == 200
    if mode == "pipeline":
        assert result["pipelineHandoffVerified"] is True
