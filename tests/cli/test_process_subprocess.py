from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_process_cli(*, config_dir: Path, stdin: str, mode: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["IAC_CODE_CONFIG_DIR"] = str(config_dir)
    if mode is None:
        env.pop("IAC_CODE_MODE", None)
    else:
        env["IAC_CODE_MODE"] = mode
    return subprocess.run(
        ["uv", "run", "iac-code", "--input-format", "stream-json", "--output-format", "stream-json"],
        cwd=REPO_ROOT,
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _stdout_frames(result: subprocess.CompletedProcess[str]) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_process_subprocess_normal_mode_initialize_and_end_session(tmp_path) -> None:
    result = _run_process_cli(
        config_dir=tmp_path,
        mode="normal",
        stdin="\n".join(
            [
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
                json.dumps({"type": "control_request", "request_id": "req-end", "request": {"subtype": "end_session"}}),
            ]
        )
        + "\n",
    )

    assert result.returncode == 0, result.stderr
    frames = _stdout_frames(result)
    assert [frame["type"] for frame in frames] == ["control_response", "control_response"]
    assert frames[0]["response"]["subtype"] == "success"
    assert frames[0]["response"]["request_id"] == "req-init"
    assert "interrupt" in frames[0]["response"]["response"]["capabilities"]
    assert frames[1]["response"]["subtype"] == "success"
    assert frames[1]["response"]["request_id"] == "req-end"


def test_process_subprocess_rejects_invalid_json(tmp_path) -> None:
    result = _run_process_cli(config_dir=tmp_path, stdin="{not-json\n")

    assert result.returncode == 1
    frames = _stdout_frames(result)
    assert frames == [
        {
            "type": "error",
            "request_id": None,
            "error": {"code": "invalid_json", "message": "Invalid JSON frame.", "retryable": False},
        }
    ]


def test_process_subprocess_pipeline_mode_initialize_and_end_session(tmp_path) -> None:
    result = _run_process_cli(
        config_dir=tmp_path,
        mode="pipeline",
        stdin="\n".join(
            [
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
                json.dumps({"type": "control_request", "request_id": "req-end", "request": {"subtype": "end_session"}}),
            ]
        )
        + "\n",
    )

    assert result.returncode == 0, result.stderr
    frames = _stdout_frames(result)
    assert [frame["type"] for frame in frames] == ["control_response", "control_response"]
    assert frames[0]["response"]["subtype"] == "success"
    assert "pipeline" in frames[0]["response"]["response"]["capabilities"]
    assert "pipeline_resume" in frames[0]["response"]["response"]["capabilities"]
    assert frames[1]["response"]["subtype"] == "success"


def test_process_subprocess_pipeline_mode_returns_recoverable_task_id(tmp_path) -> None:
    context_dir = tmp_path / "process-pipeline" / "contexts"
    context_dir.mkdir(parents=True)
    (context_dir / "ctx-1.json").write_text(
        json.dumps(
            {
                "contextId": "ctx-1",
                "taskId": "task-1",
                "iacCodeSessionId": "iac-session-1",
                "cwd": str(REPO_ROOT),
                "sidecarStatus": "waiting_input",
                "activeTaskId": "task-1",
            }
        ),
        encoding="utf-8",
    )
    result = _run_process_cli(
        config_dir=tmp_path,
        mode="pipeline",
        stdin="\n".join(
            [
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
                json.dumps(
                    {
                        "type": "user",
                        "request_id": "req-resume",
                        "metadata": {"iac_code": {"contextId": "ctx-1"}},
                        "message": {"role": "user", "content": "continue"},
                    }
                ),
            ]
        )
        + "\n",
    )

    assert result.returncode == 0, result.stderr
    frames = _stdout_frames(result)
    error = next(frame for frame in frames if frame["type"] == "error")
    assert error["request_id"] == "req-resume"
    assert error["error"]["code"] == "pipeline_task_required"
    assert error["error"]["data"] == {
        "contextId": "ctx-1",
        "recoverableTaskId": "task-1",
        "sidecarStatus": "waiting_input",
    }


def test_process_subprocess_unknown_mode_uses_normal_fallback(tmp_path) -> None:
    result = _run_process_cli(
        config_dir=tmp_path,
        mode="unexpected",
        stdin=json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}})
        + "\n",
    )

    assert result.returncode == 0, result.stderr
    frames = _stdout_frames(result)
    assert len(frames) == 1
    assert frames[0]["type"] == "control_response"
    assert frames[0]["response"]["subtype"] == "success"
