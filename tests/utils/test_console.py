# tests/utils/test_console.py
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_console_detection_cache(monkeypatch):
    from iac_code.utils import console as console_mod

    if hasattr(console_mod, "_stdout_initial_mode"):
        monkeypatch.setattr(console_mod, "_stdout_initial_mode", None)
    if hasattr(console_mod, "_stdout_initial_mode_known"):
        monkeypatch.setattr(console_mod, "_stdout_initial_mode_known", False)
    for env_var in ("WT_SESSION", "TERM_PROGRAM", "VSCODE_PID", "ANSICON", "ConEmuANSI"):
        monkeypatch.delenv(env_var, raising=False)


class TestEnableVirtualTerminal:
    @patch("iac_code.utils.console.sys")
    def test_noop_on_non_windows(self, mock_sys):
        """On non-Windows platforms, the function returns immediately without
        touching ctypes (which on Linux/macOS lacks ``windll``)."""
        from iac_code.utils.console import enable_virtual_terminal

        mock_sys.platform = "linux"
        # If it tried to access ctypes.windll on linux it would raise,
        # so reaching the end of the call already verifies the early return.
        enable_virtual_terminal()

    @patch("iac_code.utils.console.sys")
    def test_sets_vt_flag_on_stdout_only(self, mock_sys):
        """On Windows the function must:
        1. call GetStdHandle with -11 (STD_OUTPUT_HANDLE);
        2. OR the VT processing flag (0x0004) onto the *existing* stdout
           console mode bits — not replace them;
        3. NOT enable ENABLE_VIRTUAL_TERMINAL_INPUT on stdin (it breaks
           msvcrt.getwch() by converting extended keys to ANSI escapes).
        """
        mock_sys.platform = "win32"

        stdout_handle = object()

        fake_kernel32 = MagicMock()
        fake_kernel32.GetStdHandle.return_value = stdout_handle

        def gcm_side_effect(_handle, mode_ptr):
            mode_ptr._obj.value = 0x0008
            return 1

        fake_kernel32.GetConsoleMode.side_effect = gcm_side_effect
        fake_kernel32.SetConsoleMode.return_value = 1

        fake_ctypes = self._fake_ctypes(fake_kernel32)
        with patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            from iac_code.utils.console import enable_virtual_terminal

            enable_virtual_terminal()

        # Only STD_OUTPUT_HANDLE requested.
        fake_kernel32.GetStdHandle.assert_called_once_with(-11)

        set_calls = fake_kernel32.SetConsoleMode.call_args_list
        assert len(set_calls) == 1

        # stdout: existing 0x0008 must be preserved AND VT processing (0x0004) added.
        stdout_call = set_calls[0]
        assert stdout_call.args[0] is stdout_handle
        assert stdout_call.args[1] == 0x0008 | 0x0004

    @patch("iac_code.utils.console.sys")
    def test_get_console_mode_failure_skips_set(self, mock_sys):
        """If GetConsoleMode returns 0 (failure — e.g. handle is not a real
        console), SetConsoleMode must not be called for that handle."""
        mock_sys.platform = "win32"

        fake_kernel32 = MagicMock()
        fake_kernel32.GetStdHandle.return_value = object()
        fake_kernel32.GetConsoleMode.return_value = 0  # always fails
        fake_kernel32.SetConsoleMode.return_value = 1

        fake_ctypes = self._fake_ctypes(fake_kernel32)
        with patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            from iac_code.utils.console import enable_virtual_terminal

            enable_virtual_terminal()

        fake_kernel32.SetConsoleMode.assert_not_called()

    @patch("iac_code.utils.console.sys")
    def test_attribute_error_on_windll_swallowed(self, mock_sys):
        """When the platform claims to be win32 but ctypes lacks ``windll``
        (e.g. running these tests on macOS/Linux without a real win32), the
        AttributeError must be caught — the function must not crash."""
        mock_sys.platform = "win32"

        # Fake ctypes WITHOUT a windll attribute → AttributeError on access.
        fake_ctypes = types.ModuleType("ctypes")
        with patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            from iac_code.utils.console import enable_virtual_terminal

            enable_virtual_terminal()  # must not raise

    @staticmethod
    def _fake_ctypes(fake_kernel32) -> types.ModuleType:
        """Build a stand-in ``ctypes`` module: real c_ulong/byref, fake windll."""
        import ctypes as real_ctypes

        fake_ctypes = types.ModuleType("ctypes")
        fake_ctypes.windll = types.SimpleNamespace(kernel32=fake_kernel32)  # type: ignore[attr-defined]
        fake_ctypes.c_ulong = real_ctypes.c_ulong  # type: ignore[attr-defined]
        fake_ctypes.byref = real_ctypes.byref  # type: ignore[attr-defined]
        fake_ctypes.c_int = real_ctypes.c_int  # type: ignore[attr-defined]
        fake_ctypes.c_void_p = real_ctypes.c_void_p  # type: ignore[attr-defined]
        fake_ctypes.c_bool = real_ctypes.c_bool  # type: ignore[attr-defined]
        fake_ctypes.POINTER = real_ctypes.POINTER  # type: ignore[attr-defined]
        return fake_ctypes


class TestEnableVirtualTerminalTypeDeclarations:
    """Phase 2 fix-forward: declares ctypes argtypes/restype to prevent
    64-bit HANDLE truncation and gates calls on a valid handle."""

    def test_declares_argtypes_and_restype(self, monkeypatch):
        import ctypes
        import types as _types
        from unittest.mock import MagicMock

        from iac_code.utils import console as console_mod

        mock_kernel32 = MagicMock()
        mock_kernel32.GetStdHandle.return_value = 12345
        mock_kernel32.GetConsoleMode.return_value = True

        monkeypatch.setattr("iac_code.utils.console.sys.platform", "win32")
        monkeypatch.setattr(ctypes, "windll", _types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        console_mod.enable_virtual_terminal()

        assert mock_kernel32.GetStdHandle.argtypes == [ctypes.c_int]
        assert mock_kernel32.GetStdHandle.restype == ctypes.c_void_p

    def test_skips_when_get_std_handle_returns_none(self, monkeypatch):
        """When GetStdHandle returns None (no console), don't call GetConsoleMode."""
        import ctypes
        import types as _types
        from unittest.mock import MagicMock

        from iac_code.utils import console as console_mod

        mock_kernel32 = MagicMock()
        mock_kernel32.GetStdHandle.return_value = None
        monkeypatch.setattr("iac_code.utils.console.sys.platform", "win32")
        monkeypatch.setattr(ctypes, "windll", _types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        console_mod.enable_virtual_terminal()

        mock_kernel32.GetConsoleMode.assert_not_called()
        mock_kernel32.SetConsoleMode.assert_not_called()


class TestConsoleCapabilityQueries:
    def test_supports_virtual_terminal_false_when_win32_stdout_lacks_vt(self, monkeypatch):
        import ctypes
        import types as _types
        from unittest.mock import MagicMock

        from iac_code.utils import console as console_mod

        mock_kernel32 = MagicMock()
        mock_kernel32.GetStdHandle.return_value = 12345

        def get_console_mode(_handle, mode_ptr):
            mode_ptr._obj.value = 0x0008
            return True

        mock_kernel32.GetConsoleMode.side_effect = get_console_mode
        monkeypatch.setattr("iac_code.utils.console.sys.platform", "win32")
        monkeypatch.setattr(ctypes, "windll", _types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        assert console_mod.stdout_supports_virtual_terminal() is False
        assert console_mod.is_legacy_windows_console() is True
        assert console_mod.use_ascii_symbols() is True

    def test_supports_virtual_terminal_true_when_win32_stdout_has_vt(self, monkeypatch):
        import ctypes
        import types as _types
        from unittest.mock import MagicMock

        from iac_code.utils import console as console_mod

        mock_kernel32 = MagicMock()
        mock_kernel32.GetStdHandle.return_value = 12345

        def get_console_mode(_handle, mode_ptr):
            mode_ptr._obj.value = 0x0008 | 0x0004
            return True

        mock_kernel32.GetConsoleMode.side_effect = get_console_mode
        monkeypatch.setattr("iac_code.utils.console.sys.platform", "win32")
        monkeypatch.setattr(ctypes, "windll", _types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        assert console_mod.stdout_supports_virtual_terminal() is True
        assert console_mod.is_legacy_windows_console() is False
        assert console_mod.use_ascii_symbols() is False

    def test_ascii_fallback_survives_successful_vt_enable_in_classic_console(self, monkeypatch):
        import ctypes
        import types as _types
        from unittest.mock import MagicMock

        from iac_code.utils import console as console_mod

        mode_value = {"value": 0x0008}
        mock_kernel32 = MagicMock()
        mock_kernel32.GetStdHandle.return_value = 12345

        def get_console_mode(_handle, mode_ptr):
            mode_ptr._obj.value = mode_value["value"]
            return True

        def set_console_mode(_handle, new_mode):
            mode_value["value"] = int(new_mode)
            return True

        mock_kernel32.GetConsoleMode.side_effect = get_console_mode
        mock_kernel32.SetConsoleMode.side_effect = set_console_mode
        monkeypatch.setattr("iac_code.utils.console.sys.platform", "win32")
        monkeypatch.setattr(ctypes, "windll", _types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        console_mod.enable_virtual_terminal()

        assert console_mod.stdout_supports_virtual_terminal() is True
        assert console_mod.is_legacy_windows_console() is True
        assert console_mod.use_ascii_symbols() is True

    def test_modern_windows_terminal_env_is_not_legacy_console(self, monkeypatch):
        import ctypes
        import types as _types
        from unittest.mock import MagicMock

        from iac_code.utils import console as console_mod

        mock_kernel32 = MagicMock()
        mock_kernel32.GetStdHandle.return_value = 12345

        def get_console_mode(_handle, mode_ptr):
            mode_ptr._obj.value = 0x0008
            return True

        mock_kernel32.GetConsoleMode.side_effect = get_console_mode
        monkeypatch.setattr("iac_code.utils.console.sys.platform", "win32")
        monkeypatch.setattr(ctypes, "windll", _types.SimpleNamespace(kernel32=mock_kernel32), raising=False)
        monkeypatch.setenv("WT_SESSION", "fake-session")

        assert console_mod.is_legacy_windows_console() is False
        assert console_mod.use_ascii_symbols() is False

    def test_non_windows_is_not_legacy_and_supports_vt(self, monkeypatch):
        from iac_code.utils import console as console_mod

        monkeypatch.setattr("iac_code.utils.console.sys.platform", "linux")

        assert console_mod.stdout_supports_virtual_terminal() is True
        assert console_mod.is_legacy_windows_console() is False
        assert console_mod.use_ascii_symbols() is False
