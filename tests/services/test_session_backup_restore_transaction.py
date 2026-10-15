"""A failed restore must remain retryable without trusting a partial local tree."""

import json
from pathlib import Path

import pytest

from iac_code.services.session_backup import (
    BackupReason,
    SessionBackupBlocked,
    SessionBackupError,
    SessionBackupNotReadyError,
    SessionBackupService,
)
from iac_code.services.session_backup_staging import StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


class RestoreScenario:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path.resolve()
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(self.root / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(self.root / "shared"))
        monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
        self.cwd = "/repo"
        self.session_id = "restore-session"
        self.source_storage = SessionStorage(projects_dir=self.root / "source" / "projects")
        self.local_storage = SessionStorage(projects_dir=self.root / "local" / "projects")
        self.source = self.source_storage.session_dir(self.cwd, self.session_id)
        self.local = self.local_storage.session_dir(self.cwd, self.session_id)
        write_session_metadata(
            self.source,
            SessionMetadata(session_id=self.session_id, cwd=self.cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        self.writer = SessionBackupService(self.source_storage, retry_delays=())
        self.reader = SessionBackupService(self.local_storage, retry_delays=())
        self.writer.initialize_session(self.cwd, self.session_id)
        self.publish("old")

    def publish(self, text: str) -> None:
        (self.source / "session.jsonl").write_text(
            json.dumps({"role": "user", "content": text}) + "\n", encoding="utf-8"
        )
        (self.source / "payload.txt").write_text(text, encoding="utf-8")
        self.writer.backup_session(self.cwd, self.session_id, reason=BackupReason.NORMAL_TURN_END, critical=True)

    def restore(self):
        return self.reader.reconcile_session(self.cwd, self.session_id)

    @staticmethod
    def fail_state(*_args):
        raise OSError("state failure")


@pytest.mark.parametrize("failure_point", ["copy", "state"])
@pytest.mark.parametrize("existing_local", [False, True], ids=["cold", "refresh"])
def test_restore_recovers_after_transient_local_io_failure(tmp_path, monkeypatch, existing_local, failure_point):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    if existing_local:
        scenario.restore()
        scenario.publish("new")
    original_copy = scenario.reader._copy_file
    original_write_state = scenario.reader._write_state

    def fail_copy(source, destination):
        if source.name == "session.jsonl":
            raise OSError("transient local restore copy failure")
        original_copy(source, destination)

    def fail_state(destination, state):
        raise OSError("transient local restore state failure")

    monkeypatch.setattr(scenario.reader, "_copy_file", fail_copy if failure_point == "copy" else original_copy)
    monkeypatch.setattr(
        scenario.reader, "_write_state", fail_state if failure_point == "state" else original_write_state
    )
    with pytest.raises(OSError, match="transient local restore"):
        scenario.restore()
    if not existing_local:
        assert not scenario.local.exists(), "Failed cold restore left an authoritative-looking partial local session"
    monkeypatch.setattr(scenario.reader, "_copy_file", original_copy)
    monkeypatch.setattr(scenario.reader, "_write_state", original_write_state)

    recovered = scenario.restore()
    assert recovered.state is not None
    assert recovered.state.generation == (2 if existing_local else 1)
    assert scenario.local_storage.load(scenario.cwd, scenario.session_id)[0].content == (
        "new" if existing_local else "old"
    )
    assert (scenario.local / "payload.txt").read_text(encoding="utf-8") == ("new" if existing_local else "old")


def test_restore_does_not_replace_an_existing_session_without_backup_state(tmp_path, monkeypatch):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    write_session_metadata(
        scenario.local,
        SessionMetadata(session_id=scenario.session_id, cwd=scenario.cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    (scenario.local / "session.jsonl").write_text("keep this unknown local data\n", encoding="utf-8")

    with pytest.raises(SessionBackupError, match="existing session is missing backup state"):
        scenario.restore()

    assert (scenario.local / "session.jsonl").read_text(encoding="utf-8") == "keep this unknown local data\n"


@pytest.mark.parametrize("staged", [False, True], ids=["direct", "staged"])
def test_interrupted_refresh_blocks_read_initialize_and_backup_until_exact_retry(tmp_path, monkeypatch, staged):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    scenario.restore()
    scenario.publish("new")
    original_write_state = scenario.reader._write_state
    with monkeypatch.context() as failure:
        failure.setattr(scenario.reader, "_write_state", scenario.fail_state)
        with pytest.raises(OSError, match="state failure"):
            scenario.restore()
    reader = (
        StagedSessionBackupService(scenario.root / "staging", scenario.local_storage, retry_delays=())
        if staged
        else scenario.reader
    )
    assert scenario.reader._write_state == original_write_state
    with pytest.raises(SessionBackupError, match="local session restore is incomplete"):
        reader.read_local_state(scenario.cwd, scenario.session_id)
    with pytest.raises(SessionBackupError, match="local session restore is incomplete"):
        reader.initialize_session(scenario.cwd, scenario.session_id)
    with pytest.raises(SessionBackupBlocked, match="local session restore is incomplete"):
        reader.backup_session(scenario.cwd, scenario.session_id, reason=BackupReason.TERMINAL, critical=True)
    result = reader.restore_session(scenario.cwd, scenario.session_id)
    assert result.restored is True
    assert not (scenario.local / ".backup-restore.json").exists()
    assert reader.read_local_state(scenario.cwd, scenario.session_id).generation == 2


def test_interrupted_refresh_keeps_permission_generation_floor(tmp_path, monkeypatch):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    scenario.restore()
    scenario.publish("new")
    with monkeypatch.context() as failure:
        failure.setattr(scenario.reader, "_write_state", scenario.fail_state)
        with pytest.raises(OSError):
            scenario.restore()
    with pytest.raises(SessionBackupNotReadyError):
        scenario.reader.reconcile_session(scenario.cwd, scenario.session_id, minimum_generation=3)
    assert (scenario.local / ".backup-restore.json").exists()


@pytest.mark.parametrize("shared_change", ["same_generation_other_commit", "older_generation"])
def test_interrupted_refresh_rejects_a_conflicting_or_older_shared_capture(tmp_path, monkeypatch, shared_change):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    scenario.restore()
    shared = scenario.root / "shared" / "projects" / scenario.source.parent.name / scenario.session_id
    previous_state = (shared / ".backup-state.json").read_text(encoding="utf-8")
    scenario.publish("new")
    with monkeypatch.context() as failure:
        failure.setattr(scenario.reader, "_write_state", scenario.fail_state)
        with pytest.raises(OSError):
            scenario.restore()
    if shared_change == "same_generation_other_commit":
        shared_state = json.loads((shared / ".backup-state.json").read_text(encoding="utf-8"))
        shared_state["commit_id"] = "different-commit-at-same-generation"
        (shared / ".backup-state.json").write_text(json.dumps(shared_state), encoding="utf-8")
    else:
        (shared / ".backup-state.json").write_text(previous_state, encoding="utf-8")
    with pytest.raises(SessionBackupError, match="restore conflicts with its committed shared capture"):
        scenario.restore()
    assert (scenario.local / "payload.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("staged", [False, True], ids=["direct", "staged"])
def test_interrupted_refresh_adopts_a_newer_committed_shared_capture(tmp_path, monkeypatch, staged):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    scenario.restore()
    scenario.publish("new")
    with monkeypatch.context() as failure:
        failure.setattr(scenario.reader, "_write_state", scenario.fail_state)
        with pytest.raises(OSError):
            scenario.restore()
    scenario.publish("later")
    reader = (
        StagedSessionBackupService(scenario.root / "staging", scenario.local_storage, retry_delays=())
        if staged
        else scenario.reader
    )
    result = reader.reconcile_session(scenario.cwd, scenario.session_id, minimum_generation=3)
    assert result.action == "restored"
    assert result.state.generation == 3
    assert scenario.local_storage.load(scenario.cwd, scenario.session_id)[0].content == "later"
    assert (scenario.local / "payload.txt").read_text(encoding="utf-8") == "later"
    assert not (scenario.local / ".backup-restore.json").exists()


def test_newer_capture_marker_commits_before_retry_payload_mutation(tmp_path, monkeypatch):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    scenario.restore()
    scenario.publish("new")
    with monkeypatch.context() as failure:
        failure.setattr(scenario.reader, "_write_state", scenario.fail_state)
        with pytest.raises(OSError):
            scenario.restore()
    scenario.publish("later")
    shared = scenario.root / "shared" / "projects" / scenario.source.parent.name / scenario.session_id
    shared_state = json.loads((shared / ".backup-state.json").read_text(encoding="utf-8"))

    def fail_after_marker_advanced(_source, _destination):
        marker = json.loads((scenario.local / ".backup-restore.json").read_text(encoding="utf-8"))
        assert marker["generation"] == 3
        assert marker["commitId"] == shared_state["commit_id"]
        raise OSError("second transient restore failure")

    with monkeypatch.context() as failure:
        failure.setattr(scenario.reader, "_mirror", fail_after_marker_advanced)
        with pytest.raises(OSError, match="second transient restore failure"):
            scenario.restore()
    with pytest.raises(SessionBackupError, match="local session restore is incomplete"):
        scenario.reader.read_local_state(scenario.cwd, scenario.session_id)
    assert scenario.restore().state.generation == 3
    assert scenario.local_storage.load(scenario.cwd, scenario.session_id)[0].content == "later"


def test_restore_marker_is_excluded_from_mirrored_payload(tmp_path, monkeypatch):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    assert not scenario.reader._included(Path(".backup-restore.json"))
    scenario.restore()
    scenario.publish("new")
    scenario.restore()
    assert not (scenario.local / ".backup-restore.json").exists()


@pytest.mark.parametrize("marker_content", ["invalid-json", '{"version": 1}'])
def test_existing_local_restore_marker_fails_closed_when_invalid(tmp_path, monkeypatch, marker_content):
    scenario = RestoreScenario(tmp_path, monkeypatch)
    scenario.restore()
    (scenario.local / ".backup-restore.json").write_text(marker_content, encoding="utf-8")
    with pytest.raises(SessionBackupError, match="local session restore (marker is invalid|conflicts)"):
        scenario.restore()
    assert scenario.local_storage.load(scenario.cwd, scenario.session_id)[0].content == "old"
