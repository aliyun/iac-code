from __future__ import annotations

import json
from pathlib import Path

from iac_code import __version__
from iac_code.agent.message import Message, TextBlock, ToolUseBlock
from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage


def test_append_and_load_roundtrip(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"), git_branch="main")
    storage.append(
        "/repo",
        "transcript_att_0001",
        Message(role="assistant", content=[TextBlock(text="hi")]),
        git_branch="main",
    )

    messages = storage.load("/repo", "transcript_att_0001")

    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "hello"
    assert messages[1].get_text() == "hi"


def test_transcript_lives_inside_sidecar(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"))

    assert storage.session_path("/repo", "transcript_att_0001") == (
        tmp_path / "pipeline" / "transcripts" / "transcript_att_0001" / "session.jsonl"
    )
    assert storage.session_dir("/repo", "transcript_att_0001") == (
        tmp_path / "pipeline" / "transcripts" / "transcript_att_0001"
    )


def test_load_skips_lite_meta_rows(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    path = storage.session_path("/repo", "transcript_att_0001")
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"type": "pipeline_init", "session_id": "transcript_att_0001"}\n'
        '{"role": "user", "content": "real", "session_id": "transcript_att_0001"}\n',
        encoding="utf-8",
    )

    messages = storage.load("/repo", "transcript_att_0001")

    assert len(messages) == 1
    assert messages[0].content == "real"


def test_append_stamps_required_fields(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"), git_branch="main")

    row = json.loads(storage.session_path("/repo", "transcript_att_0001").read_text(encoding="utf-8"))
    assert row["session_id"] == "transcript_att_0001"
    assert row["cwd"] == "/repo"
    assert row["git_branch"] == "main"
    assert row["version"] == __version__


def test_append_meta_requires_type(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    try:
        storage.append_meta("/repo", "transcript_att_0001", {"last_prompt": "missing type"})
    except ValueError as exc:
        assert "meta_entry must include a 'type' field" in str(exc)
    else:
        raise AssertionError("meta row without type was accepted")


def test_append_meta_stamps_session_and_is_skipped_by_load(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.append_meta("/repo", "transcript_att_0001", {"type": "pipeline_init"})

    row = json.loads(storage.session_path("/repo", "transcript_att_0001").read_text(encoding="utf-8"))
    assert row == {"type": "pipeline_init", "session_id": "transcript_att_0001"}
    assert storage.load("/repo", "transcript_att_0001") == []


def test_save_overwrites_and_stamps_messages(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    storage.append("/repo", "transcript_att_0001", Message(role="user", content="old"), git_branch="old-branch")

    storage.save(
        "/repo",
        "transcript_att_0001",
        [
            Message(role="user", content="new"),
            Message(role="assistant", content=[TextBlock(text="saved")]),
        ],
        git_branch="main",
    )

    rows = [
        json.loads(line)
        for line in storage.session_path("/repo", "transcript_att_0001").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert [row["content"] for row in rows] == ["new", [{"type": "text", "text": "saved"}]]
    assert all(row["session_id"] == "transcript_att_0001" for row in rows)
    assert all(row["cwd"] == "/repo" for row in rows)
    assert all(row["git_branch"] == "main" for row in rows)
    assert all(row["version"] == __version__ for row in rows)

    messages = storage.load("/repo", "transcript_att_0001")
    assert [message.get_text() for message in messages] == ["new", "saved"]


def test_exists_tracks_session_path(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    assert storage.exists("/repo", "transcript_att_0001") is False

    storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"))

    assert storage.exists("/repo", "transcript_att_0001") is True


def test_repair_interrupted_delegates_to_session_storage(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    messages = [
        Message(role="user", content="run tool"),
        Message(role="assistant", content=[ToolUseBlock(id="tu_1", name="complete_step", input={})]),
    ]

    repaired = storage.repair_interrupted(messages)

    assert len(repaired) == 3
    assert repaired[-1].role == "user"
    assert repaired[-1].content[0].tool_use_id == "tu_1"
    assert repaired[-1].content[0].is_error is True


def test_rejects_unsafe_transcript_id(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    try:
        storage.append("/repo", "../escape", Message(role="user", content="bad"))
    except ValueError as exc:
        assert "unsafe transcript id" in str(exc)
    else:
        raise AssertionError("unsafe transcript id was accepted")
