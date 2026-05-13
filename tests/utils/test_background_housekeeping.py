import os
import time

from iac_code.utils.background_housekeeping import start_background_housekeeping


class TestStartBackgroundHousekeeping:
    def test_calls_cleanup_after_delay(self, tmp_path):
        """验证后台线程启动并调用 cleanup_old_session_files。"""
        base_dir = str(tmp_path)
        # 创建一个过期文件
        session_dir = os.path.join(base_dir, "sess")
        os.makedirs(session_dir)
        old_file = os.path.join(session_dir, "t.txt")
        with open(old_file, "w") as f:
            f.write("data")
        old_time = time.time() - 31 * 86400
        os.utime(old_file, (old_time, old_time))

        # 用 delay=0 立即执行
        thread = start_background_housekeeping(base_dir=base_dir, delay_seconds=0)
        thread.join(timeout=5)

        assert not os.path.exists(old_file)

    def test_does_not_block_caller(self, tmp_path):
        """start_background_housekeeping 应立即返回（daemon 线程）。"""
        thread = start_background_housekeeping(base_dir=str(tmp_path), delay_seconds=9999)
        assert thread.daemon is True
        # 不等待，直接验证线程是 daemon

    def test_no_error_on_missing_dir(self):
        """base_dir 不存在时不报错。"""
        thread = start_background_housekeeping(base_dir="/nonexistent/cleanup/dir", delay_seconds=0)
        thread.join(timeout=5)
        # 没有异常即通过
