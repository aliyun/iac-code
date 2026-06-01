import sys

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

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
    def test_memory_files_are_owner_only(self, manager, tmp_path):
        manager.save("private", content="Data", memory_type="project", description="Index test")

        memory_file = tmp_path / "private.md"
        index_file = tmp_path / "MEMORY.md"
        assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"
        assert oct(memory_file.stat().st_mode & 0o777) == "0o600"
        assert oct(index_file.stat().st_mode & 0o777) == "0o600"

    def test_prompt_content(self, manager):
        manager.save("m1", content="Rule 1", memory_type="feedback", description="R")
        assert "Rule 1" in manager.get_prompt_content()

    @pytest.mark.parametrize("name", ["", "   ", ".", "..", "../escape", "a/b", r"a\b", "/tmp/escape", "bad name"])
    def test_save_rejects_invalid_memory_names(self, manager, tmp_path, name):
        outside = tmp_path.parent / "escape.md"

        with pytest.raises(ValueError, match="Invalid memory name"):
            manager.save(name, content="X", memory_type="user", description="bad")

        assert not outside.exists()

    @pytest.mark.parametrize("name", ["../escape", "a/b", r"a\b", "/tmp/escape"])
    def test_load_and_delete_reject_invalid_memory_names(self, manager, name):
        with pytest.raises(ValueError, match="Invalid memory name"):
            manager.load(name)
        with pytest.raises(ValueError, match="Invalid memory name"):
            manager.delete(name)

    @pytest.mark.parametrize("name", ["project-note", "user_1", "release.2026"])
    def test_accepts_safe_memory_names(self, manager, name):
        manager.save(name, content="safe", memory_type="user", description="ok")

        mem = manager.load(name)

        assert mem is not None
        assert mem["content"] == "safe"

    @pytest.mark.parametrize("name", ["MEMORY", "memory"])
    def test_rejects_reserved_index_memory_names(self, manager, name):
        with pytest.raises(ValueError, match="Invalid memory name"):
            manager.save(name, content="X", memory_type="user", description="reserved")

    def test_legacy_invalid_memory_file_does_not_break_listing_or_index_update(self, manager, tmp_path):
        legacy = tmp_path / "old memory.md"
        legacy.write_text("---\nname: old memory\ndescription: Legacy\ntype: user\n---\n\nlegacy content\n")

        memories = manager.list_memories()
        manager.save("new-safe", content="new content", memory_type="user", description="New")

        assert [memory["name"] for memory in memories] == ["old memory"]
        assert "old memory" in manager.get_index_content()
        assert "new-safe" in manager.get_index_content()
