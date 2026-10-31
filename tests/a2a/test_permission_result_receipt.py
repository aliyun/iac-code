"""Only the original complete persisted ToolResult batch can compact a permission."""

import pytest

from iac_code.agent.message import Message, ToolResultBlock, ToolUseBlock
from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
from iac_code.services.permission_wait import (
    PermissionWaitCheckpointStore,
    PermissionWaitPolicy,
    build_permission_checkpoint,
    canonical_digest,
)
from iac_code.services.session_storage import SessionStorage


class PermissionResultTranscript:
    def __init__(self, root):
        self.cwd = str(root)
        storage = SessionStorage()
        session = storage.ensure_v2_session_dir_for_new_session(self.cwd, "session-1")
        self.transcript = PipelineTranscriptStorage(session / "pipeline")
        tools = [
            ToolUseBlock(id="first", name="bash", input={"cmd": "first"}),
            ToolUseBlock(id="second", name="bash", input={"cmd": "second"}),
        ]
        self.assistant = Message(role="assistant", content=tools)
        self.transcript.append(self.cwd, "attempt-original", self.assistant)
        self.store = PermissionWaitCheckpointStore(self.cwd, "session-1")
        self.record = self.store.create(
            build_permission_checkpoint(
                session_id="session-1",
                task_id="task-1",
                context_id="ctx-1",
                input_id="input-1",
                tool_use_id="second",
                tool_name="bash",
                tool_input={"cmd": "second"},
                permission_class="pipeline",
                policy=PermissionWaitPolicy(),
                continuation_frame={
                    "assistantMessageRef": "pipeline/transcripts/attempt-original/session.jsonl:0",
                    "assistantMessageDigest": canonical_digest([tool.model_dump(mode="json") for tool in tools]),
                    "orderedToolUseIds": ["first", "second"],
                    "currentIndex": 1,
                    "decisions": [
                        {"toolUseId": "first", "state": "allow", "source": "policy", "deniedResult": None},
                        {"toolUseId": "second", "state": "pending", "source": None, "deniedResult": None},
                    ],
                },
            )
        )

    def append_results(self, identifiers):
        message = Message(
            role="user", content=[ToolResultBlock(tool_use_id=value, content="result") for value in identifiers]
        )
        self.transcript.append(self.cwd, "attempt-original", message)
        return canonical_digest(message.to_dict())


@pytest.mark.parametrize("result", ["missing", "partial", "reordered", "foreign", "changed_assistant", "complete"])
def test_result_receipt_requires_original_assistant_and_complete_ordered_batch(tmp_path, monkeypatch, result):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    fixture = PermissionResultTranscript(tmp_path)
    expected = None
    if result == "partial":
        fixture.append_results(["second"])
    elif result == "reordered":
        fixture.append_results(["second", "first"])
    elif result == "foreign":
        fixture.append_results(["first", "other"])
    elif result in {"changed_assistant", "complete"}:
        expected = fixture.append_results(["first", "second"])
        if result == "changed_assistant":
            fixture.record["continuationFrame"]["assistantMessageDigest"] = "f" * 64
            expected = None
    assert fixture.store.persisted_result_digest(fixture.record) == expected
