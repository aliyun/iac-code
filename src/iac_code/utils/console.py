# src/iac_code/utils/console.py
"""Windows console Virtual Terminal Processing enablement."""

from __future__ import annotations

import os
import sys

STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_stdout_initial_mode_known = False
_stdout_initial_mode: int | None = None


def _configure_console_api(ctypes, kernel32) -> None:
    kernel32.GetStdHandle.argtypes = [ctypes.c_int]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetConsoleMode.restype = ctypes.c_bool
    kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.SetConsoleMode.restype = ctypes.c_bool


def _get_stdout_console_mode() -> int | None:
    """Return stdout console mode bits on Windows, or None when unavailable."""
    if sys.platform != "win32":
        return None

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        _configure_console_api(ctypes, kernel32)

        handle_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if handle_out and kernel32.GetConsoleMode(handle_out, ctypes.byref(mode)):
            return int(mode.value)
    except (AttributeError, OSError, ValueError):
        return None
    return None


def _remember_stdout_initial_mode(mode: int | None) -> None:
    global _stdout_initial_mode, _stdout_initial_mode_known
    if not _stdout_initial_mode_known:
        _stdout_initial_mode = mode
        _stdout_initial_mode_known = True


def _stdout_mode_for_legacy_detection() -> int | None:
    if _stdout_initial_mode_known:
        return _stdout_initial_mode
    return _get_stdout_console_mode()


def _has_modern_windows_terminal_environment() -> bool:
    if os.environ.get("WT_SESSION"):
        return True
    if os.environ.get("TERM_PROGRAM"):
        return True
    if os.environ.get("VSCODE_PID"):
        return True
    if os.environ.get("ANSICON"):
        return True
    return os.environ.get("ConEmuANSI", "").upper() == "ON"


def stdout_supports_virtual_terminal() -> bool:
    """Return whether stdout can safely receive ANSI/VT control sequences."""
    if sys.platform != "win32":
        return True
    mode = _get_stdout_console_mode()
    return mode is not None and bool(mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING)


def is_legacy_windows_console() -> bool:
    """Return True for a real Windows console without VT output enabled."""
    if sys.platform != "win32":
        return False
    if _has_modern_windows_terminal_environment():
        return False
    mode = _stdout_mode_for_legacy_detection()
    return mode is not None and not bool(mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING)


def use_ascii_symbols() -> bool:
    """Return whether UI symbols should prefer ASCII fallbacks."""
    return is_legacy_windows_console()


def enable_virtual_terminal() -> None:
    """Enable ANSI escape sequence support on Windows 10+.

    No-op on non-Windows platforms. Silently ignores failures
    (e.g. Windows < 10 or non-console handles).
    """
    if sys.platform != "win32":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        _configure_console_api(ctypes, kernel32)

        # stdout: ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
        handle_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if handle_out and kernel32.GetConsoleMode(handle_out, ctypes.byref(mode)):
            _remember_stdout_initial_mode(int(mode.value))
            kernel32.SetConsoleMode(handle_out, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except (AttributeError, OSError, ValueError):
        pass
