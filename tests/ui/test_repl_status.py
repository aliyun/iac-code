from types import SimpleNamespace
from unittest.mock import MagicMock

from iac_code.agent.message import Message, ToolResultBlock
from iac_code.state.app_state import AppState, AppStateStore
from iac_code.ui.repl import InlineREPL


def test_count_user_turns_ignores_tool_result_messages() -> None:
    messages = [
        Message(role="user", content="first"),
        Message(role="assistant", content="answer"),
        Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="tool", is_error=False)]),
        Message(role="user", content="second"),
    ]

    assert InlineREPL._count_user_turns(messages) == 2


def test_status_snapshot_uses_agent_loop_and_original_cwd(monkeypatch) -> None:
    repl = object.__new__(InlineREPL)
    repl._session_id = "abc123"
    repl._was_resumed = True
    repl._original_cwd = "/tmp/status-project"
    repl.store = AppStateStore(AppState(model="qwen3.7-max", cwd="/other/cwd"))
    repl._provider_manager = MagicMock()
    repl._provider_manager.get_provider_display.return_value = "Alibaba Cloud Bailian"
    repl._provider_manager.get_model_name.return_value = "qwen3.7-max"
    repl._agent_loop = MagicMock()
    repl._agent_loop.max_turns = 100
    repl._agent_loop.get_session_usage.return_value = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=2,
        cache_creation_input_tokens=1,
        total_tokens=18,
        recorded_events=1,
        has_recorded_usage=True,
    )
    repl._agent_loop.get_context_usage.return_value = {
        "total_tokens": 58000,
        "context_window": 128000,
        "usage_percent": 45.3125,
    }
    repl._agent_loop.context_manager.get_messages.return_value = [
        Message(role="user", content="first"),
        Message(role="assistant", content="answer"),
    ]

    monkeypatch.setattr("iac_code.ui.repl.get_active_provider_key", lambda: "dashscope")
    monkeypatch.setattr(
        "iac_code.ui.repl.CloudCredentials",
        lambda: SimpleNamespace(get_provider=lambda name: SimpleNamespace(region_id="cn-beijing")),
    )

    snapshot = repl.get_status_snapshot()

    assert snapshot["session_id"] == "abc123"
    assert snapshot["resumed"] is True
    assert snapshot["cwd"] == "/tmp/status-project"
    assert snapshot["provider"] == "Alibaba Cloud Bailian"
    assert snapshot["model"] == "qwen3.7-max"
    assert snapshot["region"] == "cn-beijing"
    assert snapshot["turn_count"] == 1
    assert snapshot["max_turns"] == 100
    assert snapshot["api_usage"].total_tokens == 18
    assert snapshot["context_usage"]["usage_percent"] == 45.3125


def test_status_snapshot_uses_runtime_provider_manager(monkeypatch) -> None:
    repl = object.__new__(InlineREPL)
    repl._session_id = "runtime"
    repl._was_resumed = False
    repl._original_cwd = "/tmp/status-project"
    repl.store = AppStateStore(AppState(model="stale-model", cwd="/tmp/status-project"))
    repl._provider_manager = MagicMock()
    repl._provider_manager.get_provider_display.return_value = "Runtime Provider"
    repl._provider_manager.get_model_name.return_value = "runtime-model"
    repl._agent_loop = MagicMock()
    repl._agent_loop.max_turns = 100
    repl._agent_loop.get_session_usage.return_value = SimpleNamespace(
        total_tokens=0,
        recorded_events=0,
        has_recorded_usage=False,
    )
    repl._agent_loop.get_context_usage.return_value = {}
    repl._agent_loop.context_manager.get_messages.return_value = []

    monkeypatch.setattr("iac_code.ui.repl.get_active_provider_key", lambda: "openai")
    monkeypatch.setattr(
        "iac_code.ui.repl.CloudCredentials",
        lambda: SimpleNamespace(get_provider=lambda name: None),
    )

    snapshot = repl.get_status_snapshot()

    assert snapshot["provider"] == "Runtime Provider"
    assert snapshot["model"] == "runtime-model"
