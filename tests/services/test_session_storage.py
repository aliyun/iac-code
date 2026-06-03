import json
import sys

import pytest

from iac_code.agent.message import Message, TextBlock, ToolResultBlock, ToolUseBlock
from iac_code.services.session_metadata import SESSION_JSONL_FILENAME, SESSION_METADATA_FILENAME
from iac_code.services.session_storage import SessionStorage
from iac_code.services.session_usage import SessionUsageStore
from iac_code.types.stream_events import Usage

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

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
    def test_append_writes_owner_only_session_file(self, storage):
        storage.append(CWD, "private-session", Message(role="user", content="hi"), git_branch=None)
        path = storage.session_path(CWD, "private-session")

        assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
        assert oct(path.stat().st_mode & 0o777) == "0o600"

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
        assert path.name == SESSION_JSONL_FILENAME
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

    def test_cross_project_lookup_ignores_usage_sidecars(self, storage):
        import os

        usage_store = SessionUsageStore(projects_dir=storage._projects_dir)
        storage.append(CWD, "real", Message(role="user", content="real"), git_branch=None)
        usage_store.append(CWD, "real", Usage(input_tokens=10, output_tokens=5), provider="dashscope", model="qwen")
        usage_path = usage_store.path_for(CWD, "real")
        os.utime(usage_path, (usage_path.stat().st_atime, usage_path.stat().st_mtime + 100))

        assert storage.find_session_anywhere("real.usage") is None
        assert storage.get_latest_session_anywhere() == (CWD, "real")

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


def test_new_session_uses_directory_format(storage):
    storage.append(CWD, "dir-session", Message(role="user", content="hi"), git_branch="main")

    legacy_path = storage.legacy_session_path(CWD, "dir-session")
    session_dir = storage.session_dir(CWD, "dir-session")

    assert session_dir.is_dir()
    assert (session_dir / SESSION_JSONL_FILENAME).exists()
    assert not legacy_path.exists()
    assert storage.load(CWD, "dir-session") == [Message(role="user", content="hi")]


def test_existing_legacy_session_stays_legacy_until_rename(storage):
    legacy_path = storage.legacy_session_path(CWD, "legacy")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")

    storage.append(CWD, "legacy", Message(role="assistant", content="next"), git_branch=None)

    assert legacy_path.exists()
    assert not storage.session_dir(CWD, "legacy").exists()
    assert [m.role for m in storage.load(CWD, "legacy")] == ["user", "assistant"]


def test_rename_legacy_session_migrates_to_directory(storage):
    legacy_path = storage.legacy_session_path(CWD, "legacy-rename")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")

    result = storage.rename_session(CWD, "legacy-rename", "deploy-prod", git_branch="main")

    session_dir = storage.session_dir(CWD, "legacy-rename")
    assert result == "renamed"
    assert not legacy_path.exists()
    assert (session_dir / SESSION_JSONL_FILENAME).exists()
    assert (session_dir / SESSION_METADATA_FILENAME).exists()
    assert storage.read_metadata(CWD, "legacy-rename").name == "deploy-prod"
    assert storage.load(CWD, "legacy-rename")[0].content == "old"


def test_rename_rejects_same_project_duplicate_name(storage):
    storage.append(CWD, "one", Message(role="user", content="one"), git_branch=None)
    storage.append(CWD, "two", Message(role="user", content="two"), git_branch=None)
    storage.rename_session(CWD, "one", "deploy-prod", git_branch=None)

    with pytest.raises(ValueError, match="already exists"):
        storage.rename_session(CWD, "two", "deploy-prod", git_branch=None)


def test_rename_allows_same_name_in_different_projects(storage):
    storage.append("/p1", "one", Message(role="user", content="one"), git_branch=None)
    storage.append("/p2", "two", Message(role="user", content="two"), git_branch=None)

    assert storage.rename_session("/p1", "one", "deploy-prod", git_branch=None) == "renamed"
    assert storage.rename_session("/p2", "two", "deploy-prod", git_branch=None) == "renamed"


def test_rename_to_existing_name_is_noop(storage):
    storage.append(CWD, "same", Message(role="user", content="one"), git_branch=None)
    storage.rename_session(CWD, "same", "deploy-prod", git_branch=None)

    assert storage.rename_session(CWD, "same", "deploy-prod", git_branch=None) == "unchanged"
