from iac_code.agent.agent_loop import AgentLoop
from iac_code.services.context_manager import ContextManager


def test_agent_loop_has_context_manager():
    assert hasattr(AgentLoop, "run")
    assert hasattr(AgentLoop, "run_streaming")
    assert hasattr(AgentLoop, "compact")
    assert hasattr(AgentLoop, "get_context_usage")


def test_context_manager_integration():
    cm = ContextManager(system_prompt="You are helpful.", model="qwen")
    cm.add_user_message("Hello, how are you?")
    cm.add_assistant_message("I'm doing well, thank you!")
    cm.add_user_message("Can you help me write some code?")
    usage = cm.get_usage()
    assert usage["message_count"] == 3
    assert usage["total_tokens"] > 0
    assert 0 < usage["usage_percent"] < 100
    api_msgs = cm.get_api_messages()
    assert len(api_msgs) == 3
    assert api_msgs[0]["role"] == "user"
    assert api_msgs[1]["role"] == "assistant"
