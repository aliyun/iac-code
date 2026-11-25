"""Attribute preparation work without changing wire timing or logging contents."""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from iac_code.a2a.runtime_overrides import configure_runtime_model, refresh_runtime_cloud_tools
from iac_code.a2a.task_store import _close_runtime
from iac_code.services.first_token_timing import RequestTiming, timing_phase, use_request_timing
from iac_code.services.permission_wait import resolve_permission_execution_identity
from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
from iac_code.services.providers.aliyun_identity import AliyunCallerIdentityResolver
from iac_code.utils import state_io
from tests.a2a.test_first_token_timing import Clock


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_atomic_write_timing_includes_flush_replace_and_failure_cleanup(tmp_path, monkeypatch, durable, fails):
    clock = Clock()
    timing = RequestTiming("fixture-request", protocol="a2a", clock_ns=clock)
    path = tmp_path / "private-session-name.json"
    path.write_text("original", encoding="utf-8")
    fsync_calls = []

    def fsync(fd):
        fsync_calls.append(fd)
        clock.advance(3)

    def replace(source, target):
        clock.advance(7)
        if fails:
            raise OSError("fixture write failed")
        source.replace(target)

    monkeypatch.setattr(state_io.os, "fsync", fsync)
    with use_request_timing(timing), timing_phase("pipeline_prepare"):
        if fails:
            with pytest.raises(OSError, match="fixture write failed"):
                state_io.atomic_write_text(path, "private document content", durable=durable, _safe_replace=replace)
        else:
            state_io.atomic_write_text(path, "private document content", durable=durable, _safe_replace=replace)

    expected_syncs = (1 if fails or sys.platform == "win32" else 2) if durable else 0
    assert len(fsync_calls) == expected_syncs
    assert timing.phase_totals_ms["state_file_write"] == 7 + 3 * expected_syncs
    assert timing.phase_totals_ms["pipeline_prepare"] == timing.phase_totals_ms["state_file_write"]
    assert timing.timeline[2]["stage"] == "state_file_write_completed"
    assert timing.timeline[2]["result"] == ("failed" if fails else "completed")
    assert path.read_text(encoding="utf-8") == ("original" if fails else "private document content")
    assert not list(tmp_path.glob(".*.tmp"))
    encoded = json.dumps(timing.timeline)
    assert str(tmp_path) not in encoded
    assert "private-session-name" not in encoded
    assert "private document content" not in encoded


def test_runtime_configuration_and_cloud_tool_refresh_have_separate_durations():
    clock = Clock()
    timing = RequestTiming("fixture-request", clock_ns=clock)
    calls = []

    def reconfigure(*args, **kwargs):
        calls.append("model")
        clock.advance(11)

    def refresh():
        calls.append("tools")
        clock.advance(13)

    runtime = SimpleNamespace(provider_manager=SimpleNamespace(reconfigure=reconfigure), refresh_cloud_tools=refresh)
    with use_request_timing(timing):
        configure_runtime_model(runtime, "fixture-model", from_metadata=True, provider_config_frozen=True)
        refresh_runtime_cloud_tools(runtime)
    assert calls == ["model", "tools"]
    assert timing.phase_totals_ms == {"runtime_model_configure": 11, "runtime_cloud_tools_refresh": 13}


def test_pipeline_step_preparation_includes_building_its_prompt_and_agent_loop(tmp_path, monkeypatch):
    from iac_code.pipeline.engine.context import PipelineContext
    from tests.pipeline.engine.test_step_executor import SIMPLE_DEPS, _make_executor, _make_step

    executor = _make_executor(tmp_path)
    original = executor._build_full_system_prompt
    clock = Clock()
    timing = RequestTiming("fixture-request", clock_ns=clock)

    def build_prompt(*args, **kwargs):
        clock.advance(19)
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "_build_full_system_prompt", build_prompt)
    with use_request_timing(timing):
        context = executor.build_agent_loop_context(_make_step(), PipelineContext(SIMPLE_DEPS), "fixture-session")
    assert context.agent_loop._pipeline_mode is True
    assert timing.phase_totals_ms["pipeline_step_prepare"] == 19
    assert "Parse intent." in context.agent_loop.system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_runtime_close_records_actual_wait_even_when_close_fails(fails):
    clock = Clock()
    timing = RequestTiming("fixture-request", clock_ns=clock)

    async def close():
        clock.advance(17)
        if fails:
            raise RuntimeError("fixture close failed")

    with use_request_timing(timing):
        await _close_runtime(SimpleNamespace(aclose=close))
    assert timing.phase_totals_ms == {"runtime_close": 17}
    # Runtime-close errors retain their existing best-effort semantics.
    assert timing.timeline[-1]["result"] == "completed"


@pytest.mark.asyncio
async def test_permission_identity_timing_includes_the_sts_wait_without_logging_identity(monkeypatch):
    clock = Clock()
    timing = RequestTiming("fixture-request", clock_ns=clock)
    credential = AliyunCredential(mode="AK", access_key_id="fake-id", access_key_secret="fake-secret")
    monkeypatch.setattr(AliyunCredentials, "load", staticmethod(lambda: credential))

    async def request(_credential, _region):
        clock.advance(43)
        return {"IdentityType": "RAMUser", "AccountId": "1001", "UserId": "2002"}

    with use_request_timing(timing):
        identity = await resolve_permission_execution_identity(
            tool_name="aliyun_api",
            tool_input={"region_id": "cn-hangzhou"},
            resolver=AliyunCallerIdentityResolver(request=request),
        )
    assert identity.principal_kind == "ram_user"
    assert timing.phase_totals_ms == {"permission_identity_check": 43}
    assert "fake-id" not in json.dumps(timing.timeline)
    assert "1001" not in json.dumps(timing.timeline)


@pytest.mark.asyncio
async def test_permission_checkpoint_backup_timing_preserves_generation_fence(monkeypatch):
    from iac_code.a2a.input_required import backup_permission_wait_checkpoint

    clock = Clock()
    timing = RequestTiming("fixture-request", clock_ns=clock)
    expected = SimpleNamespace(enabled=True, succeeded=True, shared_committed=True)
    operations = []

    def fenced(boundary_id, *, expected_generation, operation):
        operations.append((boundary_id, expected_generation))
        return operation()

    def backup(cwd, session_id, **kwargs):
        clock.advance(31)
        return expected

    store = SimpleNamespace(load=lambda boundary: {"generation": 7}, run_generation_fenced=fenced)
    with use_request_timing(timing):
        result = await backup_permission_wait_checkpoint(
            store=store,
            boundary_id="fixture-boundary",
            cwd=".",
            session_id="fixture-session",
            backup_service=SimpleNamespace(backup_session=backup),
        )
    assert result is expected
    assert operations == [("fixture-boundary", 7)]
    assert timing.phase_totals_ms == {"permission_checkpoint_backup": 31}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "pipeline"])
async def test_permission_audit_rebuild_keeps_initialization_and_close_as_nested_phases(monkeypatch, tmp_path, mode):
    from iac_code.a2a import executor as normal_module
    from iac_code.a2a import pipeline_executor as pipeline_module

    clock = Clock()
    timing = RequestTiming("fixture-request", clock_ns=clock)
    expected = object()

    async def rebuild(*args, **kwargs):
        clock.advance(23)
        return expected

    async def close():
        clock.advance(11)

    runtime = SimpleNamespace(agent_loop=SimpleNamespace(rebuild_permission_audit_event=rebuild), aclose=close)

    def create_runtime(options):
        with timing_phase("runtime_prepare"):
            clock.advance(17)
        return runtime

    recovered = SimpleNamespace(tool_name="bash", tool_input={}, tool_use_id="fixture-tool", audit_context={})
    with use_request_timing(timing):
        if mode == "normal":
            monkeypatch.setattr(normal_module, "SessionStorage", lambda: SimpleNamespace(load=lambda *args: [object()]))
            monkeypatch.setattr(normal_module, "create_agent_runtime", create_runtime)
            result = await normal_module.IacCodeA2AExecutor._rebuild_normal_permission_audit_event(
                object(),
                recovered=recovered,
                cwd=str(tmp_path),
                session_id="fixture-session",
                model="fixture-model",
                model_from_metadata=False,
                metadata_api_key=None,
                request_policy_override=None,
                user_id=None,
                aliyun_credential=None,
                preferred_language=None,
            )
        else:
            from contextlib import nullcontext

            def create_pipeline(*args, **kwargs):
                clock.advance(5)
                return SimpleNamespace(rebuild_permission_audit_event=rebuild)

            monkeypatch.setattr(pipeline_module, "create_agent_runtime", create_runtime)
            monkeypatch.setattr(pipeline_module, "create_pipeline", create_pipeline)
            executor = SimpleNamespace(
                _backup_service=SimpleNamespace(restore_session=lambda *args: None),
                _resolve_request_pipeline_name=lambda **kwargs: "fixture-pipeline",
                _model="fixture-model",
                _provider_key_override=None,
                _provider_api_key_override=None,
                _provider_base_url_override=None,
                _provider_config_frozen=False,
                _provider_config_override=None,
                _effort_override=None,
                _resource_selector_enabled=False,
                _request_context=lambda **kwargs: nullcontext(),
                _configure_agent_runtime_for_request=lambda value: None,
                _inspect_pipeline_prerequisite_metadata=lambda **kwargs: {},
                _aliyun_delegated_executor_factory=None,
                _candidate_presentation=None,
            )
            executor._create_pipeline = lambda **kwargs: pipeline_module.IacCodeA2APipelineExecutor._create_pipeline(
                executor,
                **kwargs,
            )
            runtime.provider_manager = Mock()
            runtime.tool_registry = Mock()
            result = await pipeline_module.IacCodeA2APipelineExecutor.rebuild_permission_audit_event(
                executor,
                cwd=str(tmp_path),
                session_id="fixture-session",
                checkpoint={},
                recovered=recovered,
            )
    assert result is expected
    assert timing.phase_totals_ms["runtime_prepare"] == 17
    assert timing.phase_totals_ms["runtime_close"] == 11
    expected_total = 17 + 23 + 11 + (5 if mode == "pipeline" else 0)
    assert timing.phase_totals_ms["permission_audit_rebuild"] == expected_total
    if mode == "pipeline":
        assert timing.phase_totals_ms["pipeline_prepare"] == 5
    assert timing.timeline[0]["stage"] == "permission_audit_rebuild_started"
    assert timing.timeline[-1]["stage"] == "permission_audit_rebuild_completed"


@pytest.mark.parametrize("new_session", [False, True])
@pytest.mark.parametrize("mode", ["normal", "pipeline"])
def test_preparation_phases_do_not_emit_info_or_claim_first_event(tmp_path, caplog, mode, new_session):
    clock = Clock()
    timing = RequestTiming(
        "fixture-request",
        protocol="a2a",
        request_kind="a2a",
        mode=mode,
        is_new_session=new_session,
        clock_ns=clock,
    )
    caplog.set_level(logging.INFO, logger="iac_code.services.first_token_timing")
    with use_request_timing(timing):
        timing.record("request_received")
        state_io.atomic_write_json(tmp_path / "state.json", {"private": "fixture contents"}, durable=False)
        assert "first_event_send_completed" not in timing.milestones
        assert not any(
            record.name == "iac_code.services.first_token_timing"
            and json.loads(record.message)["stage"] != "request_received"
            for record in caplog.records
        )
        clock.advance(101)
        timing.record_send_completed(event_type="question")
        assert timing.milestones["first_event_send_completed"] == 101
        first_summary = next(
            json.loads(record.message)
            for record in caplog.records
            if record.name == "iac_code.services.first_token_timing"
            and json.loads(record.message)["stage"] == "first_token_summary"
        )
        assert first_summary["elapsed_ms"] == 101
        assert first_summary["timeline_events_dropped"] == 0
        first_snapshot = json.dumps(first_summary, sort_keys=True)
        clock.advance(29)
        state_io.atomic_write_json(tmp_path / "later-state.json", {}, durable=False)
        summaries = [
            json.loads(record.message)
            for record in caplog.records
            if record.name == "iac_code.services.first_token_timing"
            and json.loads(record.message)["stage"] == "first_token_summary"
        ]
        assert len(summaries) == 1
        assert timing.milestones["first_event_send_completed"] == 101
        assert summaries[0]["elapsed_ms"] == 101
        assert "private" not in first_snapshot
        assert str(tmp_path) not in first_snapshot
