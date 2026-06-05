import asyncio
import contextlib

import pytest

from iac_code.tasks.task_state import TaskManager, TaskStatus


class TestTaskManager:
    def test_register(self):
        mgr = TaskManager()
        tid = mgr.register(description="Find TODOs", agent_type="explore")
        task = mgr.get(tid)
        assert task.status == TaskStatus.RUNNING
        assert task.description == "Find TODOs"

    def test_complete(self):
        mgr = TaskManager()
        tid = mgr.register(description="test")
        mgr.complete(tid, result="Done")
        assert mgr.get(tid).status == TaskStatus.COMPLETED

    def test_fail(self):
        mgr = TaskManager()
        tid = mgr.register(description="test")
        mgr.fail(tid, error="Boom")
        assert mgr.get(tid).status == TaskStatus.FAILED

    def test_stop(self):
        mgr = TaskManager()
        tid = mgr.register(description="test")
        mgr.stop(tid)
        assert mgr.get(tid).status == TaskStatus.STOPPED

    def test_list_all(self):
        mgr = TaskManager()
        mgr.register(description="a")
        mgr.register(description="b")
        assert len(mgr.list_all()) == 2

    def test_update_progress(self):
        mgr = TaskManager()
        tid = mgr.register(description="test")
        mgr.update_progress(tid, tool_use_count=5, token_count=1000)
        assert mgr.get(tid).tool_use_count == 5

    @pytest.mark.asyncio
    async def test_stop_cancels_registered_asyncio_task(self):
        mgr = TaskManager()
        tid = mgr.register(description="test")

        async def wait_forever():
            await asyncio.Event().wait()

        background_task = asyncio.create_task(wait_forever())
        try:
            mgr.attach_task(tid, background_task)
            mgr.stop(tid)
            await asyncio.sleep(0)

            assert mgr.get(tid).status == TaskStatus.STOPPED
            assert background_task.cancelled()
        finally:
            background_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_task

    def test_complete_and_fail_do_not_override_stopped_task(self):
        mgr = TaskManager()
        tid = mgr.register(description="test")

        mgr.stop(tid)
        mgr.complete(tid, result="done")
        mgr.fail(tid, error="boom")

        task = mgr.get(tid)
        assert task.status == TaskStatus.STOPPED
        assert task.result is None
        assert task.error is None
