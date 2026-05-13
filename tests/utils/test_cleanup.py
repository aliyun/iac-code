import os
import time

import pytest

from iac_code.utils.cleanup import cleanup_old_session_files

DEFAULT_CLEANUP_PERIOD_DAYS = 30


@pytest.fixture
def base_dir(tmp_path):
    return str(tmp_path)


class TestCleanupOldSessionFiles:
    def test_removes_expired_files(self, base_dir):
        """超过 30 天的结果文件应被删除。"""
        session_dir = os.path.join(base_dir, "sess1")
        os.makedirs(session_dir)
        old_file = os.path.join(session_dir, "tool1.txt")
        with open(old_file, "w") as f:
            f.write("old result")
        # 将 mtime 设为 31 天前
        old_time = time.time() - (DEFAULT_CLEANUP_PERIOD_DAYS + 1) * 86400
        os.utime(old_file, (old_time, old_time))

        result = cleanup_old_session_files(base_dir)

        assert not os.path.exists(old_file)
        assert result["deleted"] >= 1
        # 空 session 目录也应被清理
        assert not os.path.exists(session_dir)

    def test_keeps_recent_files(self, base_dir):
        """未过期的文件应保留。"""
        session_dir = os.path.join(base_dir, "sess2")
        os.makedirs(session_dir)
        recent_file = os.path.join(session_dir, "tool2.txt")
        with open(recent_file, "w") as f:
            f.write("recent result")

        result = cleanup_old_session_files(base_dir)

        assert os.path.exists(recent_file)
        assert result["deleted"] == 0

    def test_handles_missing_base_dir(self):
        """base_dir 不存在时不应报错。"""
        result = cleanup_old_session_files("/nonexistent/path")
        assert result["deleted"] == 0
        assert result["errors"] == 0

    def test_removes_empty_session_dirs(self, base_dir):
        """空的 session 目录应被清理。"""
        empty_dir = os.path.join(base_dir, "empty-sess")
        os.makedirs(empty_dir)

        cleanup_old_session_files(base_dir)

        assert not os.path.exists(empty_dir)

    def test_mixed_sessions(self, base_dir):
        """同时存在过期和未过期的 session。"""
        # 过期 session
        old_session = os.path.join(base_dir, "old-sess")
        os.makedirs(old_session)
        old_file = os.path.join(old_session, "t1.txt")
        with open(old_file, "w") as f:
            f.write("old")
        old_time = time.time() - (DEFAULT_CLEANUP_PERIOD_DAYS + 1) * 86400
        os.utime(old_file, (old_time, old_time))

        # 未过期 session
        new_session = os.path.join(base_dir, "new-sess")
        os.makedirs(new_session)
        new_file = os.path.join(new_session, "t2.txt")
        with open(new_file, "w") as f:
            f.write("new")

        result = cleanup_old_session_files(base_dir)

        assert not os.path.exists(old_file)
        assert os.path.exists(new_file)
        assert result["deleted"] == 1
