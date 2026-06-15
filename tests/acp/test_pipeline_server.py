from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import acp
import pytest

from iac_code.acp.server import ACPServer
from iac_code.pipeline.config import RunMode

_PIPELINE_UNSUPPORTED = "ACP does not support pipeline mode."


class _FakeConn:
    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        pass


def _write_session_file(config_dir: Path, session_id: str, text: str = "hello") -> None:
    project_dir = config_dir / "projects" / "-tmp"
    project_dir.mkdir(parents=True, exist_ok=True)
    message = {"role": "user", "content": [{"type": "text", "text": text}]}
    (project_dir / f"{session_id}.jsonl").write_text(json.dumps(message) + "\n", encoding="utf-8")


def _patch_pipeline_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    runtime_calls: list[str] = []

    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("iac_code.acp.server.get_run_mode", lambda: RunMode.PIPELINE)
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")

    def fake_create_runtime(options: Any) -> Any:
        runtime_calls.append(options.session_id or str(uuid.uuid4()))
        raise AssertionError("ACP pipeline mode should fail before creating a runtime")

    monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", fake_create_runtime)
    return runtime_calls


async def _assert_pipeline_unsupported(coro, runtime_calls: list[str]) -> None:
    with pytest.raises(acp.RequestError) as exc_info:
        await coro

    assert exc_info.value.code == -32602
    assert _PIPELINE_UNSUPPORTED in str(exc_info.value)
    assert runtime_calls == []


@pytest.mark.asyncio
async def test_new_session_rejects_pipeline_mode_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_calls = _patch_pipeline_mode(monkeypatch, tmp_path)
    server = ACPServer()
    server.on_connect(_FakeConn())

    await _assert_pipeline_unsupported(server.new_session(cwd="/tmp"), runtime_calls)

    assert server.sessions == {}


@pytest.mark.asyncio
async def test_load_session_rejects_pipeline_mode_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_calls = _patch_pipeline_mode(monkeypatch, tmp_path)
    _write_session_file(tmp_path, "stored-pipeline")
    server = ACPServer()
    server.on_connect(_FakeConn())

    await _assert_pipeline_unsupported(server.load_session(cwd="/tmp", session_id="stored-pipeline"), runtime_calls)

    assert server.sessions == {}


@pytest.mark.asyncio
async def test_resume_session_rejects_pipeline_mode_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_calls = _patch_pipeline_mode(monkeypatch, tmp_path)
    _write_session_file(tmp_path, "resume-pipeline")
    server = ACPServer()
    server.on_connect(_FakeConn())

    await _assert_pipeline_unsupported(server.resume_session(cwd="/tmp", session_id="resume-pipeline"), runtime_calls)

    assert server.sessions == {}


@pytest.mark.asyncio
async def test_fork_session_rejects_pipeline_mode_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_calls = _patch_pipeline_mode(monkeypatch, tmp_path)
    _write_session_file(tmp_path, "source-pipeline")
    server = ACPServer()
    server.on_connect(_FakeConn())

    await _assert_pipeline_unsupported(server.fork_session(cwd="/tmp", session_id="source-pipeline"), runtime_calls)

    assert server.sessions == {}
