"""Regression tests for user escape gating in pipeline mode (问题 5)."""

from unittest.mock import MagicMock

import pytest

from iac_code.pipeline.config import RunMode
from iac_code.pipeline.engine.step_spec import AllowUserEscapes


@pytest.fixture
def repl_with_pipeline():
    """Construct a MagicMock(InlineREPL) with self._pipeline mocked to AllowUserEscapes(False, False, False)."""
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline.allow_user_escapes = AllowUserEscapes()
    repl.renderer = MagicMock()
    repl.command_registry = MagicMock()
    repl.command_registry.is_command.side_effect = lambda s: s.startswith("/")
    repl._is_pipeline_safe_command = InlineREPL._is_pipeline_safe_command.__get__(repl)
    repl._maybe_block_user_escape = InlineREPL._maybe_block_user_escape.__get__(repl)
    return repl


@pytest.fixture
def repl_without_pipeline(monkeypatch):
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = None
    repl._runtime_mode = RunMode.PIPELINE
    repl.renderer = MagicMock()
    repl.command_registry = MagicMock()
    repl.command_registry.is_command.side_effect = lambda s: s.startswith("/")
    repl._is_pipeline_safe_command = InlineREPL._is_pipeline_safe_command.__get__(repl)
    repl._get_runtime_mode = InlineREPL._get_runtime_mode.__get__(repl)
    repl._maybe_block_user_escape = InlineREPL._maybe_block_user_escape.__get__(repl)
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    return repl


def test_default_pipeline_blocks_shell(repl_with_pipeline):
    """问题 5-a：默认 allow_user_escapes 下 !cmd 被拦。"""
    blocked = repl_with_pipeline._maybe_block_user_escape("!ls")
    assert blocked is True
    repl_with_pipeline.renderer.print_system_message.assert_called_once()
    args, kwargs = repl_with_pipeline.renderer.print_system_message.call_args
    assert "yellow" in (kwargs.get("style") or "")
    assert "Shell" in args[0]


def test_default_pipeline_blocks_skill(repl_with_pipeline):
    """问题 5-b：$skill 被拦。"""
    blocked = repl_with_pipeline._maybe_block_user_escape("$some_skill prompt")
    assert blocked is True


def test_default_pipeline_blocks_slash_command_except_whitelist(repl_with_pipeline):
    """问题 5-c：/clear 被拦，/status /exit /help /resume 不被拦。"""
    assert repl_with_pipeline._maybe_block_user_escape("/clear") is True
    assert repl_with_pipeline._maybe_block_user_escape("/status") is False
    assert repl_with_pipeline._maybe_block_user_escape("/exit") is False
    assert repl_with_pipeline._maybe_block_user_escape("/help") is False
    assert repl_with_pipeline._maybe_block_user_escape("/resume foo") is False


def test_allow_user_escapes_true_permits_all(repl_with_pipeline):
    """问题 5-d：yaml 三个开关全 true 时三种触发都通过。"""
    repl_with_pipeline._pipeline.allow_user_escapes = AllowUserEscapes(skill=True, command=True, shell=True)
    assert repl_with_pipeline._maybe_block_user_escape("!ls") is False
    assert repl_with_pipeline._maybe_block_user_escape("$some_skill") is False
    assert repl_with_pipeline._maybe_block_user_escape("/clear") is False


def test_normal_mode_no_gate(repl_with_pipeline):
    """问题 5-e：_pipeline is None 时 gate 不生效，输入照常走。"""
    repl_with_pipeline._pipeline = None
    assert repl_with_pipeline._maybe_block_user_escape("!ls") is False
    assert repl_with_pipeline._maybe_block_user_escape("$skill") is False
    assert repl_with_pipeline._maybe_block_user_escape("/clear") is False


def test_pipeline_mode_blocks_shell_before_runner_exists(repl_without_pipeline):
    assert repl_without_pipeline._maybe_block_user_escape("!ls") is True


def test_pipeline_mode_blocks_skill_before_runner_exists(repl_without_pipeline):
    assert repl_without_pipeline._maybe_block_user_escape("$iac-aliyun test") is True


def test_pipeline_mode_blocks_slash_command_before_runner_exists(repl_without_pipeline):
    assert repl_without_pipeline._maybe_block_user_escape("/clear") is True
    assert repl_without_pipeline._maybe_block_user_escape("/help") is False
    assert repl_without_pipeline._maybe_block_user_escape("/status") is False
    assert repl_without_pipeline._maybe_block_user_escape("/resume") is False
    assert repl_without_pipeline._maybe_block_user_escape("/exit") is False


def test_normal_mode_does_not_gate_before_runner_exists(repl_without_pipeline, monkeypatch):
    repl_without_pipeline._runtime_mode = RunMode.NORMAL
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    assert repl_without_pipeline._maybe_block_user_escape("!ls") is False
    assert repl_without_pipeline._maybe_block_user_escape("$skill") is False
    assert repl_without_pipeline._maybe_block_user_escape("/clear") is False
