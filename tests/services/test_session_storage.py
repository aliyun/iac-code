import json

import pytest

from iac_code.agent.message import Message, TextBlock, ToolResultBlock, ToolUseBlock
from iac_code.services.session_storage import SessionStorage

CWD = "/tmp/proj-x"


@pytest.fixture
def storage(tmp_path):
    return SessionStorage(projects_dir=tmp_path)


@pytest.fixture
def sample_messages():
    return [
        Message(role="user", content="Hello"),
        Message(role="assistant", content=[TextBlock(text="Hi! Let me read that file.")]),
        Message(
            role="assistant",
            content=[ToolUseBlock(id="t1", name="read_file", input={"file_path": "/tmp/test.py"})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="t1", content="print('hello')", is_error=False)],
        ),
    ]


class TestSessionStorage:
    def test_save_and_load_roundtrip(self, storage, sample_messages):
        storage.save(CWD, "s1", sample_messages, git_branch="main")
        loaded = storage.load(CWD, "s1")
        assert len(loaded) == 4
        assert loaded[0].role == "user"
        assert loaded[0].get_text() == "Hello"

    def test_append_round_trip(self, storage):
        msg1 = Message(role="user", content="First")
        msg2 = Message(role="assistant", content=[TextBlock(text="Second")])
        storage.append(CWD, "s2", msg1, git_branch="main")
        storage.append(CWD, "s2", msg2, git_branch="main")
        loaded = storage.load(CWD, "s2")
        assert len(loaded) == 2
        assert loaded[0].get_text() == "First"

    def test_load_nonexistent(self, storage):
        assert storage.load(CWD, "nope") == []

    def test_exists(self, storage):
        assert not storage.exists(CWD, "missing")
        storage.append(CWD, "exists-id", Message(role="user", content="hi"), git_branch=None)
        assert storage.exists(CWD, "exists-id")

    def test_meta_rows_skipped_on_load(self, storage):
        storage.append(CWD, "meta-test", Message(role="user", content="real"), git_branch=None)
        storage.append_meta(CWD, "meta-test", {"type": "last-prompt", "last_prompt": "real"})
        loaded = storage.load(CWD, "meta-test")
        assert len(loaded) == 1
        assert loaded[0].get_text() == "real"

    def test_meta_requires_type(self, storage):
        with pytest.raises(ValueError):
            storage.append_meta(CWD, "x", {"last_prompt": "no type"})

    def test_message_rows_are_stamped(self, storage):
        storage.append(
            CWD,
            "stamped",
            Message(role="user", content="hi"),
            git_branch="dev",
        )
        path = storage.session_path(CWD, "stamped")
        line = path.read_text(encoding="utf-8").splitlines()[0]
        obj = json.loads(line)
        assert obj["session_id"] == "stamped"
        assert obj["cwd"] == CWD
        assert obj["git_branch"] == "dev"
        assert "version" in obj

    def test_tool_use_preserved(self, storage, sample_messages):
        storage.save(CWD, "tools", sample_messages, git_branch=None)
        loaded = storage.load(CWD, "tools")
        tool_uses = loaded[2].get_tool_use_blocks()
        assert len(tool_uses) == 1
        assert tool_uses[0].name == "read_file"

    def test_find_session_anywhere(self, storage):
        storage.append("/tmp/a", "id-aa", Message(role="user", content="from a"), git_branch=None)
        storage.append("/tmp/b", "id-bb", Message(role="user", content="from b"), git_branch=None)
        result = storage.find_session_anywhere("id-bb")
        assert result is not None
        cwd, path = result
        assert cwd == "/tmp/b"
        assert path.name == "id-bb.jsonl"
        assert storage.find_session_anywhere("missing") is None

    def test_get_latest_session_anywhere(self, storage):
        import os
        import time

        storage.append("/tmp/a", "older", Message(role="user", content="older"), git_branch=None)
        time.sleep(0.01)
        storage.append("/tmp/b", "newer", Message(role="user", content="newer"), git_branch=None)
        # Force the b file's mtime to clearly exceed a's
        b_path = storage.session_path("/tmp/b", "newer")
        os.utime(b_path, (b_path.stat().st_atime, b_path.stat().st_mtime + 100))

        result = storage.get_latest_session_anywhere()
        assert result == ("/tmp/b", "newer")

    def test_repair_interrupted_inserts_synthetic_results(self, storage):
        storage.append(
            CWD,
            "torn",
            Message(role="user", content="kick"),
            git_branch=None,
        )
        storage.append(
            CWD,
            "torn",
            Message(
                role="assistant",
                content=[ToolUseBlock(id="t1", name="Bash", input={})],
            ),
            git_branch=None,
        )
        loaded = storage.load(CWD, "torn")
        assert SessionStorage.detect_interruption(loaded)
        repaired = SessionStorage.repair_interrupted(loaded)
        assert repaired[-1].role == "user"
        assert any(getattr(b, "is_error", False) for b in repaired[-1].content)
