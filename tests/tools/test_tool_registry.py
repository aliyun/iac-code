from iac_code.tools.base import ToolRegistry


class TestToolRegistryExclude:
    def test_exclude_removes_named_tools(self):
        registry = ToolRegistry()
        registry.register_default_tools()
        excluded = registry.exclude(["bash"])
        assert excluded.get("bash") is None
        assert excluded.get("read_file") is not None
        assert excluded.get("edit_file") is not None

    def test_exclude_empty_list_clones_all(self):
        registry = ToolRegistry()
        registry.register_default_tools()
        excluded = registry.exclude([])
        assert excluded.get("bash") is not None
        assert excluded.get("read_file") is not None

    def test_exclude_nonexistent_name_ignored(self):
        registry = ToolRegistry()
        registry.register_default_tools()
        excluded = registry.exclude(["nonexistent_tool"])
        assert len(excluded.list_tools()) == len(registry.list_tools())

    def test_exclude_multiple(self):
        registry = ToolRegistry()
        registry.register_default_tools()
        excluded = registry.exclude(["bash", "write_file", "edit_file"])
        assert excluded.get("bash") is None
        assert excluded.get("write_file") is None
        assert excluded.get("edit_file") is None
        assert excluded.get("read_file") is not None
        assert excluded.get("glob") is not None
