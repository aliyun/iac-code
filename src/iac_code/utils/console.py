# src/iac_code/utils/console.py
"""Windows console Virtual Terminal Processing enablement."""

from __future__ import annotations

import sys


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

        # Declare types — HANDLE is void* (64-bit on x64), not c_int.
        kernel32.GetStdHandle.argtypes = [ctypes.c_int]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetConsoleMode.restype = ctypes.c_bool
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.SetConsoleMode.restype = ctypes.c_bool

        # stdout: ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
        handle_out = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if handle_out and kernel32.GetConsoleMode(handle_out, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle_out, mode.value | 0x0004)
    except (AttributeError, OSError, ValueError):
        pass
