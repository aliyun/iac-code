"""Desktop-only close admission and active-work accounting."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from iac_code.web.session_manager import WebSessionManager


class DesktopRuntimeController:
    """Small event-loop-owned controller for Host lifecycle requests.

    All mutating methods are intentionally synchronous.  The control reader posts
    them onto the Web event loop, so entering quiescing and sampling work is one
    loop callback rather than an asynchronous check-then-act sequence.
    """

    def __init__(self, manager: WebSessionManager, default_project_cwd: Path) -> None:
        self.manager = manager
        self.default_project_cwd = default_project_cwd
        self.quiescing = False
        self.committed_shutdown = False
        self.dynamic_suggestions: set[asyncio.Future[Any]] = set()
        self.runtime_lifecycle: set[asyncio.Future[Any]] = set()
        self._fixed_slots: dict[str, object | None] = {
            "git_bash_install": None,
            "prerequisite_install": None,
            "prerequisite_probe": None,
            "desktop_diagnostics": None,
        }
        self._fixed_slot_cancel: dict[str, Callable[[], None] | None] = dict.fromkeys(self._fixed_slots)

    def set_default_project(self, path: str) -> Path:
        project = Path(path).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise ValueError("selected project must be a directory")
        self.default_project_cwd = project
        self.manager.cwd = project
        return project

    def prepare_close(self) -> dict[str, Any]:
        self.quiescing = True
        return self.close_state()

    def resume(self) -> dict[str, Any]:
        if not self.committed_shutdown:
            self.quiescing = False
        return {**self.close_state(), "type": "resumed"}

    def commit_shutdown(self, *, force: bool = False) -> None:
        self.quiescing = True
        self.committed_shutdown = True
        if force:
            self.cancel_fixed_work()

    def accepts_external_submission(self) -> bool:
        return not self.quiescing and not self.committed_shutdown

    def reserve_slot(self, name: str, owner: object, *, cancel: Callable[[], None] | None = None) -> bool:
        if name not in self._fixed_slots:
            raise ValueError("unknown Desktop work slot")
        if self.quiescing or self._fixed_slots[name] is not None:
            return False
        self._fixed_slots[name] = owner
        self._fixed_slot_cancel[name] = cancel
        return True

    def release_slot(self, name: str, owner: object) -> None:
        if self._fixed_slots.get(name) is owner:
            self._fixed_slots[name] = None
            self._fixed_slot_cancel[name] = None

    def cancel_fixed_work(self) -> None:
        """Cooperatively stop the exhaustive Desktop-only work slots."""
        for name, owner in tuple(self._fixed_slots.items()):
            if owner is None:
                continue
            cancel = self._fixed_slot_cancel[name]
            if cancel is not None:
                cancel()
            task_cancel = getattr(owner, "cancel", None)
            if callable(task_cancel):
                task_cancel()

    def close_state(self) -> dict[str, Any]:
        active_work = sum(owner is not None for owner in self._fixed_slots.values())
        active_work += sum(not task.done() for task in self.dynamic_suggestions)
        active_work += sum(not task.done() for task in self.runtime_lifecycle)
        awaiting_user = 0
        for session in self.manager.loaded_sessions():
            turn_task_active = session.active_turn_task is not None and not session.active_turn_task.done()
            turn_active = turn_task_active or session.turn_lock.locked()
            active_work += int(turn_active)
            active_work += sum(not task.done() for task in session.active_local_tasks)
            active_work += len(session.queued_inputs)
            awaiting_user += len(session.pending_permissions)
            awaiting_user += len(session.pending_questions)
            awaiting_user += len(session.pending_elicitations)
        return {
            "type": "close-state",
            "activeWorkCount": active_work,
            "awaitingUserInputCount": awaiting_user,
            "quiescing": self.quiescing,
        }
