import os

import pytest

from iac_code.tools.result_storage import ResultStorage


@pytest.fixture
def storage(tmp_path):
    return ResultStorage(storage_dir=str(tmp_path), max_inline_chars=100, preview_chars=50)


class TestResultStorage:
    def test_small_result_inline(self, storage):
        result = storage.process(tool_use_id="t1", content="short")
        assert result.content == "short"
        assert result.is_externalized is False

    def test_large_result_externalized(self, storage):
        content = "x" * 1000
        result = storage.process(tool_use_id="t2", content=content)
        assert result.is_externalized is True
        assert len(result.content) < len(content)
        assert result.file_path is not None
        assert os.path.exists(result.file_path)

    def test_externalized_file_content(self, storage):
        content = "line\n" * 100
        result = storage.process(tool_use_id="t3", content=content)
        with open(result.file_path) as f:
            assert f.read() == content

    def test_preview_has_truncation_notice(self, storage):
        content = "y" * 200
        result = storage.process(tool_use_id="t4", content=content)
        assert "truncated" in result.content.lower() or "..." in result.content
