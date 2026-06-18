from __future__ import annotations

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message
from iac_code.pipeline.engine.cleanup import CLEANUP_PROMPT_METADATA_TYPE, create_cleanup_prompt_message
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage


class FakeProviderManager:
    def __init__(self):
        self.calls = []

    def get_model_name(self):
        return "fake-model"

    async def stream(self, *, messages, system, tools=None):
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        yield TextDeltaEvent(text="ok")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class RecordingStorage:
    def __init__(self):
        self.appended = []
        self.saved = []

    def append(self, cwd, session_id, message, *, git_branch=None):
        self.appended.append((cwd, session_id, message, git_branch))

    def save(self, cwd, session_id, messages, *, git_branch=None):
        self.saved.append((cwd, session_id, messages, git_branch))


@pytest.mark.asyncio
async def test_continue_streaming_uses_existing_context_without_appending_user_message():
    provider = FakeProviderManager()
    storage = RecordingStorage()
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="system",
        tool_registry=ToolRegistry(),
        session_storage=storage,
        session_id="transcript_att_0001",
        resume_messages=[Message(role="user", content="already persisted prompt")],
        cwd="/repo",
    )

    events = [event async for event in loop.continue_streaming()]

    assert any(isinstance(event, TextDeltaEvent) and event.text == "ok" for event in events)
    assert len(provider.calls) == 1
    assert provider.calls[0]["messages"][0].content == "already persisted prompt"
    appended_roles = [message.role for _cwd, _sid, message, _branch in storage.appended]
    assert appended_roles == ["assistant"]


def test_stamp_last_turn_elapsed_preserves_cleanup_prompt_in_session(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    cwd = "/repo"
    session_id = "session-cleanup"
    storage.append(cwd, session_id, create_cleanup_prompt_message("cleanup hidden prompt"))
    loop = AgentLoop(
        provider_manager=FakeProviderManager(),
        system_prompt="system",
        tool_registry=ToolRegistry(),
        session_storage=storage,
        session_id=session_id,
        resume_messages=[Message(role="user", content="later"), Message(role="assistant", content="done")],
        cwd=cwd,
    )

    loop.stamp_last_turn_elapsed(1.5)

    loaded = storage.load(cwd, session_id)
    assert [message.content for message in loaded] == ["later", "done", "cleanup hidden prompt"]
    assert loaded[-1].metadata["type"] == CLEANUP_PROMPT_METADATA_TYPE
