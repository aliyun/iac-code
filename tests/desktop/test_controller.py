from __future__ import annotations

import asyncio

import pytest

from iac_code.desktop.controller import DesktopRuntimeController
from iac_code.web.session_manager import WebSessionManager


@pytest.mark.asyncio
async def test_close_state_counts_a_turn_once_when_task_and_lock_overlap(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    session = manager.create_session(cwd=str(project))
    controller = DesktopRuntimeController(manager, project)
    blocker = asyncio.get_running_loop().create_future()
    session.active_turn_task = blocker

    await session.turn_lock.acquire()
    try:
        state = controller.prepare_close()
    finally:
        session.turn_lock.release()
        blocker.cancel()

    assert state == {
        "type": "close-state",
        "activeWorkCount": 1,
        "awaitingUserInputCount": 0,
        "quiescing": True,
    }
    assert controller.accepts_external_submission() is False
    assert controller.resume() == {
        "type": "resumed",
        "activeWorkCount": 0,
        "awaitingUserInputCount": 0,
        "quiescing": False,
    }


def test_force_shutdown_runs_cooperative_cancel_and_cancels_slot_owner(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    controller = DesktopRuntimeController(manager, project)
    events: list[str] = []

    class Owner:
        def cancel(self) -> None:
            events.append("task-cancel")

    owner = Owner()
    assert controller.reserve_slot("prerequisite_install", owner, cancel=lambda: events.append("cooperative-cancel"))

    controller.commit_shutdown(force=True)

    assert events == ["cooperative-cancel", "task-cancel"]
    assert controller.committed_shutdown is True
