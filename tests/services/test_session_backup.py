import json
import os
import sys
from pathlib import Path

import pytest

from iac_code.services.session_backup import (
    BackupReason,
    SessionBackupBlocked,
    SessionBackupError,
    SessionBackupService,
    SessionRestoreResult,
)
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


def _read_backup_marker(session_dir: Path) -> dict[str, object]:
    return json.loads((session_dir / ".backup-state.json").read_text(encoding="utf-8"))


def _create_v2_session_dir(storage: SessionStorage, cwd: str, session_id: str) -> Path:
    session_dir = storage.session_dir(cwd, session_id)
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd=cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    return session_dir


def test_backup_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    service = SessionBackupService(storage)

    result = service.backup_session("/repo", "s1", reason=BackupReason.NORMAL_TURN_END, critical=False)

    assert result.enabled is False
    assert result.copied_files == 0


def test_backup_disabled_when_env_unset_does_not_resolve_session_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)

    class RaisingStorage:
        def session_dir(self, _cwd: str, _session_id: str) -> Path:
            raise AssertionError("session_dir should not be called when backup is disabled")

    result = SessionBackupService(RaisingStorage()).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    assert result.enabled is False


def test_backup_disabled_when_env_unset_does_not_validate_legacy_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)

    result = SessionBackupService().backup_session(
        "/repo",
        "../legacy-session",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    assert result.enabled is False


@pytest.mark.parametrize(
    "raw_backup_root",
    [
        "relative-backup",
        pytest.param(r"C:\backup", marks=pytest.mark.skipif(os.name == "nt", reason="valid on Windows")),
        r"C:",
        r"C:\\",
        r"\\server\share",
    ],
)
def test_backup_rejects_non_absolute_or_windows_root_backup_dir(
    monkeypatch: pytest.MonkeyPatch,
    raw_backup_root: str,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", raw_backup_root)

    with pytest.raises(SessionBackupError, match="backup root"):
        SessionBackupService()._backup_root()


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics only")
def test_backup_accepts_windows_absolute_backup_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", r"C:\backup")

    assert SessionBackupService()._backup_root() == Path(r"C:\backup")


@pytest.mark.parametrize(
    "session_id",
    ["/tmp/s1", "../s1", "a/b", r"a\b", ".", "..", "", "CON", "NUL.txt", "COM1", "LPT9.log", "s1.", "s1 "],
)
def test_backup_rejects_unsafe_session_id_before_storage_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_id: str,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    calls = []

    class RecordingStorage:
        def session_dir(self, cwd: str, requested_session_id: str) -> Path:
            calls.append((cwd, requested_session_id))
            raise AssertionError("session_dir should not be called for unsafe session_id")

    with pytest.raises(SessionBackupError, match="unsafe session_id"):
        SessionBackupService(RecordingStorage()).backup_session(
            "/repo",
            session_id,
            reason=BackupReason.NORMAL_TURN_END,
            critical=True,
        )

    assert calls == []
    assert not backup_root.exists()


def test_backup_mirrors_session_and_excludes_local_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    (session_dir / "permission-audit.jsonl").write_text("audit\n", encoding="utf-8")
    (session_dir / "usage.jsonl").write_text("usage\n", encoding="utf-8")
    (session_dir / ".backup-state.json").write_text("local\n", encoding="utf-8")
    (session_dir / ".backup-lock").write_text("lock\n", encoding="utf-8")
    (session_dir / ".session.jsonl.lock").write_text("session lock\n", encoding="utf-8")
    (session_dir / ".permission-audit.jsonl.lock").write_text("audit lock\n", encoding="utf-8")
    (session_dir / ".usage.jsonl.lock").write_text("usage lock\n", encoding="utf-8")

    result = SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.PIPELINE_STEP_COMPLETED,
        critical=True,
    )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert result.enabled is True
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "one\n"
    assert (mirror / "permission-audit.jsonl").read_text(encoding="utf-8") == "audit\n"
    assert (mirror / "usage.jsonl").read_text(encoding="utf-8") == "usage\n"
    assert not (mirror / ".backup-state.json").exists()
    assert not (mirror / ".backup-lock").exists()
    assert not (mirror / ".session.jsonl.lock").exists()
    assert not (mirror / ".permission-audit.jsonl.lock").exists()
    assert not (mirror / ".usage.jsonl.lock").exists()
    marker = _read_backup_marker(session_dir)
    assert marker["status"] == "succeeded"
    assert marker["reason"] == "pipeline_step_completed"
    assert "error" not in marker


def test_restore_session_copies_backup_only_v2_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_projects = tmp_path / "config" / "projects"
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    cwd = "/repo"
    session_id = "restore-session"
    storage = SessionStorage(projects_dir=config_projects)
    backup_storage = SessionStorage(projects_dir=backup_root / "projects")
    backup_session_dir = _create_v2_session_dir(backup_storage, cwd, session_id)
    (backup_session_dir / "session.jsonl").write_text('{"role":"user","content":"from backup"}\n', encoding="utf-8")
    (backup_session_dir / "a2a").mkdir()
    (backup_session_dir / "a2a" / "context.json").write_text('{"context_id":"ctx-1"}\n', encoding="utf-8")

    result = SessionBackupService(session_storage=storage, retry_delays=()).restore_session(cwd, session_id)

    restored_session_dir = storage.session_dir(cwd, session_id)
    assert isinstance(result, SessionRestoreResult)
    assert result.enabled is True
    assert result.restored is True
    assert result.source == backup_session_dir
    assert result.destination == restored_session_dir
    assert (restored_session_dir / "metadata.json").is_file()
    assert (restored_session_dir / "session.jsonl").read_text(encoding="utf-8") == (
        '{"role":"user","content":"from backup"}\n'
    )
    assert (restored_session_dir / "a2a" / "context.json").read_text(encoding="utf-8") == '{"context_id":"ctx-1"}\n'


def test_restore_session_does_not_overwrite_existing_config_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_projects = tmp_path / "config" / "projects"
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    cwd = "/repo"
    session_id = "restore-session"
    storage = SessionStorage(projects_dir=config_projects)
    local_session_dir = _create_v2_session_dir(storage, cwd, session_id)
    (local_session_dir / "session.jsonl").write_text("local\n", encoding="utf-8")
    backup_storage = SessionStorage(projects_dir=backup_root / "projects")
    backup_session_dir = _create_v2_session_dir(backup_storage, cwd, session_id)
    (backup_session_dir / "session.jsonl").write_text("backup\n", encoding="utf-8")

    result = SessionBackupService(session_storage=storage, retry_delays=()).restore_session(cwd, session_id)

    assert result.enabled is True
    assert result.restored is False
    assert result.destination == local_session_dir
    assert (local_session_dir / "session.jsonl").read_text(encoding="utf-8") == "local\n"


def test_backup_deletes_stale_mirror_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    (mirror / "stale.txt").write_text("old\n", encoding="utf-8")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert not (mirror / "stale.txt").exists()
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"


def test_backup_deletes_stale_symlink_to_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    stale_link = mirror / "stale-link"
    _symlink_or_skip(outside_dir, stale_link, target_is_directory=True)

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert not stale_link.is_symlink()
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"
    assert outside_dir.exists()


def test_backup_lock_rejects_source_reparse_point(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = SessionBackupService()
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    monkeypatch.setattr(service, "_is_reparse_point", lambda path: path == session_dir)

    with pytest.raises(SessionBackupError, match="session source"):
        with service._session_backup_lock(session_dir):
            pass

    assert not (session_dir / ".backup-lock").exists()


def test_backup_lock_rejects_lock_file_reparse_point(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = SessionBackupService()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lock_path = session_dir / ".backup-lock"
    lock_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(service, "_is_reparse_point", lambda path: path == lock_path)

    with pytest.raises(SessionBackupError, match="backup lock"):
        with service._session_backup_lock(session_dir):
            pass


def test_backup_lock_uses_msvcrt_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_code.services import session_backup as session_backup_module

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def locking(self, _fd: int, mode: int, nbytes: int) -> None:
            self.calls.append((mode, nbytes))

    fake_msvcrt = FakeMsvcrt()
    opened: list[tuple[Path, int, int]] = []
    service = SessionBackupService(session_storage=object())
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(session_backup_module.os, "name", "nt", raising=False)

    def fake_open_no_follow(path: Path, flags: int, mode: int) -> int:
        opened.append((path, flags, mode))
        return os.open(path, flags, mode)

    monkeypatch.setattr(service, "_open_no_follow_fd", fake_open_no_follow)

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    with service._session_backup_lock(session_dir):
        assert (session_dir / ".backup-lock").exists()

    assert fake_msvcrt.calls == [(fake_msvcrt.LK_LOCK, 1), (fake_msvcrt.LK_UNLCK, 1)]
    assert opened == [(session_dir / ".backup-lock", os.O_RDWR | os.O_CREAT, 0o600)]


def test_canonical_windows_path_text_normalizes_verbatim_prefixes() -> None:
    assert SessionBackupService._canonical_windows_path_text(r"\\?\C:\work\repo") == r"c:\work\repo"
    assert SessionBackupService._canonical_windows_path_text(r"\\?\UNC\server\share\repo") == r"\\server\share\repo"


def test_backup_windows_no_follow_rejects_reparse_attribute_from_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    import types

    class FakeFunction:
        def __init__(self, result: object = True, *, reparse_info: bool = False) -> None:
            self.result = result
            self.reparse_info = reparse_info
            self.calls: list[tuple[object, ...]] = []
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            self.calls.append(args)
            if self.reparse_info:
                args[1]._obj.dwFileAttributes = 0x400
            return self.result

    class FakeKernel32:
        def __init__(self) -> None:
            self.CreateFileW = FakeFunction(result=1234)
            self.GetFileInformationByHandle = FakeFunction(reparse_info=True)
            self.CloseHandle = FakeFunction(result=True)

    fake_kernel32 = FakeKernel32()
    fake_msvcrt = types.SimpleNamespace(open_osfhandle=lambda *_args: pytest.fail("open_osfhandle called"))
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)

    with pytest.raises(SessionBackupError, match="regular file"):
        SessionBackupService()._open_windows_no_follow_fd(tmp_path / "session.jsonl", os.O_RDONLY, 0o600)

    assert fake_kernel32.GetFileInformationByHandle.calls
    assert fake_kernel32.CloseHandle.calls == [(1234,)]


def test_backup_rejects_windows_physical_alias_overlap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_code.services import session_backup as session_backup_module

    source = tmp_path / "config" / "projects" / "repo" / "s1"
    source.mkdir(parents=True)
    write_session_metadata(
        source,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    backup_root = tmp_path / "backup-alias"
    destination = backup_root / "projects" / source.parent.name / "s1"
    service = SessionBackupService(session_storage=object())

    physical_paths = {
        source: r"\\?\C:\work\repo\.iac-code\projects\repo\s1",
        backup_root: r"\\?\C:\work\repo\.iac-code\projects\repo\s1\backup",
        destination: r"\\?\C:\work\repo\.iac-code\projects\repo\s1\backup\projects\repo\s1",
    }

    monkeypatch.setattr(session_backup_module.os, "name", "nt", raising=False)
    monkeypatch.setattr(service, "_windows_physical_path_text", lambda path: physical_paths[path])

    with pytest.raises(SessionBackupError, match="overlaps session source"):
        service._validate_mirror_paths(source, destination, backup_root)


def test_backup_rejects_windows_physical_source_inside_backup_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.services import session_backup as session_backup_module

    source = tmp_path / "config" / "projects" / "repo" / "s1"
    source.mkdir(parents=True)
    write_session_metadata(
        source,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    backup_root = tmp_path / "backup-alias"
    destination = backup_root / "projects" / source.parent.name / "s1"
    service = SessionBackupService(session_storage=object())

    physical_paths = {
        source: r"\\?\C:\work\repo\.iac-code\projects\repo\s1",
        backup_root: r"\\?\C:\work\repo\.iac-code",
        destination: r"\\?\C:\work\repo\.iac-code\projects\repo\s1",
    }

    monkeypatch.setattr(session_backup_module.os, "name", "nt", raising=False)
    monkeypatch.setattr(service, "_windows_physical_path_text", lambda path: physical_paths[path])

    with pytest.raises(SessionBackupError, match="overlaps session source"):
        service._validate_mirror_paths(source, destination, backup_root)


def test_windows_physical_path_sets_win32_signatures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import ctypes

    class FakeFunction:
        def __init__(self, result=None) -> None:
            self.result = result
            self.calls = []
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            self.calls.append(args)
            if len(args) >= 2 and hasattr(args[1], "value"):
                args[1].value = r"\\?\C:\real\session"
                return len(args[1].value)
            return self.result

    class FakeKernel32:
        def __init__(self) -> None:
            self.CreateFileW = FakeFunction(result=1234567890123)
            self.GetFinalPathNameByHandleW = FakeFunction()
            self.CloseHandle = FakeFunction(result=True)

    fake_kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)

    physical = SessionBackupService._windows_existing_physical_path_text(tmp_path)

    assert physical == r"\\?\C:\real\session"
    assert fake_kernel32.CreateFileW.argtypes is not None
    assert fake_kernel32.CreateFileW.restype is not None
    assert fake_kernel32.GetFinalPathNameByHandleW.argtypes is not None
    assert fake_kernel32.GetFinalPathNameByHandleW.restype is not None
    assert fake_kernel32.CloseHandle.argtypes is not None
    assert fake_kernel32.CloseHandle.restype is not None
    assert fake_kernel32.CloseHandle.calls == [(1234567890123,)]


def test_unlink_uses_rmdir_for_windows_directory_symlink(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.services import session_backup as session_backup_module

    class FakeDirectorySymlink:
        def __init__(self) -> None:
            self.rmdir_called = False
            self.unlink_called = False

        def is_symlink(self) -> bool:
            return True

        def is_dir(self) -> bool:
            return True

        def rmdir(self) -> None:
            self.rmdir_called = True

        def unlink(self) -> None:
            self.unlink_called = True

    fake_path = FakeDirectorySymlink()
    monkeypatch.setattr(session_backup_module.os, "name", "nt", raising=False)

    SessionBackupService(session_storage=object())._unlink(fake_path)  # type: ignore[arg-type]

    assert fake_path.rmdir_called is True
    assert fake_path.unlink_called is False


def test_unlink_uses_lstat_directory_attribute_for_broken_windows_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.services import session_backup as session_backup_module

    class FakeStat:
        st_file_attributes = getattr(os.stat_result, "FILE_ATTRIBUTE_DIRECTORY", 0x10)

    class FakeBrokenDirectorySymlink:
        def __init__(self) -> None:
            self.rmdir_called = False
            self.unlink_called = False

        def is_symlink(self) -> bool:
            return True

        def stat(self, *, follow_symlinks: bool = True):
            assert follow_symlinks is False
            return FakeStat()

        def is_dir(self) -> bool:
            return False

        def rmdir(self) -> None:
            self.rmdir_called = True

        def unlink(self) -> None:
            self.unlink_called = True

    fake_path = FakeBrokenDirectorySymlink()
    monkeypatch.setattr(session_backup_module.os, "name", "nt", raising=False)

    SessionBackupService(session_storage=object())._unlink(fake_path)  # type: ignore[arg-type]

    assert fake_path.rmdir_called is True
    assert fake_path.unlink_called is False


def test_backup_deletes_stale_broken_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    stale_link = mirror / "broken-link"
    _symlink_or_skip(tmp_path / "missing-target", stale_link)

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert not stale_link.is_symlink()
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"


def test_backup_deletes_stale_non_regular_destination_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is unavailable")
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    stale_fifo = mirror / "stale.pipe"
    try:
        os.mkfifo(stale_fifo)
    except OSError as exc:
        pytest.skip(f"fifo creation unsupported: {exc}")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert not stale_fifo.exists()
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"


def test_backup_skips_source_symlink_to_external_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    external_file = tmp_path / "external-secret.txt"
    external_file.write_text("secret\n", encoding="utf-8")
    _symlink_or_skip(external_file, session_dir / "linked-secret.txt")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    (mirror / "linked-secret.txt").write_text("stale\n", encoding="utf-8")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert not (mirror / "linked-secret.txt").exists()
    assert external_file.read_text(encoding="utf-8") == "secret\n"


def test_backup_copy_file_refuses_symlink_source(tmp_path: Path) -> None:
    external_file = tmp_path / "external-secret.txt"
    external_file.write_text("secret\n", encoding="utf-8")
    source_link = tmp_path / "session" / "linked-secret.txt"
    source_link.parent.mkdir()
    _symlink_or_skip(external_file, source_link)
    destination = tmp_path / "mirror" / "linked-secret.txt"

    with pytest.raises(SessionBackupError, match="regular file"):
        SessionBackupService(session_storage=object())._copy_file(source_link, destination)

    assert not destination.exists()
    assert external_file.read_text(encoding="utf-8") == "secret\n"


def test_backup_skips_source_symlink_to_external_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    external_dir = tmp_path / "external-dir"
    external_dir.mkdir()
    (external_dir / "secret.txt").write_text("secret\n", encoding="utf-8")
    _symlink_or_skip(external_dir, session_dir / "linked-dir", target_is_directory=True)

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert not (mirror / "linked-dir").exists()
    assert external_dir.exists()
    assert (external_dir / "secret.txt").read_text(encoding="utf-8") == "secret\n"


def test_backup_skips_broken_source_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    _symlink_or_skip(tmp_path / "missing-source-target", session_dir / "broken-link")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert not (mirror / "broken-link").exists()


def test_backup_rejects_symlinked_source_session_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = storage.session_dir("/repo", "s1")
    session_dir.parent.mkdir(parents=True)
    external_session = tmp_path / "external-session"
    external_session.mkdir()
    write_session_metadata(
        external_session,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    (external_session / "secret.txt").write_text("secret\n", encoding="utf-8")
    _symlink_or_skip(external_session, session_dir, target_is_directory=True)

    with pytest.raises(SessionBackupBlocked, match="session source"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert not (mirror / "secret.txt").exists()
    assert (external_session / "secret.txt").read_text(encoding="utf-8") == "secret\n"


def test_backup_rejects_reparse_point_source_ancestry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    service = SessionBackupService(session_storage=storage, retry_delays=())
    monkeypatch.setattr(service, "_is_reparse_point", lambda path: Path(path) == session_dir.parent)

    with pytest.raises(SessionBackupBlocked, match="session source"):
        service.backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert not (mirror / "session.jsonl").exists()
    assert not (session_dir / ".backup-state.json").exists()
    assert not (session_dir / ".backup-lock").exists()


def test_backup_skips_non_regular_source_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is unavailable")
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    fifo_path = session_dir / "event.pipe"
    try:
        os.mkfifo(fifo_path)
    except OSError as exc:
        pytest.skip(f"fifo creation unsupported: {exc}")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    (mirror / "event.pipe").write_text("stale\n", encoding="utf-8")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert not (mirror / "event.pipe").exists()
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"


def test_backup_failed_marker_sanitizes_raw_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup-secret-token"
    backup_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    service = SessionBackupService(session_storage=storage, retry_delays=())

    result = service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    marker = _read_backup_marker(session_dir)
    assert result.succeeded is False
    assert result.error is not None
    assert str(tmp_path) not in result.error
    assert str(tmp_path) not in marker["error"]
    assert "backup-secret-token" not in result.error
    assert "backup-secret-token" not in marker["error"]


def test_backup_error_redacts_arbitrary_mount_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    service = SessionBackupService(session_storage=storage, retry_delays=())

    def fail_with_mount_path(*_args, **_kwargs) -> None:
        raise SessionBackupError("copy failed for /mnt/oss/customer-bucket/tenant-a/session/s1")

    monkeypatch.setattr(service, "_validate_mirror_paths", fail_with_mount_path)

    result = service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    marker = _read_backup_marker(session_dir)
    assert result.succeeded is False
    assert result.error is not None
    for public_error in (result.error, str(marker["error"])):
        assert "/mnt/oss" not in public_error
        assert "customer-bucket" not in public_error
        assert "tenant-a" not in public_error


def test_backup_error_redacts_windows_paths_with_spaces() -> None:
    error = SessionBackupService._public_error_text(
        SessionBackupError(
            r"copy failed for C:\Users\Alice\OneDrive - Org\Backup Root\secret.txt"
            r" and \\server\secret share\tenant-a\file.json"
        )
    )

    assert "OneDrive" not in error
    assert "Org" not in error
    assert "Backup Root" not in error
    assert "secret share" not in error
    assert "tenant-a" not in error


def test_backup_replaces_stale_file_with_source_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "foo").mkdir()
    (session_dir / "foo" / "child.txt").write_text("child\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    (mirror / "foo").write_text("stale file\n", encoding="utf-8")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert (mirror / "foo").is_dir()
    assert (mirror / "foo" / "child.txt").read_text(encoding="utf-8") == "child\n"


def test_backup_replaces_stale_root_file_with_session_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("stale file\n", encoding="utf-8")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert mirror.is_dir()
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"


def test_backup_replaces_stale_directory_with_source_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "foo").write_text("fresh file\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    (mirror / "foo").mkdir(parents=True)
    (mirror / "foo" / "old.txt").write_text("old\n", encoding="utf-8")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert (mirror / "foo").is_file()
    assert (mirror / "foo").read_text(encoding="utf-8") == "fresh file\n"


def test_backup_replaces_stale_fifo_with_source_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is unavailable")
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    source_file = session_dir / "foo"
    source_file.write_text("", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    mirror_fifo = mirror / "foo"
    try:
        os.mkfifo(mirror_fifo)
    except OSError as exc:
        pytest.skip(f"fifo creation unsupported: {exc}")
    fifo_stat = mirror_fifo.stat()
    os.utime(source_file, ns=(fifo_stat.st_atime_ns, fifo_stat.st_mtime_ns))

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert (mirror / "foo").is_file()
    assert (mirror / "foo").read_text(encoding="utf-8") == ""


def test_backup_root_inside_session_dir_blocks_without_recursive_mirror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    backup_root = session_dir / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))

    with pytest.raises(SessionBackupBlocked, match="session source"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert not backup_root.exists()
    assert not mirror.exists()
    assert not (backup_root / "projects" / session_dir.parent.name / "s1" / "backup" / "projects").exists()


def test_backup_rejects_source_inside_planned_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    source = backup_root / "projects" / "s1" / "s1" / "source"
    source.mkdir(parents=True)
    source_file = source / "session.jsonl"
    source_file.write_text("fresh\n", encoding="utf-8")

    class FakeStorage:
        def session_dir(self, _cwd: str, _session_id: str) -> Path:
            return source

    with pytest.raises(SessionBackupBlocked, match="backup destination"):
        SessionBackupService(session_storage=FakeStorage(), retry_delays=()).backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    assert source_file.read_text(encoding="utf-8") == "fresh\n"


def test_backup_rejects_backup_root_containing_session_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    backup_root = session_dir.parent.parent
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))

    with pytest.raises(SessionBackupBlocked, match="backup destination"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert not (mirror / "session.jsonl").exists()


def test_backup_rejects_destination_ancestry_symlink_outside_backup_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(outside, backup_root / "projects", target_is_directory=True)
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")

    with pytest.raises(SessionBackupBlocked, match="destination ancestry"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    assert not (outside / session_dir.parent.name / "s1" / "session.jsonl").exists()


def test_backup_rejects_destination_ancestry_symlink_inside_backup_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    real_projects = backup_root / "real_projects"
    real_projects.mkdir()
    _symlink_or_skip(real_projects, backup_root / "projects", target_is_directory=True)
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")

    with pytest.raises(SessionBackupBlocked, match="destination ancestry"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    assert not (real_projects / session_dir.parent.name / "s1" / "session.jsonl").exists()


def test_backup_accepts_symlinked_configured_backup_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_target = tmp_path / "backup-target"
    backup_target.mkdir()
    backup_link = tmp_path / "backup-link"
    _symlink_or_skip(backup_target, backup_link, target_is_directory=True)
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_link))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")

    result = SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.INPUT_REQUIRED,
        critical=True,
    )

    assert result.succeeded is True
    assert (backup_target / "projects" / session_dir.parent.name / "s1" / "session.jsonl").read_text(
        encoding="utf-8"
    ) == "fresh\n"


def test_restore_accepts_symlinked_configured_backup_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_target = tmp_path / "backup-target"
    backup_target.mkdir()
    backup_link = tmp_path / "backup-link"
    _symlink_or_skip(backup_target, backup_link, target_is_directory=True)
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_link))
    cwd = "/home/workspace/ctx-restore"
    session_id = "restore-session"
    source_storage = SessionStorage(projects_dir=tmp_path / "config-source" / "projects")
    source_session_dir = _create_v2_session_dir(source_storage, cwd, session_id)
    (source_session_dir / "session.jsonl").write_text('{"role":"user","content":"from backup"}\n', encoding="utf-8")
    SessionBackupService(session_storage=source_storage, retry_delays=()).backup_session(
        cwd,
        session_id,
        reason=BackupReason.NORMAL_TURN_END,
        critical=True,
    )
    restored_storage = SessionStorage(projects_dir=tmp_path / "config-restored" / "projects")

    result = SessionBackupService(session_storage=restored_storage, retry_delays=()).restore_session(cwd, session_id)

    restored_session_dir = restored_storage.session_dir(cwd, session_id)
    assert result.restored is True
    assert result.source == backup_link / "projects" / source_session_dir.parent.name / session_id
    assert result.destination == restored_session_dir
    assert (restored_session_dir / "session.jsonl").read_text(encoding="utf-8") == (
        '{"role":"user","content":"from backup"}\n'
    )


def test_backup_accepts_symlinked_backup_root_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ancestor_target = tmp_path / "ancestor-target"
    ancestor_target.mkdir()
    ancestor_link = tmp_path / "ancestor-link"
    _symlink_or_skip(ancestor_target, ancestor_link, target_is_directory=True)
    backup_root = ancestor_link / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")

    result = SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.INPUT_REQUIRED,
        critical=True,
    )

    assert result.succeeded is True
    assert (ancestor_target / "backup" / "projects" / session_dir.parent.name / "s1" / "session.jsonl").read_text(
        encoding="utf-8"
    ) == "fresh\n"


def test_backup_heals_leaf_destination_session_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.parent.mkdir(parents=True)
    outside_target = tmp_path / "outside-target"
    outside_target.mkdir()
    _symlink_or_skip(outside_target, mirror, target_is_directory=True)

    SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert mirror.is_dir()
    assert not mirror.is_symlink()
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"
    assert not (outside_target / "session.jsonl").exists()


def test_backup_uses_local_lock_and_excludes_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    service = SessionBackupService(session_storage=storage)
    original_mirror = service._mirror

    def assert_lock_exists(source: Path, destination: Path):
        assert (session_dir / ".backup-lock").exists()
        return original_mirror(source, destination)

    monkeypatch.setattr(service, "_mirror", assert_lock_exists)

    service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert (session_dir / ".backup-lock").exists()
    assert not (mirror / ".backup-lock").exists()


def test_backup_fsyncs_structural_metadata_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "new_dir").mkdir()
    (session_dir / "new_dir" / "child.txt").write_text("child\n", encoding="utf-8")
    (session_dir / "dir_conflict").write_text("fresh file\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    (mirror / "stale.txt").write_text("stale\n", encoding="utf-8")
    (mirror / "dir_conflict").mkdir()
    (mirror / "dir_conflict" / "old.txt").write_text("old\n", encoding="utf-8")
    (mirror / "empty").mkdir()
    fsync_calls: list[Path] = []
    monkeypatch.setattr("iac_code.services.session_backup.fsync_parent_dir", fsync_calls.append)

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert mirror in fsync_calls
    assert mirror / "new_dir" in fsync_calls
    assert mirror / "stale.txt" in fsync_calls
    assert mirror / "dir_conflict" in fsync_calls
    assert mirror / "empty" in fsync_calls


def test_failed_marker_for_mirror_failure_is_written_while_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("fcntl lock assertion is POSIX-only")
    import fcntl

    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    service = SessionBackupService(session_storage=storage, retry_delays=())
    original_write_marker = service._write_marker
    failed_marker_lock_states: list[bool] = []

    def fail_mirror(*_args, **_kwargs):
        raise OSError("mirror failed")

    def assert_lock_state_for_marker(
        source: Path,
        *,
        reason: BackupReason,
        status: str,
        error: str | None,
        **kwargs,
    ) -> None:
        if status == "failed":
            with (source / ".backup-lock").open("a+b") as lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    failed_marker_lock_states.append(True)
                else:
                    failed_marker_lock_states.append(False)
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        original_write_marker(source, reason=reason, status=status, error=error, **kwargs)

    monkeypatch.setattr(service, "_mirror", fail_mirror)
    monkeypatch.setattr(service, "_write_marker", assert_lock_state_for_marker)

    with pytest.raises(SessionBackupBlocked, match="mirror failed"):
        service.backup_session("/repo", "s1", reason=BackupReason.INPUT_REQUIRED, critical=True)

    assert failed_marker_lock_states == [True]


def test_critical_backup_retries_then_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    calls = {"count": 0}

    service = SessionBackupService(storage, (0, 0))

    def fail_copy(*_args, **_kwargs):
        calls["count"] += 1
        raise OSError("copy failed")

    monkeypatch.setattr(service, "_copy_file", fail_copy)

    with pytest.raises(SessionBackupBlocked, match="copy failed"):
        service.backup_session("/repo", "s1", reason=BackupReason.INPUT_REQUIRED, critical=True)

    assert calls["count"] == 3
    marker = _read_backup_marker(session_dir)
    assert marker["status"] == "failed"
    assert marker["reason"] == "input_required"
    assert "copy failed" in str(marker["error"])


def test_critical_backup_lock_failure_retries_then_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    (session_dir / ".backup-lock").mkdir()
    sleep_calls: list[float] = []
    monkeypatch.setattr("iac_code.services.session_backup.time.sleep", sleep_calls.append)

    with pytest.raises(SessionBackupBlocked, match=".backup-lock"):
        SessionBackupService(session_storage=storage, retry_delays=(0, 0)).backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    assert sleep_calls == [0, 0]
    marker = _read_backup_marker(session_dir)
    assert marker["status"] == "failed"
    assert marker["reason"] == "input_required"
    assert ".backup-lock" in str(marker["error"])


def test_critical_backup_lock_symlink_retries_then_blocks_without_following_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    external_lock = tmp_path / "external-lock"
    external_lock.write_text("external\n", encoding="utf-8")
    _symlink_or_skip(external_lock, session_dir / ".backup-lock")

    with pytest.raises(SessionBackupBlocked, match="backup lock"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "s1",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    assert external_lock.read_text(encoding="utf-8") == "external\n"
    marker = _read_backup_marker(session_dir)
    assert marker["status"] == "failed"
    assert marker["reason"] == "input_required"
    assert "backup lock" in str(marker["error"])


def test_non_critical_backup_lock_failure_returns_enabled_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    (session_dir / ".backup-lock").mkdir()

    result = SessionBackupService(session_storage=storage, retry_delays=(0, 0)).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    assert result.enabled is True
    assert result.succeeded is False
    assert result.error is not None
    assert result.retry_count == 2
    marker = _read_backup_marker(session_dir)
    assert marker["status"] == "failed"
    assert marker["reason"] == "normal_turn_end"
    assert marker["retry_count"] == 2
    assert marker["attempt"] == 3
    assert marker["exhausted"] is True
    assert ".backup-lock" in str(marker["error"])


def test_non_critical_backup_root_failure_returns_enabled_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    calls = {"count": 0}
    service = SessionBackupService(session_storage=storage, retry_delays=(0, 0))

    def fail_backup_root() -> Path:
        calls["count"] += 1
        raise OSError("backup root failed")

    monkeypatch.setattr(service, "_backup_root", fail_backup_root)

    result = service.backup_session("/repo", "s1", reason=BackupReason.NORMAL_TURN_END, critical=False)

    assert result.enabled is True
    assert result.succeeded is False
    assert result.error == "backup root failed"
    assert result.retry_count == 2
    assert calls["count"] == 3
    marker = _read_backup_marker(session_dir)
    assert marker["status"] == "failed"
    assert marker["reason"] == "normal_turn_end"
    assert marker["retry_count"] == 2
    assert marker["attempt"] == 3
    assert marker["exhausted"] is True
    assert "backup root failed" in str(marker["error"])


def test_missing_unmarked_source_blocks_when_critical_and_does_not_create_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = storage.session_dir("/repo", "missing")

    with pytest.raises(SessionBackupBlocked, match="supported session layout"):
        SessionBackupService(session_storage=storage, retry_delays=(0, 0)).backup_session(
            "/repo",
            "missing",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    mirror = backup_root / "projects" / session_dir.parent.name / "missing"
    assert not session_dir.exists()
    assert not mirror.exists()


def test_missing_unmarked_source_non_critical_skips_without_creating_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = storage.session_dir("/repo", "missing")

    result = SessionBackupService(session_storage=storage, retry_delays=(0, 0)).backup_session(
        "/repo",
        "missing",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    mirror = backup_root / "projects" / session_dir.parent.name / "missing"
    assert result.enabled is False
    assert not session_dir.exists()
    assert not mirror.exists()


def test_non_critical_backup_reports_unsupported_layout_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = storage.session_dir("/repo", "future")
    write_session_metadata(session_dir, SessionMetadata(session_id="future", cwd="/repo", layout_version=99))

    result = SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
        "/repo",
        "future",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    assert result.enabled is True
    assert result.succeeded is False
    assert "Unsupported session layout version" in str(result.error)
    assert not (backup_root / "projects" / session_dir.parent.name / "future").exists()


def test_critical_backup_converts_unsupported_layout_to_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = storage.session_dir("/repo", "future")
    write_session_metadata(session_dir, SessionMetadata(session_id="future", cwd="/repo", layout_version=99))

    with pytest.raises(SessionBackupBlocked, match="Unsupported session layout version"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "future",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    assert not (backup_root / "projects" / session_dir.parent.name / "future").exists()


def test_critical_backup_blocks_legacy_file_session_without_mutating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    legacy_path = storage.legacy_session_path("/repo", "legacy")
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")

    with pytest.raises(SessionBackupBlocked, match="supported session layout"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "legacy",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    session_dir = storage.session_dir("/repo", "legacy")
    mirror = backup_root / "projects" / legacy_path.parent.name / "legacy"
    assert not session_dir.exists()
    assert not mirror.exists()


def test_critical_backup_blocks_unmarked_directory_session_without_mutating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = storage.session_dir("/repo", "legacy-dir")
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text('{"role":"user","content":"old"}\n', encoding="utf-8")

    with pytest.raises(SessionBackupBlocked, match="supported session layout"):
        SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
            "/repo",
            "legacy-dir",
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
        )

    mirror = backup_root / "projects" / session_dir.parent.name / "legacy-dir"
    assert not (session_dir / ".backup-state.json").exists()
    assert not (session_dir / ".backup-lock").exists()
    assert not mirror.exists()


def test_tmp_named_session_files_are_mirrored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    nested_tmp = session_dir / "nested.tmp"
    nested_tmp.mkdir(parents=True)
    (nested_tmp / "inside.txt").write_text("keep nested\n", encoding="utf-8")
    tool_results = session_dir / "tool-results"
    tool_results.mkdir(parents=True)
    (tool_results / "foo.tmp").write_text("keep tmp file\n", encoding="utf-8")
    (session_dir / "session.jsonl").write_text("keep\n", encoding="utf-8")

    SessionBackupService(session_storage=storage).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.PIPELINE_STEP_COMPLETED,
        critical=True,
    )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "keep\n"
    assert (mirror / "nested.tmp" / "inside.txt").read_text(encoding="utf-8") == "keep nested\n"
    assert (mirror / "tool-results" / "foo.tmp").read_text(encoding="utf-8") == "keep tmp file\n"


def test_backup_skips_reparse_point_like_source_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    junction_like = session_dir / "junction"
    junction_like.mkdir()
    (junction_like / "outside.txt").write_text("must not copy\n", encoding="utf-8")
    service = SessionBackupService(session_storage=storage)
    monkeypatch.setattr(service, "_is_reparse_point", lambda path: Path(path) == junction_like)

    service.backup_session("/repo", "s1", reason=BackupReason.PIPELINE_STEP_COMPLETED, critical=True)

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"
    assert not (mirror / "junction").exists()


def test_backup_deletes_stale_reparse_point_like_destination_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    mirror.mkdir(parents=True)
    stale_reparse = mirror / "stale-junction"
    stale_reparse.mkdir()
    service = SessionBackupService(session_storage=storage)
    monkeypatch.setattr(service, "_is_reparse_point", lambda path: Path(path) == stale_reparse)

    service.backup_session("/repo", "s1", reason=BackupReason.PIPELINE_STEP_COMPLETED, critical=True)

    assert not stale_reparse.exists()
    assert (mirror / "session.jsonl").read_text(encoding="utf-8") == "fresh\n"


def test_backup_rejects_reparse_point_like_destination_ancestry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    projects_dir = backup_root / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("fresh\n", encoding="utf-8")
    service = SessionBackupService(session_storage=storage, retry_delays=())
    monkeypatch.setattr(service, "_is_reparse_point", lambda path: Path(path) == projects_dir)

    with pytest.raises(SessionBackupBlocked, match="destination ancestry"):
        service.backup_session("/repo", "s1", reason=BackupReason.PIPELINE_STEP_COMPLETED, critical=True)

    assert not (projects_dir / session_dir.parent.name / "s1" / "session.jsonl").exists()


def test_unlink_does_not_make_symlink_target_writable(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    _symlink_or_skip(target, link)
    service = SessionBackupService()
    calls: list[Path] = []
    service._make_writable = calls.append  # type: ignore[method-assign]

    service._unlink(link)

    assert calls == []
    assert target.read_text(encoding="utf-8") == "target\n"
    assert not link.exists()


def test_backup_uses_short_temp_prefix_for_long_file_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    long_name = "a" * 245
    (session_dir / long_name).write_text("long\n", encoding="utf-8")

    SessionBackupService(session_storage=storage, retry_delays=()).backup_session(
        "/repo",
        "s1",
        reason=BackupReason.PIPELINE_STEP_COMPLETED,
        critical=True,
    )

    mirror = backup_root / "projects" / session_dir.parent.name / "s1"
    assert (mirror / long_name).read_text(encoding="utf-8") == "long\n"


def test_marker_write_failure_does_not_mask_original_backup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    service = SessionBackupService(session_storage=storage, retry_delays=())

    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed")

    def fail_marker(*_args, **_kwargs):
        raise OSError("marker failed")

    monkeypatch.setattr(service, "_copy_file", fail_copy)
    monkeypatch.setattr(service, "_write_marker", fail_marker)

    with pytest.raises(SessionBackupBlocked, match="copy failed"):
        service.backup_session("/repo", "s1", reason=BackupReason.INPUT_REQUIRED, critical=True)


def test_critical_backup_blocked_does_not_chain_original_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    service = SessionBackupService(session_storage=storage, retry_delays=())

    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed at /private/mount/secret/session")

    monkeypatch.setattr(service, "_copy_file", fail_copy)

    with pytest.raises(SessionBackupBlocked) as exc_info:
        service.backup_session("/repo", "s1", reason=BackupReason.INPUT_REQUIRED, critical=True)

    assert exc_info.value.__cause__ is None
    assert "/private/mount" not in str(exc_info.value)


def test_critical_backup_blocked_exposes_retry_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
    session_dir = _create_v2_session_dir(storage, "/repo", "s1")
    (session_dir / "session.jsonl").write_text("one\n", encoding="utf-8")
    service = SessionBackupService(session_storage=storage, retry_delays=(0, 0))

    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(service, "_copy_file", fail_copy)

    with pytest.raises(SessionBackupBlocked) as exc_info:
        service.backup_session("/repo", "s1", reason=BackupReason.INPUT_REQUIRED, critical=True)

    assert exc_info.value.retry_count == 2
    assert exc_info.value.result is not None
    assert exc_info.value.result.retry_count == 2
    assert exc_info.value.result.succeeded is False
