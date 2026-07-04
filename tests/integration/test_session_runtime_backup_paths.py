from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from iac_code.a2a.artifacts import artifact_store_for_session
from iac_code.agent.message import Message
from iac_code.mcp.output import convert_mcp_tool_result
from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
from iac_code.services.permissions.audit import (
    PermissionAuditRecord,
    PermissionAuditSettings,
    emit_permission_audit,
)
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.session_layout import SessionPaths
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2
from iac_code.services.session_storage import SessionStorage
from iac_code.services.session_usage import SessionUsageStore
from iac_code.types.stream_events import Usage
from iac_code.utils.image.pasted_content import PastedContent
from iac_code.utils.image.store import ImageStore


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_same_file(source_root: Path, mirror_root: Path, relative: str, expected: bytes | None = None) -> None:
    source = source_root / relative
    mirror = mirror_root / relative
    assert source.is_file(), relative
    assert mirror.is_file(), relative
    assert mirror.read_bytes() == source.read_bytes()
    if expected is not None:
        assert mirror.read_bytes() == expected


def test_v2_session_runtime_paths_are_session_owned_and_backup_mirrored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())

    cwd = str(tmp_path / "repo")
    session_id = "session-e2e"
    transcript_id = "transcript_att_0001"
    storage = SessionStorage(projects_dir=config_dir / "projects")
    storage.append(cwd, session_id, Message(role="user", content="create a stack"), git_branch="main")

    session_dir = storage.session_dir(cwd, session_id)
    session_paths = SessionPaths.require_supported(session_dir)
    assert storage.read_metadata(cwd, session_id).layout_version == SESSION_LAYOUT_VERSION_V2

    usage_store = SessionUsageStore(path_provider=lambda _cwd, _session_id: session_paths.usage_path)
    assert usage_store.append(cwd, session_id, Usage(input_tokens=11, output_tokens=7), provider="test", model="m")

    assert emit_permission_audit(
        PermissionAuditRecord(
            session_id=session_id,
            cwd=cwd,
            tool_name="aliyun_ros_stack",
            tool_use_id="tool-root",
            decision="allow",
            scope="once",
            source="test",
        ),
        settings=PermissionAuditSettings(max_file_bytes=1024, max_files=1),
    )

    stored_image = ImageStore(session_id, session_root=session_dir).store(
        PastedContent(
            id=1,
            type="image",
            content=base64.b64encode(b"root-image").decode("ascii"),
            media_type="image/png",
        )
    )
    assert stored_image == str(session_paths.image_cache_dir / "1.png")

    root_mcp_result = convert_mcp_tool_result(
        {
            "content": [
                {
                    "type": "image",
                    "data": base64.b64encode(b"root-tool-result").decode("ascii"),
                    "mimeType": "image/png",
                }
            ]
        },
        server_name="ros",
        tool_name="render_template",
        session_id=session_id,
        session_dir=session_dir,
    )
    root_tool_artifact = Path(root_mcp_result.metadata["mcp"]["artifacts"][0]["path"])

    transcript_storage = PipelineTranscriptStorage(session_dir / "pipeline")
    transcript_storage.append(cwd, transcript_id, Message(role="assistant", content="step output"))
    transcript_dir = session_paths.transcript_dir(transcript_id)
    transcript_usage_store = SessionUsageStore(path_provider=lambda _cwd, _session_id: transcript_dir / "usage.jsonl")
    assert transcript_usage_store.append(
        cwd,
        transcript_id,
        Usage(input_tokens=3, output_tokens=5),
        provider="test",
        model="step-model",
    )
    assert emit_permission_audit(
        PermissionAuditRecord(
            session_id=transcript_id,
            cwd=cwd,
            tool_name="write_file",
            tool_use_id="tool-transcript",
            decision="allow",
            scope="once",
            source="test",
            audit_log_path=str(transcript_dir / "permission-audit.jsonl"),
        ),
        settings=PermissionAuditSettings(max_file_bytes=1024, max_files=1),
    )
    transcript_mcp_result = convert_mcp_tool_result(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///tmp/template.json",
                        "mimeType": "application/json",
                        "blob": base64.b64encode(b'{"ok": true}').decode("ascii"),
                    },
                }
            ]
        },
        server_name="ros",
        tool_name="export",
        session_id=transcript_id,
        session_dir=transcript_dir,
    )
    transcript_tool_artifact = Path(transcript_mcp_result.metadata["mcp"]["artifacts"][0]["path"])

    artifact_metadata = artifact_store_for_session(session_dir).save_text(
        filename="stack-output.txt",
        content="stack-id: s-123",
        media_type="text/plain",
    )
    a2a_artifact_path = session_paths.a2a_artifacts_dir / artifact_metadata.artifact_id / "stack-output.txt"

    # These two files are executor-owned snapshots. This test writes the files directly to cover
    # session backup mirror shape without coupling to the full A2A task-store/executor flow.
    session_paths.a2a_dir.mkdir(parents=True, exist_ok=True)
    session_paths.a2a_task_path.write_text(
        json.dumps({"task_id": "task-1", "state": "completed"}, sort_keys=True),
        encoding="utf-8",
    )
    session_paths.a2a_context_path.write_text(
        json.dumps({"context_id": "ctx-1", "session_id": session_id}, sort_keys=True),
        encoding="utf-8",
    )

    result = SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
        cwd,
        session_id,
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    mirror = backup_root / "projects" / session_dir.parent.name / session_id
    assert result.enabled is True
    assert result.source == session_dir
    assert result.destination == mirror

    _assert_same_file(session_dir, mirror, "metadata.json")
    assert _read_json(mirror / "metadata.json")["layout_version"] == SESSION_LAYOUT_VERSION_V2
    _assert_same_file(session_dir, mirror, "session.jsonl")
    _assert_same_file(session_dir, mirror, "usage.jsonl")
    _assert_same_file(session_dir, mirror, "permission-audit.jsonl")
    _assert_same_file(session_dir, mirror, "image-cache/1.png", b"root-image")
    _assert_same_file(session_dir, mirror, root_tool_artifact.relative_to(session_dir).as_posix(), b"root-tool-result")
    _assert_same_file(session_dir, mirror, f"pipeline/transcripts/{transcript_id}/session.jsonl")
    _assert_same_file(session_dir, mirror, f"pipeline/transcripts/{transcript_id}/usage.jsonl")
    _assert_same_file(session_dir, mirror, f"pipeline/transcripts/{transcript_id}/permission-audit.jsonl")
    _assert_same_file(
        session_dir,
        mirror,
        transcript_tool_artifact.relative_to(session_dir).as_posix(),
        b'{"ok": true}',
    )
    _assert_same_file(session_dir, mirror, "a2a/task.json")
    _assert_same_file(session_dir, mirror, "a2a/context.json")
    _assert_same_file(session_dir, mirror, a2a_artifact_path.relative_to(session_dir).as_posix(), b"stack-id: s-123")

    assert not (mirror / ".backup-state.json").exists()
    assert not (mirror / ".backup-lock").exists()
    source_marker = _read_json(session_dir / ".backup-state.json")
    assert source_marker["status"] == "succeeded"
    assert source_marker["reason"] == "terminal"

    assert not (config_dir / "tool-results" / session_id / root_tool_artifact.name).exists()
    assert not (config_dir / "image-cache" / session_id / "1.png").exists()
    assert not (config_dir / "a2a" / "artifacts" / artifact_metadata.artifact_id / "stack-output.txt").exists()
    legacy_transcript_artifact = (
        config_dir
        / "projects"
        / session_dir.parent.name
        / transcript_id
        / "tool-results"
        / transcript_tool_artifact.name
    )
    assert not legacy_transcript_artifact.exists()
