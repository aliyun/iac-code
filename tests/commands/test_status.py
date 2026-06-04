from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.cells import cell_len
from rich.console import Console

from iac_code.commands.status import _format_compact, status_command
from iac_code.i18n import setup_i18n


def _usage(**overrides):
    values = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_tokens": 0,
        "recorded_events": 0,
        "has_recorded_usage": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _render_text(renderable) -> str:
    console = Console(record=True, width=120, color_system=None)
    console.print(renderable)
    return console.export_text()


def _cell_index_before(rendered: str, value: str) -> int:
    for line in rendered.splitlines():
        if value in line:
            return cell_len(line.split(value, 1)[0])
    raise AssertionError(f"{value!r} not found in rendered output")


def test_format_compact_uses_decimal_precision_for_thousands() -> None:
    assert _format_compact(999) == "999"
    assert _format_compact(1000) == "1k"
    assert _format_compact(1500) == "1.5k"
    assert _format_compact(9999) == "10k"
    assert _format_compact(58_000) == "58k"
    assert _format_compact(1_000_000) == "1M"
    assert _format_compact(1_500_000) == "1.5M"


@pytest.mark.asyncio
async def test_status_requires_context() -> None:
    result = await status_command()
    assert "context" in result.lower()


@pytest.mark.asyncio
async def test_status_requires_repl() -> None:
    context = MagicMock()
    context.repl = None
    result = await status_command(context=context)
    assert "repl" in result.lower()


@pytest.mark.asyncio
async def test_status_prints_recorded_usage_panel() -> None:
    console = MagicMock()
    repl = MagicMock()
    repl.get_status_snapshot.return_value = {
        "session_id": "abc123",
        "resumed": True,
        "provider": "Alibaba Cloud Bailian",
        "model": "qwen3.7-max",
        "region": "cn-beijing",
        "cwd": "/tmp/status-project",
        "api_usage": _usage(
            input_tokens=12450,
            output_tokens=3280,
            cache_read_input_tokens=8200,
            cache_creation_input_tokens=10,
            total_tokens=15730,
            recorded_events=3,
            has_recorded_usage=True,
        ),
        "turn_count": 7,
        "max_turns": 100,
        "context_usage": {
            "total_tokens": 58000,
            "context_window": 128000,
            "usage_percent": 45.3125,
        },
    }
    context = MagicMock(console=console, repl=repl)

    result = await status_command(context=context)

    assert result is None
    console.print.assert_called_once()
    rendered = _render_text(console.print.call_args.args[0])
    assert "Session Status" in rendered
    assert "abc123 (resumed)" in rendered
    assert "Alibaba Cloud Bailian" in rendered
    assert "qwen3.7-max" in rendered
    assert "cn-beijing" in rendered
    assert "12,450" in rendered
    assert "3,280" in rendered
    assert "8,200" in rendered
    assert "15,730" in rendered
    assert "Cache create" not in rendered
    assert "7 / 100" in rendered
    assert "45%" in rendered


@pytest.mark.asyncio
async def test_status_prints_no_recorded_usage_message() -> None:
    console = MagicMock()
    repl = MagicMock()
    repl.get_status_snapshot.return_value = {
        "session_id": "fresh",
        "resumed": False,
        "provider": "",
        "model": "test-model",
        "region": "",
        "cwd": "/tmp/status-project",
        "api_usage": _usage(),
        "turn_count": 0,
        "max_turns": 100,
        "context_usage": {
            "total_tokens": 0,
            "context_window": 128000,
            "usage_percent": 0.0,
        },
    }
    context = MagicMock(console=console, repl=repl)

    await status_command(context=context)

    rendered = _render_text(console.print.call_args.args[0])
    assert "not configured" in rendered
    assert "No recorded API usage" in rendered


@pytest.mark.asyncio
async def test_status_uses_compiled_translations(monkeypatch) -> None:
    monkeypatch.setenv("LANGUAGE", "zh")
    setup_i18n()
    try:
        console = MagicMock()
        repl = MagicMock()
        repl.get_status_snapshot.return_value = {
            "session_id": "abc123",
            "resumed": True,
            "provider": "dashscope",
            "model": "qwen",
            "region": "cn-beijing",
            "cwd": "/tmp/status-project",
            "api_usage": _usage(input_tokens=10, output_tokens=5, total_tokens=15, has_recorded_usage=True),
            "turn_count": 1,
            "max_turns": 100,
            "context_usage": {
                "total_tokens": 1000,
                "context_window": 128000,
                "usage_percent": 1.0,
            },
        }
        context = MagicMock(console=console, repl=repl)

        await status_command(context=context)

        rendered = _render_text(console.print.call_args.args[0])
        assert "会话状态" in rendered
        assert "abc123（已恢复）" in rendered
        assert "API Token 用量（已记录）" in rendered
        assert "输入" in rendered
        assert "缓存创建" not in rendered
    finally:
        monkeypatch.setenv("LANGUAGE", "en")
        setup_i18n()


@pytest.mark.asyncio
async def test_status_aligns_translated_labels_by_display_width(monkeypatch) -> None:
    monkeypatch.setenv("LANGUAGE", "zh")
    setup_i18n()
    try:
        console = MagicMock()
        repl = MagicMock()
        repl.get_status_snapshot.return_value = {
            "session_id": "abc123",
            "resumed": True,
            "provider": "dashscope",
            "model": "qwen",
            "region": "cn-beijing",
            "cwd": "/tmp/status-project",
            "api_usage": _usage(
                input_tokens=43210,
                output_tokens=5678,
                cache_read_input_tokens=9012,
                total_tokens=48888,
                has_recorded_usage=True,
            ),
            "turn_count": 7,
            "max_turns": 100,
            "context_usage": {
                "total_tokens": 1000,
                "context_window": 128000,
                "usage_percent": 1.0,
            },
        }
        context = MagicMock(console=console, repl=repl)

        await status_command(context=context)

        rendered = _render_text(console.print.call_args.args[0])
        main_values = [
            "abc123",
            "dashscope",
            "qwen",
            "cn-beijing",
            "/tmp/status-project",
            "7 / 100",
            "已使用 1%",
        ]
        main_starts = {_cell_index_before(rendered, value) for value in main_values}
        usage_starts = {
            _cell_index_before(rendered, value)
            for value in [
                "43,210",
                "5,678",
                "9,012",
                "48,888",
            ]
        }

        assert len(main_starts) == 1
        assert len(usage_starts) == 1
    finally:
        monkeypatch.setenv("LANGUAGE", "en")
        setup_i18n()
