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
