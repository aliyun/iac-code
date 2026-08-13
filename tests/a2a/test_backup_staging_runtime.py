from pathlib import Path
from types import SimpleNamespace

import pytest

from iac_code.a2a.transports.dispatcher import A2ARuntimeComponents, create_runtime_components
from iac_code.services.session_backup_staging import StagedSessionBackupService


@pytest.mark.asyncio
async def test_a2a_runtime_injects_one_staged_backup_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", str(tmp_path / "staging"))

    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    executor = components.handler.agent_executor

    try:
        assert isinstance(executor._backup_service, StagedSessionBackupService)
        assert components.handler._backup_service is executor._backup_service
        assert components.task_store._backup_service is executor._backup_service
        assert components.backup_staging_process is not None
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_a2a_runtime_stops_backup_publisher_after_other_background_components() -> None:
    closed: list[str] = []

    class FakeTaskStore:
        async def stop_cleanup_loop(self) -> None:
            closed.append("task_store")

    class FakeExitStack:
        async def aclose(self) -> None:
            closed.append("exit_stack")

    class FakeBackupProcess:
        def close(self) -> None:
            closed.append("backup_publisher")

    components = A2ARuntimeComponents(
        handler=SimpleNamespace(agent_executor=None, _push_sender=None),
        task_store=FakeTaskStore(),
        card=None,
        app=None,
        _exit_stack=FakeExitStack(),
        backup_staging_process=FakeBackupProcess(),
    )

    await components.aclose()

    assert closed == ["task_store", "exit_stack", "backup_publisher"]
