from pathlib import Path

import pytest

from iac_code.a2a.backup import backup_session_async
from iac_code.services.session_backup import BackupReason, BackupResult


class RecordingStagedBackupService:
    def __init__(self) -> None:
        self.wait_calls = 0

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
        del result, timeout
        self.wait_calls += 1
        raise AssertionError("A2A terminal backup must not wait for shared publication")


@pytest.mark.asyncio
async def test_terminal_backup_returns_after_local_staging_without_shared_wait() -> None:
    service = RecordingStagedBackupService()

    result = await backup_session_async(
        service,
        "/repo",
        "session",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert result.staged_committed is True
    assert result.shared_committed is False
    assert service.wait_calls == 0


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
