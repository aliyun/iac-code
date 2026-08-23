import json
import shutil
import sys

import pytest

from iac_code.agent.message import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    create_recalled_memory_message,
    get_recalled_memory_files,
)
from iac_code.pipeline.engine.cleanup import CLEANUP_PROMPT_METADATA_TYPE, create_cleanup_prompt_message
from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_metadata import (
    SESSION_JSONL_FILENAME,
    SESSION_LAYOUT_VERSION_V2,
    SESSION_METADATA_FILENAME,
    SessionMetadata,
    write_session_metadata,
)
from iac_code.services.session_storage import SessionStorage
from iac_code.services.session_usage import SessionUsageStore
from iac_code.types.stream_events import Usage
from iac_code.utils import project_paths

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


def _symlink_or_skip(target, link, *, target_is_directory=False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


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

    def test_metadata_only_v2_session_exists_and_loads_empty_history(self, storage):
        session_dir = storage.ensure_v2_session_dir_for_new_session(CWD, "metadata-only", git_branch="main")
        assert session_dir is not None

        assert storage.exists(CWD, "metadata-only")
        assert storage.load(CWD, "metadata-only") == []

    def test_metadata_only_v2_session_symlink_root_is_ignored(self, storage, tmp_path):
        session_id = "metadata-only-symlink"
        session_dir = storage.session_dir(CWD, session_id)
        external_dir = tmp_path / "external-session"
        write_session_metadata(
            external_dir,
            SessionMetadata(session_id=session_id, cwd=CWD, layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        session_dir.parent.mkdir(parents=True, exist_ok=True)
        _symlink_or_skip(external_dir, session_dir, target_is_directory=True)

        assert storage.v2_session_dir(CWD, session_id) is None
        assert storage.read_metadata(CWD, session_id) is None
        assert storage.ensure_v2_session_dir_for_new_session(CWD, session_id, git_branch="main") is None

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

    def test_every_message_row_carries_created_at(self, storage, sample_messages):
        storage.append(CWD, "created-at", Message(role="user", content="appended"), git_branch="main")
        storage.save(CWD, "created-at", sample_messages, git_branch="main")
        storage.append(CWD, "created-at", Message(role="assistant", content="after save"), git_branch="main")

        path = storage.session_path(CWD, "created-at")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

        assert len(rows) == len(sample_messages) + 1
        for row in rows:
            created_at = row["metadata"]["createdAt"]
            assert isinstance(created_at, str) and created_at.endswith("Z")

    def test_meta_rows_carry_created_at(self, storage):
        storage.append_meta(CWD, "created-at-meta", {"type": "last-prompt", "last_prompt": "hi"})

        path = storage.session_path(CWD, "created-at-meta")
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        assert isinstance(row["createdAt"], str) and row["createdAt"].endswith("Z")

    def test_meta_row_created_at_is_not_overwritten(self, storage):
        storage.append_meta(
            CWD,
            "created-at-meta-explicit",
            {"type": "pipeline_init", "createdAt": "2026-08-19T06:30:00Z"},
        )

        path = storage.session_path(CWD, "created-at-meta-explicit")
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        assert row["createdAt"] == "2026-08-19T06:30:00Z"

    def test_save_preserves_original_created_at_on_rewrite(self, storage):
        message = Message(role="user", content="first")
        storage.append(CWD, "created-at-rewrite", message, git_branch="main")
        original = message.metadata["createdAt"]

        storage.save(CWD, "created-at-rewrite", storage.load(CWD, "created-at-rewrite"), git_branch="main")

        path = storage.session_path(CWD, "created-at-rewrite")
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        assert row["metadata"]["createdAt"] == original

    def test_created_at_survives_load_round_trip(self, storage):
        storage.append(CWD, "created-at-roundtrip", Message(role="user", content="hi"), git_branch=None)

        loaded = storage.load(CWD, "created-at-roundtrip")

        assert loaded[0].metadata["createdAt"]

    def test_tool_use_preserved(self, storage, sample_messages):
        storage.save(CWD, "tools", sample_messages, git_branch=None)
        loaded = storage.load(CWD, "tools")
        tool_uses = loaded[2].get_tool_use_blocks()
        assert len(tool_uses) == 1
        assert tool_uses[0].name == "read_file"

    def test_save_preserves_existing_cleanup_prompt_message(self, storage):
        cleanup = create_cleanup_prompt_message("cleanup hidden prompt")
        storage.append(CWD, "cleanup-save", cleanup, git_branch="main")

        storage.save(
            CWD,
            "cleanup-save",
            [Message(role="user", content="later"), Message(role="assistant", content="done")],
            git_branch="main",
            preserve_cleanup_prompts=True,
        )

        loaded = storage.load(CWD, "cleanup-save")
        assert [message.content for message in loaded] == ["later", "done", "cleanup hidden prompt"]
        assert loaded[-1].metadata["type"] == CLEANUP_PROMPT_METADATA_TYPE

    def test_save_does_not_duplicate_existing_cleanup_prompt_message(self, storage):
        cleanup = create_cleanup_prompt_message("cleanup hidden prompt")
        storage.append(CWD, "cleanup-save-once", cleanup, git_branch="main")

        storage.save(
            CWD,
            "cleanup-save-once",
            [cleanup, Message(role="assistant", content="done")],
            git_branch="main",
            preserve_cleanup_prompts=True,
        )

        loaded = storage.load(CWD, "cleanup-save-once")
        cleanup_messages = [
            message for message in loaded if message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE
        ]
        assert len(cleanup_messages) == 1

    def test_save_updates_cleanup_prompt_status_without_represerving_pending_prompt(self, storage, tmp_path):
        cleanup = create_cleanup_prompt_message(
            "cleanup hidden prompt",
            cleanup_ledger_path=tmp_path / "cleanup.yaml",
            cleanup_status="pending",
        )
        storage.append(CWD, "cleanup-status", cleanup, git_branch="main")

        completed = create_cleanup_prompt_message(
            "cleanup hidden prompt",
            cleanup_ledger_path=tmp_path / "cleanup.yaml",
            cleanup_status="completed",
        )
        storage.save(
            CWD,
            "cleanup-status",
            [completed, Message(role="assistant", content="done")],
            git_branch="main",
            preserve_cleanup_prompts=True,
        )

        loaded = storage.load(CWD, "cleanup-status")
        cleanup_messages = [
            message for message in loaded if message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE
        ]
        assert len(cleanup_messages) == 1
        assert cleanup_messages[0].metadata["cleanupStatus"] == "completed"

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

    def test_get_latest_session_anywhere_ignores_older_unsupported_layout_candidate(self, storage):
        import os

        future_dir = storage.session_dir("/tmp/a", "future-older")
        future_dir.mkdir(parents=True)
        future_path = future_dir / SESSION_JSONL_FILENAME
        future_path.write_text('{"role":"user","content":"old","cwd":"/tmp/a"}\n', encoding="utf-8")
        write_session_metadata(
            future_dir,
            SessionMetadata(session_id="future-older", cwd="/tmp/a", layout_version=99),
        )
        storage.append("/tmp/b", "newer", Message(role="user", content="newer"), git_branch=None)
        new_path = storage.session_path("/tmp/b", "newer")
        os.utime(future_path, (future_path.stat().st_atime, 1000))
        os.utime(new_path, (new_path.stat().st_atime, 2000))

        assert storage.get_latest_session_anywhere() == ("/tmp/b", "newer")

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
    loaded = storage.load(CWD, "dir-session")
    assert [(message.role, message.content) for message in loaded] == [("user", "hi")]


def test_recalled_memory_metadata_round_trips(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    msg = create_recalled_memory_message("# Recalled Memory\nUse YAML", ["ros-yaml.md"])

    storage.append("/tmp/project", "session-1", msg)
    loaded = storage.load("/tmp/project", "session-1")

    assert len(loaded) == 1
    assert get_recalled_memory_files(loaded[0]) == ["ros-yaml.md"]
    assert "Use YAML" in loaded[0].get_text()


def test_existing_legacy_session_stays_legacy_until_rename(storage):
    legacy_path = storage.legacy_session_path(CWD, "legacy")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")

    storage.append(CWD, "legacy", Message(role="assistant", content="next"), git_branch=None)

    assert legacy_path.exists()
    assert not storage.session_dir(CWD, "legacy").exists()
    assert [m.role for m in storage.load(CWD, "legacy")] == ["user", "assistant"]


@pytest.mark.parametrize("metadata_kind", ["v2", "future", "invalid"])
def test_legacy_file_wins_over_metadata_only_directory(storage, metadata_kind):
    session_id = f"legacy-shadow-{metadata_kind}"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    session_dir = legacy_path.parent / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    if metadata_kind == "invalid":
        (session_dir / SESSION_METADATA_FILENAME).write_text("{not-json", encoding="utf-8")
    else:
        write_session_metadata(
            session_dir,
            SessionMetadata(
                session_id=session_id,
                cwd=CWD,
                layout_version=SESSION_LAYOUT_VERSION_V2 if metadata_kind == "v2" else 99,
            ),
        )

    assert storage.session_path(CWD, session_id) == legacy_path
    assert storage.v2_session_dir(CWD, session_id) is None
    assert storage.ensure_v2_session_dir_for_new_session(CWD, session_id, git_branch="main") is None
    assert storage.read_metadata(CWD, session_id) is None
    assert storage.session_dir(CWD, session_id) != session_dir

    storage.append(CWD, session_id, Message(role="assistant", content="next"), git_branch=None)

    assert [message.content for message in storage.load(CWD, session_id)] == ["old", "next"]
    assert not (session_dir / SESSION_JSONL_FILENAME).exists()


def test_legacy_file_keeps_sidecar_only_directory_for_restore(storage):
    session_id = "legacy-with-sidecars"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    session_dir = legacy_path.parent / session_id
    (session_dir / "pipeline").mkdir(parents=True)
    (session_dir / "pipeline" / "meta.yaml").write_text("status: running\n", encoding="utf-8")

    assert storage.session_path(CWD, session_id) == legacy_path
    assert storage.session_dir(CWD, session_id) == session_dir

    storage.append(CWD, session_id, Message(role="assistant", content="next"), git_branch=None)

    assert [message.content for message in storage.load(CWD, session_id)] == ["old", "next"]
    assert not (session_dir / SESSION_JSONL_FILENAME).exists()
    assert (session_dir / "pipeline" / "meta.yaml").exists()


def test_legacy_file_keeps_sidecar_directory_with_rotated_audit_and_lock(storage):
    session_id = "legacy-with-rotated-audit"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    session_dir = legacy_path.parent / session_id
    (session_dir / "pipeline").mkdir(parents=True)
    (session_dir / "pipeline" / "meta.yaml").write_text("status: running\n", encoding="utf-8")
    (session_dir / "permission-audit.jsonl.1").write_text("", encoding="utf-8")
    (session_dir / ".permission-audit.jsonl.lock").write_text("", encoding="utf-8")

    assert storage.session_path(CWD, session_id) == legacy_path
    assert storage.session_dir(CWD, session_id) == session_dir


def test_legacy_file_keeps_direct_a2a_sidecar_directory_for_restore(storage):
    session_id = "legacy-with-a2a-sidecar"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    session_dir = legacy_path.parent / session_id
    a2a_pipeline_dir = session_dir / "a2a" / "pipeline"
    a2a_pipeline_dir.mkdir(parents=True)
    (a2a_pipeline_dir / "a2a-events.jsonl").write_text("", encoding="utf-8")

    assert storage.session_path(CWD, session_id) == legacy_path
    assert storage.session_dir(CWD, session_id) == session_dir


@pytest.mark.parametrize("sidecar_file", [None, "usage.jsonl", "permission-audit.jsonl"])
def test_legacy_file_uses_placeholder_for_non_pipeline_sidecar_directory(storage, sidecar_file):
    session_id = "legacy-non-pipeline-sidecars" if sidecar_file is None else f"legacy-{sidecar_file}"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    session_dir = legacy_path.parent / session_id
    session_dir.mkdir(parents=True)
    if sidecar_file is not None:
        (session_dir / sidecar_file).write_text("", encoding="utf-8")

    assert storage.session_path(CWD, session_id) == legacy_path
    assert storage.session_dir(CWD, session_id) == storage._legacy_sidecar_placeholder_dir(legacy_path)


@pytest.mark.parametrize("metadata_kind", ["v2", "future", "invalid"])
def test_rename_legacy_file_ignores_metadata_only_shadow_directory(storage, metadata_kind):
    session_id = f"legacy-shadow-rename-{metadata_kind}"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    session_dir = legacy_path.parent / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    if metadata_kind == "invalid":
        (session_dir / SESSION_METADATA_FILENAME).write_text("{not-json", encoding="utf-8")
    else:
        write_session_metadata(
            session_dir,
            SessionMetadata(
                session_id=session_id,
                name="deploy-prod" if metadata_kind == "v2" else "shadow-name",
                cwd=CWD,
                layout_version=SESSION_LAYOUT_VERSION_V2 if metadata_kind == "v2" else 99,
            ),
        )

    result = storage.rename_session(CWD, session_id, "deploy-prod", git_branch="main")

    metadata = storage.read_metadata(CWD, session_id)
    assert result == "renamed"
    assert not legacy_path.exists()
    assert (session_dir / SESSION_JSONL_FILENAME).exists()
    assert metadata is not None
    assert metadata.name == "deploy-prod"
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2
    assert storage.load(CWD, session_id)[0].content == "old"


@pytest.mark.parametrize("has_jsonl", [False, True])
@pytest.mark.parametrize("layout_version", [SESSION_LAYOUT_VERSION_V2, 99])
def test_rename_refuses_mismatched_metadata_before_mutating(storage, has_jsonl, layout_version):
    session_id = f"rename-conflict-{has_jsonl}-{layout_version}"
    session_dir = storage.session_dir(CWD, session_id)
    session_dir.mkdir(parents=True)
    if has_jsonl:
        (session_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"wrong"}\n', encoding="utf-8")
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id=f"different-{session_id}",
            cwd=CWD,
            name="shadow-name",
            layout_version=layout_version,
        ),
    )
    metadata_before = (session_dir / SESSION_METADATA_FILENAME).read_text(encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        storage.rename_session(CWD, session_id, "deploy-prod", git_branch="main")

    assert (session_dir / SESSION_METADATA_FILENAME).read_text(encoding="utf-8") == metadata_before


def test_rename_name_owner_ignores_mismatched_metadata(storage):
    storage.append(CWD, "actual", Message(role="user", content="hello"), git_branch=None)
    shadow_dir = storage.session_dir(CWD, "shadow")
    shadow_dir.mkdir(parents=True)
    (shadow_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"shadow"}\n', encoding="utf-8")
    write_session_metadata(
        shadow_dir,
        SessionMetadata(
            session_id="different-shadow",
            cwd=CWD,
            name="deploy-prod",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )

    assert storage.rename_session(CWD, "actual", "deploy-prod", git_branch=None) == "renamed"
    assert storage.read_metadata(CWD, "actual").name == "deploy-prod"


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


def test_rename_legacy_session_merges_legacy_sidecar_placeholder(storage):
    session_id = "legacy-rename-sidecars"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    placeholder_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    (placeholder_dir / "pipeline").mkdir(parents=True)
    (placeholder_dir / "pipeline" / "meta.yaml").write_text("status: running\n", encoding="utf-8")
    (placeholder_dir / "permission-audit.jsonl").write_text('{"decision":"allow"}\n', encoding="utf-8")

    result = storage.rename_session(CWD, session_id, "deploy-prod", git_branch="main")

    session_dir = storage.session_dir(CWD, session_id)
    assert result == "renamed"
    assert not placeholder_dir.exists()
    assert (session_dir / SESSION_JSONL_FILENAME).exists()
    assert (session_dir / "pipeline" / "meta.yaml").read_text(encoding="utf-8") == "status: running\n"
    assert (session_dir / "permission-audit.jsonl").read_text(encoding="utf-8") == '{"decision":"allow"}\n'


def test_rename_legacy_session_does_not_overwrite_existing_sidecar_child(storage):
    session_id = "legacy-rename-sidecar-conflict"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    session_dir = legacy_path.parent / session_id
    (session_dir / "pipeline").mkdir(parents=True)
    (session_dir / "pipeline" / "meta.yaml").write_text("status: existing\n", encoding="utf-8")
    placeholder_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    (placeholder_dir / "pipeline").mkdir(parents=True)
    (placeholder_dir / "pipeline" / "meta.yaml").write_text("status: placeholder\n", encoding="utf-8")

    result = storage.rename_session(CWD, session_id, "deploy-prod", git_branch="main")

    assert result == "renamed"
    assert (session_dir / SESSION_JSONL_FILENAME).exists()
    assert (session_dir / "pipeline" / "meta.yaml").read_text(encoding="utf-8") == "status: existing\n"
    assert (placeholder_dir / "pipeline" / "meta.yaml").read_text(encoding="utf-8") == "status: placeholder\n"


def test_rename_legacy_session_merges_placeholder_despite_lock_and_unknown_leftovers(storage):
    session_id = "legacy-rename-sidecar-lock"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    placeholder_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    (placeholder_dir / "pipeline").mkdir(parents=True)
    (placeholder_dir / "pipeline" / "meta.yaml").write_text("status: running\n", encoding="utf-8")
    (placeholder_dir / "permission-audit.jsonl.1").write_text("rotated\n", encoding="utf-8")
    (placeholder_dir / ".permission-audit.jsonl.lock").write_text("", encoding="utf-8")
    (placeholder_dir / "unexpected.tmp").write_text("leftover\n", encoding="utf-8")

    result = storage.rename_session(CWD, session_id, "deploy-prod", git_branch="main")

    session_dir = storage.session_dir(CWD, session_id)
    assert result == "renamed"
    assert (session_dir / "pipeline" / "meta.yaml").read_text(encoding="utf-8") == "status: running\n"
    assert (session_dir / "permission-audit.jsonl.1").read_text(encoding="utf-8") == "rotated\n"
    assert (session_dir / ".permission-audit.jsonl.lock").exists()
    assert not (placeholder_dir / "pipeline").exists()
    assert (placeholder_dir / "unexpected.tmp").read_text(encoding="utf-8") == "leftover\n"


def test_rename_legacy_session_ignores_symlinked_sidecar_placeholder(storage, tmp_path):
    session_id = "legacy-rename-sidecar-symlink"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    external_dir = tmp_path / "external-sidecars"
    (external_dir / "pipeline").mkdir(parents=True)
    (external_dir / "pipeline" / "meta.yaml").write_text("status: external\n", encoding="utf-8")
    placeholder_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    _symlink_or_skip(external_dir, placeholder_dir, target_is_directory=True)

    result = storage.rename_session(CWD, session_id, "deploy-prod", git_branch="main")

    session_dir = storage.session_dir(CWD, session_id)
    assert result == "renamed"
    assert not (session_dir / "pipeline").exists()
    assert (external_dir / "pipeline" / "meta.yaml").read_text(encoding="utf-8") == "status: external\n"


def test_legacy_session_dir_ignores_symlinked_sidecar_placeholder(storage, tmp_path):
    session_id = "legacy-sidecar-symlink"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")
    external_dir = tmp_path / "external-sidecars"
    (external_dir / "pipeline").mkdir(parents=True)
    (external_dir / "pipeline" / "events.jsonl").write_text("[]", encoding="utf-8")
    placeholder_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    _symlink_or_skip(external_dir, placeholder_dir, target_is_directory=True)

    session_dir = storage.session_dir(CWD, session_id)

    assert session_dir != placeholder_dir
    assert session_dir.name == f"{placeholder_dir.name}.conflict-sidecars"


def test_legacy_session_dir_ignores_dangling_symlinked_sidecar_placeholder(storage, tmp_path):
    session_id = "legacy-sidecar-dangling-symlink"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")
    placeholder_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    _symlink_or_skip(tmp_path / "missing-sidecars", placeholder_dir, target_is_directory=True)

    session_dir = storage.session_dir(CWD, session_id)

    assert session_dir != placeholder_dir
    assert session_dir.name == f"{placeholder_dir.name}.conflict-sidecars"


def test_legacy_session_dir_ignores_reparse_sidecar_placeholder(storage, monkeypatch):
    session_id = "legacy-sidecar-reparse"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")
    placeholder_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    placeholder_dir.mkdir()
    monkeypatch.setattr(SessionStorage, "_is_reparse_point", staticmethod(lambda path: path == placeholder_dir))

    session_dir = storage.session_dir(CWD, session_id)

    assert session_dir != placeholder_dir
    assert session_dir.name == f"{placeholder_dir.name}.conflict-sidecars"


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


def test_save_does_not_scan_old_file_unless_preserving_cleanup_prompts(tmp_path, monkeypatch):
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append("/tmp/project", "sid", Message(role="user", content="old"))

    def fail_load(cwd, session_id):
        raise AssertionError("save should not load existing messages")

    monkeypatch.setattr(storage, "load", fail_load)

    storage.save("/tmp/project", "sid", [Message(role="user", content="new")])

    assert [message.content for message in SessionStorage(projects_dir=tmp_path).load("/tmp/project", "sid")] == ["new"]


def test_save_can_preserve_cleanup_prompts_when_requested(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    cleanup = create_cleanup_prompt_message("cleanup stack-123", cleanup_ledger_path=tmp_path / "cleanup.yaml")
    storage.append("/tmp/project", "sid", cleanup)

    storage.save(
        "/tmp/project",
        "sid",
        [Message(role="user", content="new")],
        preserve_cleanup_prompts=True,
    )

    loaded = SessionStorage(projects_dir=tmp_path).load("/tmp/project", "sid")
    assert [message.content for message in loaded] == ["new", "cleanup stack-123"]


def test_append_uses_locked_jsonl_helper(tmp_path, monkeypatch):
    storage = SessionStorage(projects_dir=tmp_path)
    calls = []

    def fake_append(path, records, *, durable=False):
        calls.append((path.name, list(records), durable))

    monkeypatch.setattr("iac_code.services.session_storage.append_jsonl_locked", fake_append)

    storage.append("/tmp/project", "sid", Message(role="user", content="hello"), git_branch="main")

    assert calls[0][0] == "session.jsonl"
    assert calls[0][1][0]["content"] == "hello"
    assert calls[0][1][0]["git_branch"] == "main"


def test_legacy_migration_keeps_directory_session_when_present(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    directory = storage.session_dir("/tmp/project", "sid")
    directory.mkdir(parents=True)
    directory_path = directory / "session.jsonl"
    directory_path.write_text('{"role":"user","content":"directory"}\n', encoding="utf-8")
    legacy_path = storage.legacy_session_path("/tmp/project", "sid")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")

    assert storage._ensure_directory_format("/tmp/project", "sid") == directory

    assert directory_path.read_text(encoding="utf-8") == '{"role":"user","content":"directory"}\n'


def test_new_directory_session_writes_layout_v2_metadata(storage):
    storage.append(CWD, "layout-v2", Message(role="user", content="hello"), git_branch="main")

    metadata = storage.read_metadata(CWD, "layout-v2")

    assert metadata is not None
    assert metadata.layout_version == 2
    assert metadata.cwd == CWD
    assert metadata.git_branch == "main"


def test_write_session_metadata_uses_atomic_replace(monkeypatch, tmp_path):
    import iac_code.services.session_metadata as session_metadata

    calls = []

    def fake_atomic_write_text(path, content, *, encoding="utf-8", durable=True, replace_attempts=3):
        calls.append((path, json.loads(content), encoding, durable, replace_attempts))
        path.write_text(content, encoding=encoding)

    monkeypatch.setattr(session_metadata, "atomic_write_text", fake_atomic_write_text, raising=False)
    session_dir = tmp_path / "session"

    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="s1", cwd=CWD, layout_version=SESSION_LAYOUT_VERSION_V2),
    )

    assert calls == [
        (
            session_dir / SESSION_METADATA_FILENAME,
            {"session_id": "s1", "cwd": CWD, "schema_version": 1, "layout_version": SESSION_LAYOUT_VERSION_V2},
            "utf-8",
            True,
            3,
        )
    ]


def test_existing_legacy_directory_session_without_metadata_stays_legacy(storage):
    session_dir = storage.session_dir(CWD, "legacy-dir")
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text("", encoding="utf-8")

    storage.append(CWD, "legacy-dir", Message(role="user", content="hello"), git_branch="main")

    metadata = storage.read_metadata(CWD, "legacy-dir")
    assert metadata is None


def test_append_into_empty_precreated_session_directory_writes_layout_v2_metadata(storage):
    session_dir = storage.session_dir(CWD, "empty-precreated")
    session_dir.mkdir(parents=True)

    storage.append(CWD, "empty-precreated", Message(role="user", content="hello"), git_branch="main")

    metadata = storage.read_metadata(CWD, "empty-precreated")
    assert metadata is not None
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2


def test_ensure_v2_session_dir_marks_empty_precreated_directory(storage):
    session_dir = storage.session_dir(CWD, "empty-precreated")
    session_dir.mkdir(parents=True)

    assert storage.ensure_v2_session_dir_for_new_session(CWD, "empty-precreated", git_branch="main") == session_dir

    metadata = storage.read_metadata(CWD, "empty-precreated")
    assert metadata is not None
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2
    assert metadata.git_branch == "main"


def test_long_cwd_legacy_project_remains_readable_after_bounded_project_exists(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    legacy_project_dir.mkdir(parents=True)
    legacy_path = legacy_project_dir / "legacy-long.jsonl"
    legacy_path.write_text('{"role":"user","content":"old","cwd":"%s"}\n' % cwd, encoding="utf-8")
    current_project_dir.mkdir(parents=True)
    storage = SessionStorage(projects_dir=tmp_path)

    loaded = storage.load(cwd, "legacy-long")

    assert [message.content for message in loaded] == ["old"]
    assert storage.exists(cwd, "legacy-long")


def test_delete_session_removes_all_long_project_aliases(tmp_path):
    cwd = "/" + "long-project/" * 24
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append(cwd, "duplicate", Message(role="user", content="prompt"), git_branch=None)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    shutil.copytree(current_project_dir, legacy_project_dir)

    assert storage.delete_session(cwd, "duplicate") is True
    assert not (current_project_dir / "duplicate" / SESSION_JSONL_FILENAME).exists()
    assert not (legacy_project_dir / "duplicate" / SESSION_JSONL_FILENAME).exists()
    assert not storage.exists(cwd, "duplicate")


def test_delete_session_removes_nested_legacy_conflict_sidecar_before_same_id_is_reused(storage):
    session_id = "legacy-delete-nested-sidecar"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    placeholder_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    placeholder_dir.write_text("placeholder collision\n", encoding="utf-8")
    nested_sidecar_dir = storage.session_dir(CWD, session_id)
    assert nested_sidecar_dir.name == f"{placeholder_dir.name}.conflict-sidecars"
    (nested_sidecar_dir / "pipeline").mkdir(parents=True)
    stale_pipeline = nested_sidecar_dir / "pipeline" / "meta.yaml"
    stale_pipeline.write_text("status: stale\n", encoding="utf-8")

    assert storage.delete_session(CWD, session_id) is True

    assert not nested_sidecar_dir.exists()
    legacy_path.write_text('{"role":"user","content":"fresh"}\n', encoding="utf-8")
    reused_sidecar_dir = storage.session_dir(CWD, session_id)
    assert reused_sidecar_dir == nested_sidecar_dir
    assert not (reused_sidecar_dir / "pipeline" / "meta.yaml").exists()


def test_delete_session_removes_primary_and_historical_legacy_conflict_sidecars(storage):
    session_id = "legacy-delete-both-sidecars"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")

    primary_sidecar_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    (primary_sidecar_dir / "pipeline").mkdir(parents=True)
    (primary_sidecar_dir / "pipeline" / "current.yaml").write_text("status: current\n", encoding="utf-8")
    historical_conflict_dir = storage._conflicting_sidecar_placeholder_dir(primary_sidecar_dir)
    (historical_conflict_dir / "pipeline").mkdir(parents=True)
    (historical_conflict_dir / "pipeline" / "stale.yaml").write_text("status: stale\n", encoding="utf-8")

    assert storage.delete_session(CWD, session_id) is True

    assert not primary_sidecar_dir.exists()
    assert not historical_conflict_dir.exists()


def test_delete_session_removes_legacy_sidecars_with_web_metadata(storage):
    session_id = "legacy-delete-web-metadata"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")

    sidecar_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    (sidecar_dir / "pipeline").mkdir(parents=True)
    (sidecar_dir / "pipeline" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (sidecar_dir / "web-session.json").write_text('{"mode":"pipeline"}\n', encoding="utf-8")
    (sidecar_dir / "web-session.json.tmp").write_text('{"mode":"pipeline"}\n', encoding="utf-8")

    assert storage.delete_session(CWD, session_id) is True

    assert not legacy_path.exists()
    assert not sidecar_dir.exists()


def test_delete_session_removes_atomic_web_metadata_temp_sidecar(storage):
    session_id = "legacy-delete-atomic-web-metadata"
    legacy_path = storage.legacy_session_path(CWD, session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")

    sidecar_dir = storage._legacy_sidecar_placeholder_dir(legacy_path)
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "web-session.json").write_text('{"mode":"normal"}\n', encoding="utf-8")
    (sidecar_dir / ".web-session.json.deadbeef.tmp").write_text('{"mode":"normal"}\n', encoding="utf-8")

    assert storage.delete_session(CWD, session_id) is True

    assert not legacy_path.exists()
    assert not sidecar_dir.exists()


def test_delete_session_ignores_symlinked_project_alias(tmp_path):
    projects_dir = tmp_path / "projects"
    outside_project = tmp_path / "outside-project"
    session_dir = outside_project / "victim"
    session_dir.mkdir(parents=True)
    session_file = session_dir / SESSION_JSONL_FILENAME
    session_file.write_text('{"role":"user","content":"outside"}\n', encoding="utf-8")

    cwd = "/symlinked/project"
    project_alias = project_paths.project_dir_candidates(cwd, projects_dir)[0]
    project_alias.parent.mkdir(parents=True, exist_ok=True)
    _symlink_or_skip(outside_project, project_alias)
    storage = SessionStorage(projects_dir=projects_dir)

    assert storage.delete_session(cwd, "victim") is False
    assert session_file.exists()


def test_delete_session_ignores_reparse_point_project_alias(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    cwd = "/junction/project"
    project_alias = project_paths.project_dir_candidates(cwd, projects_dir)[0]
    session_dir = project_alias / "victim"
    session_dir.mkdir(parents=True)
    session_file = session_dir / SESSION_JSONL_FILENAME
    session_file.write_text('{"role":"user","content":"junction"}\n', encoding="utf-8")
    storage = SessionStorage(projects_dir=projects_dir)
    original = SessionStorage._is_reparse_point
    monkeypatch.setattr(
        SessionStorage,
        "_is_reparse_point",
        staticmethod(lambda path: path == project_alias or original(path)),
    )

    assert storage.delete_session(cwd, "victim") is False
    assert session_file.exists()


def test_long_cwd_legacy_directory_session_dir_not_shadowed_by_new_sidecar(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    current_sidecar_dir = current_project_dir / "legacy-dir"
    (current_sidecar_dir / "pipeline").mkdir(parents=True)
    (current_sidecar_dir / "pipeline" / "events.jsonl").write_text("", encoding="utf-8")
    legacy_session_dir = legacy_project_dir / "legacy-dir"
    legacy_session_dir.mkdir(parents=True)
    (legacy_session_dir / SESSION_JSONL_FILENAME).write_text(
        '{"role":"user","content":"old","cwd":"%s"}\n' % cwd,
        encoding="utf-8",
    )
    storage = SessionStorage(projects_dir=tmp_path)

    assert storage.session_dir(cwd, "legacy-dir") == legacy_session_dir
    assert [message.content for message in storage.load(cwd, "legacy-dir")] == ["old"]


def test_long_cwd_legacy_sidecar_only_marked_before_bounded_project_shadow(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    legacy_session_dir = legacy_project_dir / "legacy-sidecar-only"
    (legacy_session_dir / "pipeline").mkdir(parents=True)
    (legacy_session_dir / "pipeline" / "events.jsonl").write_text("", encoding="utf-8")
    storage = SessionStorage(projects_dir=tmp_path)

    session_dir = storage.ensure_v2_session_dir_for_new_session(cwd, "legacy-sidecar-only", git_branch="main")

    assert session_dir == legacy_session_dir
    assert (legacy_session_dir / SESSION_METADATA_FILENAME).exists()
    assert not (current_project_dir / "legacy-sidecar-only" / SESSION_METADATA_FILENAME).exists()


def test_long_cwd_legacy_sidecar_state_wins_over_current_metadata_only_shadow(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    session_id = "legacy-sidecar-shadow"
    current_session_dir = current_project_dir / session_id
    legacy_session_dir = legacy_project_dir / session_id
    write_session_metadata(
        current_session_dir,
        SessionMetadata(session_id=session_id, cwd=cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    (legacy_session_dir / "pipeline").mkdir(parents=True)
    (legacy_session_dir / "pipeline" / "events.jsonl").write_text("", encoding="utf-8")
    storage = SessionStorage(projects_dir=tmp_path)

    assert storage.v2_session_dir(cwd, session_id) is None
    assert storage.session_dir(cwd, session_id) == legacy_session_dir
    assert storage.ensure_v2_session_dir_for_new_session(cwd, session_id, git_branch="main") == legacy_session_dir
    assert storage.session_dir(cwd, session_id) == legacy_session_dir


def test_long_cwd_legacy_sidecar_state_wins_over_empty_current_directory(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    session_id = "legacy-sidecar-empty-shadow"
    current_session_dir = current_project_dir / session_id
    legacy_session_dir = legacy_project_dir / session_id
    current_session_dir.mkdir(parents=True)
    (legacy_session_dir / "pipeline").mkdir(parents=True)
    (legacy_session_dir / "pipeline" / "events.jsonl").write_text("", encoding="utf-8")
    storage = SessionStorage(projects_dir=tmp_path)

    assert storage.session_dir(cwd, session_id) == legacy_session_dir
    assert storage.ensure_v2_session_dir_for_new_session(cwd, session_id, git_branch="main") == legacy_session_dir
    assert not (current_session_dir / SESSION_METADATA_FILENAME).exists()


def test_new_session_dir_uses_bounded_project_even_when_legacy_long_project_exists(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    legacy_project_dir.mkdir(parents=True)
    storage = SessionStorage(projects_dir=tmp_path)

    session_dir = storage.ensure_v2_session_dir_for_new_session(cwd, "new-long", git_branch="main")

    assert session_dir == current_project_dir / "new-long"
    assert (session_dir / SESSION_METADATA_FILENAME).exists()


def test_append_new_session_uses_bounded_project_even_when_legacy_long_project_exists(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    legacy_project_dir.mkdir(parents=True)
    storage = SessionStorage(projects_dir=tmp_path)

    storage.append(cwd, "new-long-append", Message(role="user", content="new"), git_branch=None)

    assert storage.session_path(cwd, "new-long-append") == current_project_dir / "new-long-append" / "session.jsonl"
    assert not (legacy_project_dir / "new-long-append" / "session.jsonl").exists()


def test_metadata_only_shadow_does_not_hide_legacy_file_for_cross_project_lookup(storage):
    legacy_path = storage.legacy_session_path(CWD, "same-id-shadow")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy","cwd":"%s"}\n' % CWD, encoding="utf-8")
    session_dir = legacy_path.parent / "same-id-shadow"
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="same-id-shadow",
            cwd="/shadow",
            name="shadow",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )

    assert storage.find_session_anywhere("same-id-shadow") == (CWD, legacy_path)


def test_cross_project_lookup_ignores_metadata_only_shadow_across_project_dir_candidates(storage):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, storage._projects_dir)
    legacy_project_dir.mkdir(parents=True)
    legacy_path = legacy_project_dir / "legacy-long-shadow.jsonl"
    legacy_path.write_text('{"role":"user","content":"legacy","cwd":"%s"}\n' % cwd, encoding="utf-8")
    shadow_dir = current_project_dir / "legacy-long-shadow"
    write_session_metadata(
        shadow_dir,
        SessionMetadata(
            session_id="legacy-long-shadow",
            cwd=cwd,
            name="shadow",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )

    assert storage.find_session_anywhere("legacy-long-shadow") == (cwd, legacy_path)


def test_cross_project_lookup_ignores_metadata_only_shadow_with_stale_metadata_cwd(storage):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, storage._projects_dir)
    legacy_project_dir.mkdir(parents=True)
    legacy_path = legacy_project_dir / "legacy-long-stale-shadow.jsonl"
    legacy_path.write_text('{"role":"user","content":"legacy","cwd":"%s"}\n' % cwd, encoding="utf-8")
    shadow_dir = current_project_dir / "legacy-long-stale-shadow"
    write_session_metadata(
        shadow_dir,
        SessionMetadata(
            session_id="legacy-long-stale-shadow",
            cwd="/stale-cwd",
            name="shadow",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )

    assert storage.find_session_anywhere("legacy-long-stale-shadow") == (cwd, legacy_path)


def test_latest_session_ignores_metadata_only_shadow_when_same_id_legacy_exists(storage):
    import os

    legacy_path = storage.legacy_session_path(CWD, "same-id-latest-shadow")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy","cwd":"%s"}\n' % CWD, encoding="utf-8")
    session_dir = legacy_path.parent / "same-id-latest-shadow"
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="same-id-latest-shadow",
            cwd="/shadow",
            name="shadow",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    os.utime(metadata_path, (metadata_path.stat().st_atime, legacy_path.stat().st_mtime + 100))

    assert storage.get_latest_session_anywhere() == (CWD, "same-id-latest-shadow")


def test_latest_session_ignores_metadata_only_shadow_across_project_dir_candidates(storage):
    import os

    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, storage._projects_dir)
    legacy_project_dir.mkdir(parents=True)
    legacy_path = legacy_project_dir / "latest-long-shadow.jsonl"
    legacy_path.write_text('{"role":"user","content":"legacy","cwd":"%s"}\n' % cwd, encoding="utf-8")
    shadow_dir = current_project_dir / "latest-long-shadow"
    write_session_metadata(
        shadow_dir,
        SessionMetadata(
            session_id="latest-long-shadow",
            cwd=cwd,
            name="shadow",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )
    metadata_path = shadow_dir / SESSION_METADATA_FILENAME
    os.utime(metadata_path, (metadata_path.stat().st_atime, legacy_path.stat().st_mtime + 100))

    assert storage.get_latest_session_anywhere() == (cwd, "latest-long-shadow")


def test_latest_session_ignores_metadata_only_shadow_with_stale_metadata_cwd(storage):
    import os

    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, storage._projects_dir)
    legacy_project_dir.mkdir(parents=True)
    legacy_path = legacy_project_dir / "latest-long-stale-shadow.jsonl"
    legacy_path.write_text('{"role":"user","content":"legacy","cwd":"%s"}\n' % cwd, encoding="utf-8")
    shadow_dir = current_project_dir / "latest-long-stale-shadow"
    write_session_metadata(
        shadow_dir,
        SessionMetadata(
            session_id="latest-long-stale-shadow",
            cwd="/stale-cwd",
            name="shadow",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )
    metadata_path = shadow_dir / SESSION_METADATA_FILENAME
    os.utime(metadata_path, (metadata_path.stat().st_atime, legacy_path.stat().st_mtime + 100))

    assert storage.get_latest_session_anywhere() == (cwd, "latest-long-stale-shadow")


def test_latest_session_keeps_metadata_only_when_same_id_legacy_is_in_different_project(storage):
    import os

    legacy_path = storage.legacy_session_path("/legacy-project", "same-id-cross-project")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy","cwd":"/legacy-project"}\n', encoding="utf-8")
    session_dir = storage.ensure_v2_session_dir_for_new_session(
        "/metadata-project",
        "same-id-cross-project",
        git_branch="main",
    )
    assert session_dir is not None
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    os.utime(metadata_path, (metadata_path.stat().st_atime, legacy_path.stat().st_mtime + 100))

    assert storage.get_latest_session_anywhere() == ("/metadata-project", "same-id-cross-project")


def test_metadata_only_mismatched_session_id_is_ignored_by_cross_project_lookup(storage):
    session_dir = storage.session_dir(CWD, "requested")
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="different",
            cwd=CWD,
            name="wrong-id",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )

    assert storage.find_session_anywhere("requested") is None
    assert storage.get_latest_session_anywhere() is None
    assert storage.exists(CWD, "requested") is False


def test_metadata_only_mismatched_future_session_id_is_ignored(storage):
    session_dir = storage.session_dir(CWD, "requested-future")
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="different-future",
            cwd=CWD,
            name="wrong-id",
            layout_version=99,
        ),
    )

    assert storage.exists(CWD, "requested-future") is False
    assert storage.read_metadata(CWD, "requested-future") is None
    assert storage.find_session_anywhere("requested-future") is None


def test_directory_session_mismatched_metadata_is_not_current_session(storage):
    session_dir = storage.session_dir(CWD, "requested-dir")
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"wrong"}\n', encoding="utf-8")
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="different-dir",
            cwd=CWD,
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )

    assert storage.exists(CWD, "requested-dir") is False
    assert storage.load(CWD, "requested-dir") == []
    assert storage.v2_session_dir(CWD, "requested-dir") is None
    assert storage.session_path(CWD, "requested-dir") != session_dir / SESSION_JSONL_FILENAME
    assert storage.session_dir(CWD, "requested-dir") != session_dir
    assert storage.find_session_anywhere("requested-dir") is None
    assert storage.get_latest_session_anywhere() is None


def test_directory_session_mismatched_future_metadata_is_ignored(storage):
    session_dir = storage.session_dir(CWD, "requested-dir-future")
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"wrong"}\n', encoding="utf-8")
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="different-dir-future",
            cwd=CWD,
            layout_version=99,
        ),
    )

    assert storage.exists(CWD, "requested-dir-future") is False
    assert storage.load(CWD, "requested-dir-future") == []
    assert storage.find_session_anywhere("requested-dir-future") is None
    assert storage.get_latest_session_anywhere() is None


@pytest.mark.parametrize("layout_version", [SESSION_LAYOUT_VERSION_V2, 99])
def test_write_refuses_mismatched_metadata_before_mutating(storage, layout_version):
    session_dir = storage.session_dir(CWD, f"requested-write-{layout_version}")
    session_dir.mkdir(parents=True)
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id=f"different-write-{layout_version}",
            cwd=CWD,
            layout_version=layout_version,
        ),
    )
    metadata_before = (session_dir / SESSION_METADATA_FILENAME).read_text(encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        storage.append(
            CWD,
            f"requested-write-{layout_version}",
            Message(role="assistant", content="new"),
            git_branch="main",
        )

    assert not (session_dir / SESSION_JSONL_FILENAME).exists()
    assert (session_dir / SESSION_METADATA_FILENAME).read_text(encoding="utf-8") == metadata_before


def test_write_refuses_invalid_metadata_only_before_mutating(storage):
    session_dir = storage.session_dir(CWD, "invalid-metadata-write")
    session_dir.mkdir(parents=True)
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    metadata_path.write_text('{"name":"missing-session-id"}\n', encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        storage.append(CWD, "invalid-metadata-write", Message(role="assistant", content="new"), git_branch="main")

    assert not (session_dir / SESSION_JSONL_FILENAME).exists()
    assert metadata_path.read_text(encoding="utf-8") == '{"name":"missing-session-id"}\n'


def test_write_refuses_symlinked_metadata_before_mutating(storage, tmp_path):
    session_id = "symlinked-metadata-write"
    session_dir = storage.session_dir(CWD, session_id)
    session_dir.mkdir(parents=True)
    target = tmp_path / "outside-metadata.json"
    target.write_text(
        '{"session_id":"symlinked-metadata-write","layout_version":2}\n',
        encoding="utf-8",
    )
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    _symlink_or_skip(target, metadata_path)

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        storage.append(CWD, session_id, Message(role="assistant", content="new"), git_branch="main")

    assert metadata_path.is_symlink()
    assert not (session_dir / SESSION_JSONL_FILENAME).exists()


def test_usage_load_reads_legacy_long_project_after_bounded_project_exists(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    usage_dir = legacy_project_dir / "sid"
    usage_dir.mkdir(parents=True)
    (usage_dir / "usage.jsonl").write_text(
        '{"type":"usage","input_tokens":7,"output_tokens":3}\n',
        encoding="utf-8",
    )
    current_project_dir.mkdir(parents=True)
    store = SessionUsageStore(projects_dir=tmp_path)

    totals = store.load(cwd, "sid")

    assert totals.input_tokens == 7
    assert totals.output_tokens == 3


def test_usage_append_writes_existing_legacy_long_v2_session_dir_after_bounded_project_exists(tmp_path):
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, *_, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    session_dir = legacy_project_dir / "sid"
    session_dir.mkdir(parents=True)
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="sid", cwd=cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    (session_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    current_project_dir.mkdir(parents=True)
    store = SessionUsageStore(projects_dir=tmp_path)

    assert store.append(cwd, "sid", Usage(input_tokens=4, output_tokens=2))

    assert (session_dir / "usage.jsonl").exists()
    assert not (current_project_dir / "sid" / "usage.jsonl").exists()


def test_ensure_v2_session_dir_marks_sidecar_only_directory(storage):
    session_dir = storage.session_dir(CWD, "sidecar-only")
    (session_dir / "pipeline").mkdir(parents=True)
    (session_dir / "pipeline" / "events.jsonl").write_text("", encoding="utf-8")

    assert storage.ensure_v2_session_dir_for_new_session(CWD, "sidecar-only", git_branch="main") == session_dir

    metadata = storage.read_metadata(CWD, "sidecar-only")
    assert metadata is not None
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2
    assert metadata.git_branch == "main"


def test_sidecar_only_directory_without_metadata_is_not_resumable(storage):
    session_dir = storage.session_dir(CWD, "sidecar-only-unmarked")
    (session_dir / "pipeline").mkdir(parents=True)
    (session_dir / "pipeline" / "events.jsonl").write_text("", encoding="utf-8")

    assert storage.exists(CWD, "sidecar-only-unmarked") is False


def test_ensure_v2_session_dir_marks_session_owned_runtime_sidecar_directory(storage):
    session_dir = storage.session_dir(CWD, "runtime-sidecars")
    session_dir.mkdir(parents=True)
    (session_dir / "usage.jsonl").write_text("", encoding="utf-8")
    (session_dir / ".usage.jsonl.lock").write_text("", encoding="utf-8")
    (session_dir / "permission-audit.jsonl").write_text("", encoding="utf-8")
    (session_dir / "tool-results").mkdir()
    (session_dir / "image-cache").mkdir()

    assert storage.ensure_v2_session_dir_for_new_session(CWD, "runtime-sidecars", git_branch="main") == session_dir

    metadata = storage.read_metadata(CWD, "runtime-sidecars")
    assert metadata is not None
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2


@pytest.mark.parametrize(
    ("name", "is_dir"),
    [
        ("usage.jsonl", True),
        (".usage.jsonl.lock", True),
        ("permission-audit.jsonl", True),
        ("tool-results", False),
        ("image-cache", False),
    ],
)
def test_ensure_v2_session_dir_rejects_sidecar_name_with_wrong_type(storage, name, is_dir):
    session_id = f"bad-sidecar-{name}"
    session_dir = storage.session_dir(CWD, session_id)
    session_dir.mkdir(parents=True)
    path = session_dir / name
    if is_dir:
        path.mkdir()
    else:
        path.write_text("", encoding="utf-8")

    assert storage.ensure_v2_session_dir_for_new_session(CWD, session_id, git_branch="main") is None
    assert storage.read_metadata(CWD, session_id) is None


@pytest.mark.parametrize("operation", ["append", "append_meta", "save"])
def test_write_paths_refuse_unsupported_layout_before_mutating(storage, operation):
    session_dir = storage.session_dir(CWD, f"future-{operation}")
    session_dir.mkdir(parents=True)
    session_path = session_dir / SESSION_JSONL_FILENAME
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    session_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=f"future-{operation}", cwd=CWD, layout_version=99),
    )
    before_session = session_path.read_text(encoding="utf-8")
    before_metadata = metadata_path.read_text(encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        if operation == "append":
            storage.append(CWD, f"future-{operation}", Message(role="assistant", content="new"), git_branch="main")
        elif operation == "append_meta":
            storage.append_meta(CWD, f"future-{operation}", {"type": "last-prompt", "last_prompt": "new"})
        else:
            storage.save(CWD, f"future-{operation}", [Message(role="user", content="new")], git_branch="main")

    assert session_path.read_text(encoding="utf-8") == before_session
    assert metadata_path.read_text(encoding="utf-8") == before_metadata


@pytest.mark.parametrize("operation", ["append", "append_meta", "save"])
def test_write_paths_refuse_metadata_only_unsupported_layout_before_mutating(storage, operation):
    session_dir = storage.session_dir(CWD, f"future-metadata-only-{operation}")
    session_dir.mkdir(parents=True)
    session_path = session_dir / SESSION_JSONL_FILENAME
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=f"future-metadata-only-{operation}", cwd=CWD, layout_version=99),
    )
    before_metadata = metadata_path.read_text(encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        if operation == "append":
            storage.append(
                CWD,
                f"future-metadata-only-{operation}",
                Message(role="assistant", content="new"),
                git_branch="main",
            )
        elif operation == "append_meta":
            storage.append_meta(
                CWD,
                f"future-metadata-only-{operation}",
                {"type": "last-prompt", "last_prompt": "new"},
            )
        else:
            storage.save(
                CWD,
                f"future-metadata-only-{operation}",
                [Message(role="user", content="new")],
                git_branch="main",
            )

    assert not session_path.exists()
    assert metadata_path.read_text(encoding="utf-8") == before_metadata


@pytest.mark.parametrize("operation", ["load", "exists"])
def test_read_paths_refuse_unsupported_layout_before_reading(storage, operation):
    session_dir = storage.session_dir(CWD, f"future-read-{operation}")
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=f"future-read-{operation}", cwd=CWD, layout_version=99),
    )

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        if operation == "load":
            storage.load(CWD, f"future-read-{operation}")
        else:
            storage.exists(CWD, f"future-read-{operation}")


def test_find_session_anywhere_refuses_unsupported_directory_layout(storage):
    session_dir = storage.session_dir(CWD, "future-find")
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text(
        '{"role":"user","content":"old","cwd":"/tmp/proj-x"}\n',
        encoding="utf-8",
    )
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="future-find", cwd=CWD, layout_version=99),
    )

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        storage.find_session_anywhere("future-find")


def test_find_session_anywhere_prefers_v2_metadata_cwd(storage):
    session_dir = storage.session_dir("/metadata-cwd", "metadata-cwd-session")
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"legacy row"}\n', encoding="utf-8")
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="metadata-cwd-session",
            cwd="/metadata-cwd",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )

    found = storage.find_session_anywhere("metadata-cwd-session")

    assert found == ("/metadata-cwd", session_dir / SESSION_JSONL_FILENAME)


def test_get_latest_session_anywhere_uses_metadata_only_v2_session(storage):
    session_dir = storage.ensure_v2_session_dir_for_new_session(
        "/metadata-only-latest",
        "latest-meta",
        git_branch="main",
    )
    assert session_dir is not None

    assert storage.get_latest_session_anywhere() == ("/metadata-only-latest", "latest-meta")


def test_get_latest_session_anywhere_refuses_unsupported_directory_layout(storage):
    session_dir = storage.session_dir(CWD, "future-latest")
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text(
        '{"role":"user","content":"old","cwd":"/tmp/proj-x"}\n',
        encoding="utf-8",
    )
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="future-latest", cwd=CWD, layout_version=99),
    )

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        storage.get_latest_session_anywhere()


def test_rename_session_preserves_layout_version_for_v2_sessions(storage):
    storage.append(CWD, "rename-v2", Message(role="user", content="hello"), git_branch="main")

    result = storage.rename_session(CWD, "rename-v2", "deploy-prod", git_branch="feature")

    metadata = storage.read_metadata(CWD, "rename-v2")
    assert result == "renamed"
    assert metadata is not None
    assert metadata.name == "deploy-prod"
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2


def test_rename_new_directory_session_writes_layout_v2_metadata(storage):
    result = storage.rename_session(CWD, "rename-new", "deploy-prod", git_branch="main")

    metadata = storage.read_metadata(CWD, "rename-new")
    assert result == "renamed"
    assert metadata is not None
    assert metadata.name == "deploy-prod"
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2


def test_rename_session_refuses_unsupported_layout_before_modifying_metadata(storage):
    session_dir = storage.session_dir(CWD, "future-rename")
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="future-rename", name="old-name", cwd=CWD, layout_version=99),
    )
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    before_metadata = metadata_path.read_text(encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        storage.rename_session(CWD, "future-rename", "deploy-prod", git_branch="main")

    assert metadata_path.read_text(encoding="utf-8") == before_metadata


def test_rename_session_refuses_metadata_only_unsupported_layout_before_mutating(storage):
    session_dir = storage.session_dir(CWD, "future-metadata-only-rename")
    session_dir.mkdir(parents=True)
    session_path = session_dir / SESSION_JSONL_FILENAME
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="future-metadata-only-rename", name="old-name", cwd=CWD, layout_version=99),
    )
    before_metadata = metadata_path.read_text(encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        storage.rename_session(CWD, "future-metadata-only-rename", "deploy-prod", git_branch="main")

    assert not session_path.exists()
    assert metadata_path.read_text(encoding="utf-8") == before_metadata
