import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from iac_code.a2a.backup import backup_session_async
from iac_code.services.session_backup import BackupReason, BackupResult, SessionBackupBlocked


class RecordingStagedBackupService:
    def __init__(self) -> None:
        self.wait_started = threading.Event()
        self.allow_shared_commit = threading.Event()
        self.wait_calls = 0
        self.wait_timeout: float | None = None

    def backup_session(self, *_args, **_kwargs) -> BackupResult:
        return BackupResult(
            enabled=True,
            destination=Path("/tmp/staging/projects/project/session_v1"),
            generation=1,
            commit_id="commit-1",
            staged_committed=True,
            shared_committed=False,
        )

    def wait_for_shared_commit(
        self,
        result: BackupResult,
        *,
        timeout: float | None = None,
    ) -> BackupResult:
        self.wait_calls += 1
        self.wait_timeout = timeout
        self.wait_started.set()
        if not self.allow_shared_commit.wait(timeout=timeout):
            raise SessionBackupBlocked(
                "Timed out waiting for staged session backup publication.",
                retry_count=result.retry_count,
                result=result,
            )
        return replace(
            result,
            destination=Path("/oss/projects/project/session"),
            shared_committed=True,
        )


@pytest.mark.asyncio
async def test_terminal_backup_waits_for_staged_snapshot_shared_commit() -> None:
    service = RecordingStagedBackupService()

    backup_task = asyncio.create_task(
        backup_session_async(
            service,
            "/repo",
            "session",
            reason=BackupReason.TERMINAL,
            critical=True,
        )
    )

    assert await asyncio.to_thread(service.wait_started.wait, 1.0)
    assert backup_task.done() is False
    service.allow_shared_commit.set()
    result = await asyncio.wait_for(backup_task, timeout=1.0)

    assert result.shared_committed is True
    assert result.destination == Path("/oss/projects/project/session")
    assert service.wait_calls == 1


@pytest.mark.asyncio
async def test_terminal_backup_bounds_stalled_shared_publication() -> None:
    service = RecordingStagedBackupService()

    with pytest.raises(
        SessionBackupBlocked,
        match="Timed out waiting for staged session backup publication",
    ):
        await asyncio.wait_for(
            backup_session_async(
                service,
                "/repo",
                "session",
                reason=BackupReason.TERMINAL,
                critical=True,
                shared_commit_timeout=0.01,
            ),
            timeout=0.5,
        )

    assert service.wait_timeout == 0.01
    assert service.wait_calls == 1


@pytest.mark.asyncio
async def test_non_terminal_backup_keeps_existing_local_staging_behavior() -> None:
    service = RecordingStagedBackupService()

    result = await backup_session_async(
        service,
        "/repo",
        "session",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    assert result.staged_committed is True
    assert result.shared_committed is False
    assert service.wait_calls == 0
