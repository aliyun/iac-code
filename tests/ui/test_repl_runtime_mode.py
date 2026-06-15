"""Session-local runtime mode routing for InlineREPL."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_code.pipeline.config import RunMode


def _make_repl_for_normal_chat():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.run_streaming = MagicMock(return_value=[])
    repl._agent_loop.context_manager = MagicMock(get_messages=MagicMock(return_value=[]))
    repl._agent_loop.stamp_last_turn_elapsed = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.run_streaming_output = AsyncMock(return_value=0.0)
    repl.renderer._last_streaming_errors = []
    repl._streaming_error_log = []
    return repl


@pytest.mark.asyncio
async def test_handle_chat_uses_instance_runtime_mode_when_environment_is_normal(monkeypatch):
    monkeypatch.setenv("IAC_CODE_MODE", "normal")

    repl = _make_repl_for_normal_chat()
    repl._runtime_mode = RunMode.PIPELINE
    repl._handle_pipeline_chat = AsyncMock()

    await repl._handle_chat("hello")

    repl._handle_pipeline_chat.assert_awaited_once_with("hello")
    repl._agent_loop.run_streaming.assert_not_called()


@pytest.mark.asyncio
async def test_handle_chat_continue_rejects_instance_pipeline_mode_when_environment_is_normal(monkeypatch):
    monkeypatch.setenv("IAC_CODE_MODE", "normal")

    repl = _make_repl_for_normal_chat()
    repl._runtime_mode = RunMode.PIPELINE

    await repl._handle_chat_continue()

    repl._agent_loop.run_streaming.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_mode_helper_falls_back_to_environment_for_synthetic_repl(monkeypatch):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    repl = _make_repl_for_normal_chat()
    assert not hasattr(repl, "_runtime_mode")

    await repl._handle_chat_continue()

    repl._agent_loop.run_streaming.assert_not_called()


def test_runtime_mode_helper_does_not_read_environment_when_instance_mode_exists(monkeypatch):
    monkeypatch.setattr(
        "iac_code.pipeline.config.get_run_mode",
        MagicMock(side_effect=AssertionError("get_run_mode should not be called")),
    )

    repl = _make_repl_for_normal_chat()
    repl._runtime_mode = RunMode.NORMAL

    assert repl._get_runtime_mode() == RunMode.NORMAL


def _make_repl_for_initial_runtime_mode(tmp_path):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = str(tmp_path)
    repl._session_id = "session-1"
    repl._session_storage = MagicMock()
    repl._session_storage.session_dir.side_effect = lambda cwd, sid: tmp_path / sid
    return repl


def test_initial_runtime_mode_keeps_explicit_pipeline_for_fresh_start(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    repl = _make_repl_for_initial_runtime_mode(tmp_path)

    assert repl._resolve_initial_runtime_mode(None) == RunMode.PIPELINE


def test_initial_runtime_mode_resume_without_sidecar_falls_back_to_normal(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    repl = _make_repl_for_initial_runtime_mode(tmp_path)

    assert repl._resolve_initial_runtime_mode("session-1") == RunMode.NORMAL


def test_initial_runtime_mode_resume_with_active_sidecar_stays_pipeline(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    sidecar = tmp_path / "session-1" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text("status: running\ncurrent_step: step1\n", encoding="utf-8")

    repl = _make_repl_for_initial_runtime_mode(tmp_path)

    assert repl._resolve_initial_runtime_mode("session-1") == RunMode.PIPELINE


def test_initial_runtime_mode_resume_with_active_sidecar_enters_pipeline_even_when_env_is_normal(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    sidecar = tmp_path / "session-1" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text("status: waiting_input\ncurrent_step: step1\n", encoding="utf-8")

    repl = _make_repl_for_initial_runtime_mode(tmp_path)

    assert repl._resolve_initial_runtime_mode("session-1") == RunMode.PIPELINE
