import os
import time

import pytest

from iac_code.agent.message import Message, TextBlock, ToolUseBlock
from iac_code.services.session_storage import SessionStorage

CWD = "/tmp/proj-resume"


@pytest.fixture
def storage(tmp_path):
    return SessionStorage(projects_dir=tmp_path)


class TestSessionResume:
    def test_get_latest_session_anywhere(self, storage):
        storage.append(CWD, "old-session", Message(role="user", content="old"), git_branch=None)
        time.sleep(0.01)
        storage.append(CWD, "new-session", Message(role="user", content="new"), git_branch=None)
        # Bump newer file's mtime to make ordering deterministic.
        new_path = storage.session_path(CWD, "new-session")
        os.utime(new_path, (new_path.stat().st_atime, new_path.stat().st_mtime + 100))
        assert storage.get_latest_session_anywhere() == (CWD, "new-session")

    def test_no_sessions_returns_none(self, storage):
        assert storage.get_latest_session_anywhere() is None

    def test_interrupted_tool_use_detected(self, storage):
        messages = [
            Message(role="user", content="do something"),
            Message(role="assistant", content=[ToolUseBlock(id="t1", name="bash", input={"command": "ls"})]),
        ]
        storage.save(CWD, "interrupted", messages, git_branch=None)
        loaded = storage.load(CWD, "interrupted")
        assert SessionStorage.detect_interruption(loaded) is True

    def test_complete_session_not_interrupted(self, storage):
        messages = [
            Message(role="user", content="hello"),
            Message(role="assistant", content=[TextBlock(text="hi")]),
        ]
        storage.save(CWD, "complete", messages, git_branch=None)
        loaded = storage.load(CWD, "complete")
        assert SessionStorage.detect_interruption(loaded) is False

    def test_repair_interrupted(self, storage):
        messages = [
            Message(role="user", content="do something"),
            Message(role="assistant", content=[ToolUseBlock(id="t1", name="bash", input={"command": "ls"})]),
        ]
        storage.save(CWD, "repair-me", messages, git_branch=None)
        loaded = storage.load(CWD, "repair-me")
        repaired = SessionStorage.repair_interrupted(loaded)
        assert len(repaired) == 3
        last = repaired[-1]
        assert last.role == "user"

    def test_exists_returns_true_for_known_session(self, storage):
        storage.append(CWD, "my-session", Message(role="user", content="hi"), git_branch=None)
        assert storage.exists(CWD, "my-session") is True

    def test_exists_returns_false_for_unknown_session(self, storage):
        assert storage.exists(CWD, "no-such-session") is False
