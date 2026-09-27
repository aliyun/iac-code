from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path

import pytest

from scripts.a2a.e2e import common


def _json_response(payload: dict[str, object]) -> io.BytesIO:
    response = io.BytesIO(json.dumps(payload).encode("utf-8"))
    headers = Message()
    headers["Content-Type"] = "application/json"
    response.headers = headers
    return response


def test_stream_message_accepts_json_task_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = _json_response(
        {"result": {"id": "task-1", "contextId": "ctx-1", "status": {"state": "TASK_STATE_COMPLETED"}}}
    )
    monkeypatch.setattr(common, "urlopen", lambda request, timeout: response)

    summary = common.stream_message(
        server_url="http://example.invalid", cwd=str(tmp_path), prompt="answer", name="answer",
        run_dir=tmp_path, timeout=1,
    )

    assert summary.status_states == ["TASK_STATE_COMPLETED"]
    assert summary.event_count == 1
    assert summary.response_content_type == "application/json"


def test_stream_message_surfaces_json_rpc_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = _json_response({"error": {"code": -32602, "message": "invalid request"}})
    monkeypatch.setattr(common, "urlopen", lambda request, timeout: response)

    with pytest.raises(RuntimeError, match="JSON-RPC error"):
        common.stream_message(
            server_url="http://example.invalid", cwd=str(tmp_path), prompt="answer", name="answer",
            run_dir=tmp_path, timeout=1,
        )
