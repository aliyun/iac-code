import json
from pathlib import Path

import pytest

from iac_code.services.session_layout import (
    SESSION_LAYOUT_VERSION_V2,
    SessionPaths,
    UnsupportedSessionLayoutError,
    ensure_session_owned_dir,
    is_supported_session_dir_for_id,
    session_layout_version,
)
from iac_code.services.session_metadata import SESSION_METADATA_FILENAME, SessionMetadata, write_session_metadata


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


def test_missing_layout_version_is_legacy(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    write_session_metadata(session_dir, SessionMetadata(session_id="s1", cwd="/repo"))

    assert session_layout_version(session_dir) is None


def test_layout_v2_paths_are_session_scoped(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )

    paths = SessionPaths.from_session_dir(session_dir)

    assert paths.session_dir == session_dir
    assert paths.image_cache_dir == session_dir / "image-cache"
    assert paths.tool_results_dir == session_dir / "tool-results"
    assert paths.usage_path == session_dir / "usage.jsonl"
    assert paths.permission_audit_path == session_dir / "permission-audit.jsonl"
    assert paths.a2a_artifacts_dir == session_dir / "a2a" / "artifacts"
    assert paths.transcript_dir("transcript_att_0001") == (
        session_dir / "pipeline" / "transcripts" / "transcript_att_0001"
    )
    assert paths.transcript_usage_path("transcript_att_0001") == (
        session_dir / "pipeline" / "transcripts" / "transcript_att_0001" / "usage.jsonl"
    )
    assert paths.transcript_permission_audit_path("transcript_att_0001") == (
        session_dir / "pipeline" / "transcripts" / "transcript_att_0001" / "permission-audit.jsonl"
    )
    assert paths.transcript_tool_results_dir("transcript_att_0001") == (
        session_dir / "pipeline" / "transcripts" / "transcript_att_0001" / "tool-results"
    )


@pytest.mark.parametrize(
    "bad_id",
    ["", ".", "..", "../x", "a/b", "a\\b", "bad id", "CON", "NUL.txt", "COM1", "LPT9.log", "step.", "step "],
)
def test_transcript_paths_reject_unsafe_ids(tmp_path: Path, bad_id: str) -> None:
    paths = SessionPaths.from_session_dir(tmp_path / "projects" / "proj" / "s1")

    with pytest.raises(ValueError, match="unsafe transcript id"):
        paths.transcript_dir(bad_id)


def test_unknown_layout_version_is_not_legacy(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    write_session_metadata(session_dir, SessionMetadata(session_id="s1", cwd="/repo", layout_version=99))

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        SessionPaths.require_supported(session_dir)


def test_unknown_layout_version_with_valid_metadata_is_not_legacy(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    write_session_metadata(session_dir, SessionMetadata(session_id="s1", cwd="/repo", layout_version=99))

    assert session_layout_version(session_dir) == 99
    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        SessionPaths.require_supported(session_dir)


def test_ensure_session_owned_dir_rejects_symlink_leaf(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(outside, session_dir / "image-cache", target_is_directory=True)

    with pytest.raises(UnsupportedSessionLayoutError, match="session-owned path"):
        ensure_session_owned_dir(session_dir, session_dir / "image-cache")


def test_ensure_session_owned_dir_rejects_symlink_parent(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(outside, session_dir / "pipeline", target_is_directory=True)

    with pytest.raises(UnsupportedSessionLayoutError, match="session-owned path"):
        ensure_session_owned_dir(session_dir, session_dir / "pipeline" / "transcripts")


def test_ensure_session_owned_dir_rejects_reparse_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import iac_code.services.session_layout as session_layout

    session_dir = tmp_path / "projects" / "proj" / "s1"
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    target = session_dir / "tool-results"
    target.mkdir()

    monkeypatch.setattr(session_layout, "_is_reparse_point", lambda path: path == target)

    with pytest.raises(UnsupportedSessionLayoutError, match="session-owned path"):
        ensure_session_owned_dir(session_dir, target)


@pytest.mark.parametrize(
    "metadata_text",
    [
        "{not-json}\n",
        json.dumps(["not", "an", "object"]) + "\n",
        json.dumps({"layout_version": 2}) + "\n",
        json.dumps({"layout_version": 99}) + "\n",
        json.dumps({"layout_version": "2"}) + "\n",
    ],
)
def test_invalid_layout_metadata_is_not_legacy(tmp_path: Path, metadata_text: str) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_METADATA_FILENAME).write_text(metadata_text, encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        SessionPaths.require_supported(session_dir)


def test_metadata_symlink_is_unsupported_even_when_target_is_valid(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    session_dir.mkdir(parents=True)
    target = tmp_path / "outside-metadata.json"
    target.write_text(
        json.dumps({"session_id": "s1", "layout_version": SESSION_LAYOUT_VERSION_V2}) + "\n",
        encoding="utf-8",
    )
    _symlink_or_skip(target, session_dir / SESSION_METADATA_FILENAME)

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        SessionPaths.require_supported(session_dir)
    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        is_supported_session_dir_for_id(session_dir, "s1")


def test_dangling_metadata_symlink_is_unsupported(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "proj" / "s1"
    session_dir.mkdir(parents=True)
    _symlink_or_skip(tmp_path / "missing-metadata.json", session_dir / SESSION_METADATA_FILENAME)

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        SessionPaths.require_supported(session_dir)


def test_metadata_reparse_point_is_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import iac_code.services.session_metadata as session_metadata

    session_dir = tmp_path / "projects" / "proj" / "s1"
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    monkeypatch.setattr(session_metadata, "_is_reparse_point", lambda path: path == metadata_path)

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        SessionPaths.require_supported(session_dir)
