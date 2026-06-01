import os
import sys
from pathlib import Path

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

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
    def test_externalized_file_is_owner_only(self, tmp_path):
        storage = ResultStorage(storage_dir=str(tmp_path / "tool-results"), max_inline_chars=1)

        result = storage.process(tool_use_id="private", content="long output")

        file_path = Path(result.file_path)
        assert oct(file_path.parent.stat().st_mode & 0o777) == "0o700"
        assert oct(file_path.stat().st_mode & 0o777) == "0o600"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
    def test_externalized_file_restricts_tool_results_root_and_session_dir(self, tmp_path):
        storage = ResultStorage(storage_dir=str(tmp_path / "tool-results" / "session-1"), max_inline_chars=1)

        result = storage.process(tool_use_id="private", content="long output")

        file_path = Path(result.file_path)
        assert oct((tmp_path / "tool-results").stat().st_mode & 0o777) == "0o700"
        assert oct(file_path.parent.stat().st_mode & 0o777) == "0o700"

    @pytest.mark.parametrize("tool_use_id", ["../escape", "a/b", r"a\b", "/tmp/escape", "", "."])
    def test_externalized_file_cannot_escape_storage_dir(self, tmp_path, tool_use_id):
        storage_dir = tmp_path / "tool-results"
        storage = ResultStorage(storage_dir=str(storage_dir), max_inline_chars=1)

        result = storage.process(tool_use_id=tool_use_id, content="long output")

        assert result.file_path is not None
        file_path = Path(result.file_path)
        assert file_path.parent == storage_dir
        assert not (tmp_path / "escape.txt").exists()
        assert file_path.name.endswith(".txt")

    def test_externalized_file_content(self, storage):
        content = "line\n" * 100
        result = storage.process(tool_use_id="t3", content=content)
        with open(result.file_path) as f:
            assert f.read() == content

    def test_preview_has_truncation_notice(self, storage):
        content = "y" * 200
        result = storage.process(tool_use_id="t4", content=content)
        assert "truncated" in result.content.lower() or "..." in result.content
