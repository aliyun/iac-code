import os
import sys
from pathlib import Path

import pytest

from iac_code.services.session_layout import SESSION_LAYOUT_VERSION_V2, UnsupportedSessionLayoutError
from iac_code.services.session_metadata import SessionMetadata, write_session_metadata
from iac_code.tools.result_storage import ResultStorage


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


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

    def test_session_owned_storage_refuses_symlinked_tool_results_dir(self, tmp_path):
        session_dir = tmp_path / "session"
        write_session_metadata(
            session_dir,
            SessionMetadata(session_id="session", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        outside = tmp_path / "outside"
        outside.mkdir()
        _symlink_or_skip(outside, session_dir / "tool-results", target_is_directory=True)
        storage = ResultStorage(storage_dir=str(session_dir / "tool-results"), max_inline_chars=1)

        with pytest.raises(UnsupportedSessionLayoutError, match="Unsafe session-owned path"):
            storage.process(tool_use_id="tool-1", content="long output")

        assert not (outside / "tool-1.txt").exists()

    def test_session_owned_storage_refuses_symlinked_result_leaf(self, tmp_path):
        session_dir = tmp_path / "session"
        write_session_metadata(
            session_dir,
            SessionMetadata(session_id="session", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        storage_dir = session_dir / "tool-results"
        storage_dir.mkdir()
        outside = tmp_path / "outside-tool-result.txt"
        outside.write_text("outside content", encoding="utf-8")
        _symlink_or_skip(outside, storage_dir / "tool-1.txt")
        storage = ResultStorage(storage_dir=str(storage_dir), max_inline_chars=1)

        with pytest.raises(OSError, match="symlink|reparse"):
            storage.process(tool_use_id="tool-1", content="long output")

        assert outside.read_text(encoding="utf-8") == "outside content"

    def test_session_owned_storage_refuses_dangling_symlinked_metadata_before_fallback(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        _symlink_or_skip(tmp_path / "missing-metadata.json", session_dir / "metadata.json")
        outside = tmp_path / "outside"
        outside.mkdir()
        _symlink_or_skip(outside, session_dir / "tool-results", target_is_directory=True)
        storage = ResultStorage(storage_dir=str(session_dir / "tool-results"), max_inline_chars=1)

        with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
            storage.process(tool_use_id="tool-1", content="long output")

        assert not (outside / "tool-1.txt").exists()

    def test_externalized_file_content(self, storage):
        content = "line\n" * 100
        result = storage.process(tool_use_id="t3", content=content)
        with open(result.file_path) as f:
            assert f.read() == content

    def test_preview_has_truncation_notice(self, storage):
        content = "y" * 200
        result = storage.process(tool_use_id="t4", content=content)
        assert "truncated" in result.content.lower() or "..." in result.content
