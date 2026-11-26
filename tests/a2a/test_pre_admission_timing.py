"""Attribute pre-admission storage, SDK setup and workspace latency without logging content."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from iac_code.a2a.app import create_app
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.services.first_token_timing import RequestTiming, current_timing, mark_timing, use_request_timing

from . import test_first_token_timing as timing_tests

records = timing_tests.records


@pytest.mark.parametrize("method", ["SendStreamingMessage", "SendMessage"])
@pytest.mark.parametrize("mode,context_reads,workspace_calls", [("normal", 1, 1), ("pipeline", 2, 2)])
def test_pre_admission_latency_is_attributed_without_extra_info_logs(
    monkeypatch, tmp_path, records, method, mode, context_reads, workspace_calls
):
    clock = timing_tests.Clock()
    captured = {}
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_A2A_STARTUP_WARMUP", "0")
    monkeypatch.setenv("IAC_CODE_A2A_PROGRESS_ENABLED", "0")
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(tmp_path))
    monkeypatch.setattr(
        "iac_code.a2a.request_timing.RequestTiming",
        lambda *args, **kwargs: RequestTiming(*args, clock_ns=clock, **kwargs),
    )
    read_json = A2APersistenceStore._read_json

    def slow_read(path):
        if not captured:
            clock.advance(11 if path.parent.name == "tasks" else 13)
        return read_json(path)

    monkeypatch.setattr(A2APersistenceStore, "_read_json", staticmethod(slow_read))
    from iac_code.a2a import executor

    resolve_workspace = executor.resolve_workspace_path

    def slow_resolve(path):
        if not captured:
            clock.advance(17)
        return resolve_workspace(path)

    monkeypatch.setattr(executor, "resolve_workspace_path", slow_resolve)
    app = create_app(host="127.0.0.1", port=41242, token=None, model="unused", persistence_dir=tmp_path / "state")

    async def stop_at_admission(*args, **kwargs):
        timing = current_timing()
        assert timing is not None
        mark_timing("session_admission_started")
        captured.update(
            milestones=dict(timing.milestones),
            totals=dict(timing.phase_totals_ms),
            timeline=list(timing.timeline),
        )
        raise ValueError("Diagnostic boundary reached before invoking a model")

    monkeypatch.setattr(
        app.state.a2a_components.task_store, "begin_context_execution_after_reconciliation", stop_at_admission
    )
    with TestClient(app) as client:
        response = client.post(
            "/",
            headers={"A2A-Version": "1.0", "X-Request-Id": "pre-admission-probe"},
            json={
                "jsonrpc": "2.0",
                "id": "pre-admission-probe",
                "method": method,
                "params": {
                    "message": {
                        "messageId": "probe-message",
                        "contextId": "probe-context",
                        "role": "ROLE_USER",
                        "parts": [{"text": "private input"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path), "run_mode": mode, "private": "private-secret"}},
                    }
                },
            },
        )
    assert response.status_code == 200
    totals = captured["totals"]
    assert totals["task_snapshot_load"] == 22
    assert totals["context_snapshot_load"] == context_reads * 13
    assert totals["workspace_prepare"] == workspace_calls * 17
    assert totals["context_channel_resolve"] == 13
    assert totals["sdk_task_prepare"] == 11
    milestones = captured["milestones"]
    assert milestones["a2a_handler_entered"] <= milestones["sdk_task_prepare_started"]
    assert milestones["sdk_task_prepare_completed"] <= milestones["a2a_executor_entered"]
    assert milestones["session_admission_started"] - milestones["request_parsed"] == (
        22 + context_reads * 13 + workspace_calls * 17
    )
    diagnostic_stages = {event["stage"] for event in captured["timeline"]} - {
        "request_received",
        "request_admitted",
        "request_parse_started",
        "request_parse_completed",
        "request_parsed",
    }
    assert not diagnostic_stages.intersection(record["stage"] for record in records)
    encoded = json.dumps(records)
    assert "private" not in encoded and str(tmp_path) not in encoded


@pytest.mark.asyncio
async def test_channel_resolution_distinguishes_store_wait_from_storage_read(monkeypatch, tmp_path):
    clock = timing_tests.Clock()
    persistence = A2APersistenceStore(tmp_path)
    store = A2ATaskStore(persistence=persistence)
    read_json = persistence._read_json

    def slow_read(path):
        clock.advance(10)
        return read_json(path)

    monkeypatch.setattr(persistence, "_read_json", slow_read)
    timing = RequestTiming("store-wait", clock_ns=clock)
    await store._mutation_lock.acquire()
    with use_request_timing(timing):
        resolving = asyncio.create_task(store.resolve_context_telemetry_channel("new-context", None))
        try:
            await asyncio.sleep(0)
            assert not resolving.done()
            clock.advance(50)
        finally:
            store._mutation_lock.release()
        assert await resolving is None
    assert timing.phase_totals_ms["context_channel_resolve"] == 60
    assert timing.phase_totals_ms["context_snapshot_load"] == 10
