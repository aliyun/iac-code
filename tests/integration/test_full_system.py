"""End-to-end integration tests for the agent core capabilities."""

import pytest

from iac_code.agent.agent_types import get_agent_definition, get_builtin_agents
from iac_code.agent.message import Message
from iac_code.memory.memory_manager import MemoryManager
from iac_code.providers.manager import create_provider
from iac_code.providers.retry import RetryConfig
from iac_code.services.session_storage import SessionStorage
from iac_code.services.token_budget import TokenBudget
from iac_code.tools.base import ToolRegistry
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor


class TestToolRegistration:
    def test_all_core_tools_registered(self):
        registry = ToolRegistry()
        registry.register_default_tools()
        names = {t.name for t in registry.list_tools()}
        for expected in ["bash", "read_file", "edit_file", "write_file", "grep", "glob", "list_files"]:
            assert expected in names, f"Missing core tool: {expected}"

    def test_tool_executor_partitions_correctly(self):
        registry = ToolRegistry()
        registry.register_default_tools()
        executor = ToolExecutor(registry=registry)
        calls = [
            ToolCallRequest(id="1", name="read_file", input={"file_path": "/tmp/x"}),
            ToolCallRequest(id="2", name="grep", input={"pattern": "foo"}),
        ]
        concurrent, serial = executor.partition(calls)
        assert len(concurrent) == 2
        assert len(serial) == 0


class TestAgentDefinitions:
    def test_all_builtin_agents_valid(self):
        agents = get_builtin_agents()
        assert len(agents) >= 3
        for agent in agents:
            assert agent.agent_type
            assert agent.when_to_use
            assert agent.max_turns > 0

    def test_explore_tools_exist_in_registry(self):
        registry = ToolRegistry()
        registry.register_default_tools()
        registered = {t.name for t in registry.list_tools()}
        explore = get_agent_definition("explore")
        for tool_name in explore.tools:
            assert tool_name in registered, f"Explore allows '{tool_name}' but not registered"


class TestProviderCreation:
    def test_anthropic(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        p = create_provider("claude-sonnet-4-6", {"anthropic": "test"})
        assert p.get_model_name() == "claude-sonnet-4-6"

    def test_openai(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        p = create_provider("gpt-4.1", {"openai": "test"})
        assert p.get_model_name() == "gpt-4.1"

    def test_dashscope(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        p = create_provider("qwen3.6-plus", {"dashscope": "test"})
        assert p.get_model_name() == "qwen3.6-plus"


class TestTokenBudget:
    def test_lifecycle(self):
        budget = TokenBudget(total=100_000)
        budget.consume(50_000)
        assert budget.usage_percent == pytest.approx(50.0)
        assert not budget.is_exhausted

    def test_unlimited(self):
        budget = TokenBudget.unlimited()
        budget.consume(999_999)
        assert not budget.is_exhausted


class TestRetryConfig:
    def test_defaults(self):
        config = RetryConfig()
        assert config.calculate_delay(0) < 1.0
        assert config.calculate_delay(100) <= config.max_delay * 1.25


class TestMemorySystem:
    def test_save_and_load(self, tmp_path):
        mgr = MemoryManager(memory_dir=str(tmp_path))
        mgr.save("test", content="Hello", memory_type="user", description="Test")
        mem = mgr.load("test")
        assert mem is not None
        assert "Hello" in mem["content"]


class TestSessionStorage:
    def test_roundtrip(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        msg = Message(role="user", content="Hello")
        storage.append("/tmp/proj", "s1", msg, git_branch=None)
        loaded = storage.load("/tmp/proj", "s1")
        assert len(loaded) == 1
        assert loaded[0].get_text() == "Hello"
