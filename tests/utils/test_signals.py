# tests/utils/test_signals.py
from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest


class TestInstallSignalHandler:
    def test_unix_path_uses_loop_add_signal_handler(self):
        from iac_code.utils.signals import install_signal_handler

        loop = MagicMock()
        handler = MagicMock()

        remove = install_signal_handler(loop, signal.SIGINT, handler)

        loop.add_signal_handler.assert_called_once_with(signal.SIGINT, handler)
        assert callable(remove)

    def test_unix_remove_calls_loop_remove(self):
        from iac_code.utils.signals import install_signal_handler

        loop = MagicMock()
        handler = MagicMock()

        remove = install_signal_handler(loop, signal.SIGINT, handler)
        remove()

        loop.remove_signal_handler.assert_called_once_with(signal.SIGINT)

    def test_windows_fallback_when_not_implemented(self):
        from iac_code.utils.signals import install_signal_handler

        loop = MagicMock()
        loop.add_signal_handler.side_effect = NotImplementedError("Windows")
        handler = MagicMock()

        with patch("signal.getsignal", return_value=signal.SIG_DFL):
            with patch("signal.signal") as mock_signal:
                remove = install_signal_handler(loop, signal.SIGINT, handler)
                mock_signal.assert_called_once()
                assert callable(remove)

    def test_windows_fallback_when_runtime_error(self):
        from iac_code.utils.signals import install_signal_handler

        loop = MagicMock()
        loop.add_signal_handler.side_effect = RuntimeError("no signals on this loop")
        handler = MagicMock()

        with patch("signal.getsignal", return_value=signal.SIG_DFL):
            with patch("signal.signal"):
                remove = install_signal_handler(loop, signal.SIGINT, handler)
                assert callable(remove)


def _fake_sys_exit(code):
    raise SystemExit(code)


class TestReraiseDefault:
    """reraise_default: cross-platform signal re-raise."""

    @patch("iac_code.utils.signals.sys")
    def test_windows_exits_with_128_plus_signum(self, mock_sys):
        from iac_code.utils.signals import reraise_default

        mock_sys.platform = "win32"
        mock_sys.exit.side_effect = _fake_sys_exit
        with pytest.raises(SystemExit) as exc_info:
            reraise_default(signal.SIGINT)
        assert exc_info.value.code == 128 + signal.SIGINT

    @patch("iac_code.utils.signals.sys")
    def test_windows_sigterm_exits_with_128_plus_15(self, mock_sys):
        from iac_code.utils.signals import reraise_default

        mock_sys.platform = "win32"
        mock_sys.exit.side_effect = _fake_sys_exit
        with pytest.raises(SystemExit) as exc_info:
            reraise_default(signal.SIGTERM)
        assert exc_info.value.code == 128 + signal.SIGTERM

    @patch("iac_code.utils.signals.os")
    @patch("iac_code.utils.signals.signal")
    @patch("iac_code.utils.signals.sys")
    def test_unix_uses_sig_dfl_and_oskill(self, mock_sys, mock_signal, mock_os):
        from iac_code.utils.signals import reraise_default

        mock_sys.platform = "linux"
        mock_os.getpid.return_value = 12345

        reraise_default(2)

        mock_signal.signal.assert_called_once_with(2, mock_signal.SIG_DFL)
        mock_os.kill.assert_called_once_with(12345, 2)
