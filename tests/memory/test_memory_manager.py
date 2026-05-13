import pytest

from iac_code.memory.memory_manager import MemoryManager


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(memory_dir=str(tmp_path))


class TestMemoryManager:
    def test_save_and_load(self, manager):
        manager.save("user_role", content="Senior dev", memory_type="user", description="Role")
        mem = manager.load("user_role")
        assert mem is not None
        assert "Senior dev" in mem["content"]
        assert mem["type"] == "user"

    def test_list(self, manager):
        manager.save("m1", content="A", memory_type="user", description="First")
        manager.save("m2", content="B", memory_type="feedback", description="Second")
        assert len(manager.list_memories()) == 2

    def test_delete(self, manager):
        manager.save("del", content="X", memory_type="user", description="Del")
        manager.delete("del")
        assert manager.load("del") is None

    def test_update(self, manager):
        manager.save("u", content="V1", memory_type="user", description="V1")
        manager.save("u", content="V2", memory_type="user", description="V2")
        assert "V2" in manager.load("u")["content"]

    def test_index(self, manager):
        manager.save("idx", content="Data", memory_type="project", description="Index test")
        assert "idx" in manager.get_index_content()

    def test_prompt_content(self, manager):
        manager.save("m1", content="Rule 1", memory_type="feedback", description="R")
        assert "Rule 1" in manager.get_prompt_content()
