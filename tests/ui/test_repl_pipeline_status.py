"""Regression tests for pipeline-aware /status (问题 6)."""

from unittest.mock import MagicMock

import pytest


def test_status_snapshot_includes_pipeline_meta_in_pipeline_mode():
    """问题 6-a：pipeline 模式下 /status 输出 pipeline.name / current_step / step_index / total_steps。"""
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline._loaded.name = "selling"
    repl._pipeline.state_machine.current_step.step_id = "confirm_and_select"
    repl._pipeline.state_machine.current_step_index = 2  # 0-indexed
    repl._pipeline._loaded.steps = [MagicMock() for _ in range(5)]
    repl._pipeline.iter_active_agent_loops.return_value = iter([])
    repl._session_id = "abc"
    repl._was_resumed = False
    repl.store = MagicMock()
    repl._original_cwd = "/tmp"
    repl._build_normal_status = lambda: {}
    repl._status_provider_display = lambda: ""
    repl._status_model = lambda m: ""
    repl._status_region = lambda: ""
    repl._aggregate_session_usage = InlineREPL._aggregate_session_usage.__get__(repl)
    repl._aggregate_context_usage = InlineREPL._aggregate_context_usage.__get__(repl)
    repl._build_pipeline_status = InlineREPL._build_pipeline_status.__get__(repl)

    repl.get_status_snapshot = InlineREPL.get_status_snapshot.__get__(repl)
    snap = repl.get_status_snapshot()
    assert snap["pipeline"]["name"] == "selling"
    assert snap["pipeline"]["current_step"] == "confirm_and_select"
    assert snap["pipeline"]["step_index"] == 3  # displayed 1-indexed
    assert snap["pipeline"]["total_steps"] == 5


def test_status_snapshot_aggregates_parallel_candidate_usage():
    """问题 6-b：并行 step 跑 2 个 candidate 时，api_usage 是两者加总。"""
    from iac_code.ui.repl import InlineREPL

    loop_a = MagicMock()
    loop_a.get_session_usage.return_value = {"prompt_tokens": 100, "completion_tokens": 50}
    loop_a.get_context_usage.return_value = {"used_tokens": 5000, "max_tokens": 100000}
    loop_a.max_turns = 50

    loop_b = MagicMock()
    loop_b.get_session_usage.return_value = {"prompt_tokens": 200, "completion_tokens": 75}
    loop_b.get_context_usage.return_value = {"used_tokens": 8000, "max_tokens": 100000}
    loop_b.max_turns = 50

    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline._loaded.name = "selling"
    repl._pipeline._loaded.steps = [MagicMock()]
    repl._pipeline.state_machine.current_step.step_id = "x"
    repl._pipeline.state_machine.current_step_index = 0
    repl._pipeline.iter_active_agent_loops.return_value = iter([loop_a, loop_b])
    repl._session_id = "abc"
    repl._was_resumed = False
    repl.store = MagicMock()
    repl._original_cwd = "/tmp"
    repl._build_normal_status = lambda: {}
    repl._status_provider_display = lambda: ""
    repl._status_model = lambda m: ""
    repl._status_region = lambda: ""
    repl._aggregate_session_usage = InlineREPL._aggregate_session_usage.__get__(repl)
    repl._aggregate_context_usage = InlineREPL._aggregate_context_usage.__get__(repl)
    repl._build_pipeline_status = InlineREPL._build_pipeline_status.__get__(repl)

    repl.get_status_snapshot = InlineREPL.get_status_snapshot.__get__(repl)
    snap = repl.get_status_snapshot()
    assert snap["api_usage"]["prompt_tokens"] == 300  # 100 + 200
    assert snap["api_usage"]["completion_tokens"] == 125  # 50 + 75


def test_status_snapshot_unchanged_in_normal_mode():
    """问题 6-c：_pipeline is None 时 get_status_snapshot 沿用原逻辑（普通模式回归）。"""
    from iac_code.ui.repl import InlineREPL

    sentinel = {"normal": "status"}
    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = None
    repl._build_normal_status = lambda: sentinel

    repl.get_status_snapshot = InlineREPL.get_status_snapshot.__get__(repl)
    snap = repl.get_status_snapshot()
    assert snap is sentinel


def test_iter_active_agent_loops_race_safe():
    """问题 6-d：单个 loop 抛错时聚合不挂，跳过即可。"""
    from iac_code.ui.repl import InlineREPL

    loop_good = MagicMock()
    loop_good.get_session_usage.return_value = {"prompt_tokens": 100}
    loop_good.get_context_usage.return_value = {}
    loop_good.max_turns = 50
    loop_bad = MagicMock()
    loop_bad.get_session_usage.side_effect = RuntimeError("race")
    loop_bad.get_context_usage.side_effect = RuntimeError("race")
    loop_bad.max_turns = 50

    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline._loaded.name = "selling"
    repl._pipeline._loaded.steps = [MagicMock()]
    repl._pipeline.state_machine.current_step.step_id = "x"
    repl._pipeline.state_machine.current_step_index = 0
    repl._pipeline.iter_active_agent_loops.return_value = iter([loop_bad, loop_good])
    repl._session_id = "abc"
    repl._was_resumed = False
    repl.store = MagicMock()
    repl._original_cwd = "/tmp"
    repl._build_normal_status = lambda: {}
    repl._status_provider_display = lambda: ""
    repl._status_model = lambda m: ""
    repl._status_region = lambda: ""
    repl._aggregate_session_usage = InlineREPL._aggregate_session_usage.__get__(repl)
    repl._aggregate_context_usage = InlineREPL._aggregate_context_usage.__get__(repl)
    repl._build_pipeline_status = InlineREPL._build_pipeline_status.__get__(repl)

    repl.get_status_snapshot = InlineREPL.get_status_snapshot.__get__(repl)
    snap = repl.get_status_snapshot()
    assert snap["api_usage"]["prompt_tokens"] == 100


@pytest.mark.asyncio
async def test_flush_pipeline_telemetry_calls_flush_in_thread(monkeypatch):
    from iac_code.ui.repl import InlineREPL

    calls = []

    def fake_flush():
        calls.append("flush")

    async def fake_to_thread(func):
        calls.append("to_thread")
        func()

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", fake_flush)
    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    repl = MagicMock(spec=InlineREPL)
    repl._flush_pipeline_telemetry = InlineREPL._flush_pipeline_telemetry.__get__(repl)

    await repl._flush_pipeline_telemetry()

    assert calls == ["to_thread", "flush"]


@pytest.mark.asyncio
async def test_flush_pipeline_telemetry_swallows_flush_errors(monkeypatch):
    from iac_code.ui.repl import InlineREPL

    def boom():
        raise RuntimeError("flush failed")

    async def fake_to_thread(func):
        func()

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", boom)
    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    repl = MagicMock(spec=InlineREPL)
    repl._flush_pipeline_telemetry = InlineREPL._flush_pipeline_telemetry.__get__(repl)

    await repl._flush_pipeline_telemetry()
