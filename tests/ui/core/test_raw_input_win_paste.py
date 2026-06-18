"""W-I3: Windows raw input bracketed paste mode + paste sequence parsing."""

from __future__ import annotations

import io
import sys
import types

import pytest


@pytest.fixture
def fake_msvcrt(monkeypatch):
    queue: list[str] = []

    class FakeMsvcrt:
        def kbhit(self):
            return len(queue) > 0

        def getwch(self):
            return queue.pop(0)

    fake_module = types.SimpleNamespace(kbhit=FakeMsvcrt().kbhit, getwch=FakeMsvcrt().getwch)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_module)
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.time.monotonic", lambda: 0.0)
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.time.sleep", lambda d: None)
    return queue


def test_enter_writes_bracketed_paste_enable_when_vt_safe(monkeypatch):
    """W-I3: __enter__ must write ESC [ ? 2004 h."""
    buf = io.StringIO()
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.sys.stdout", buf)
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.stdout_supports_virtual_terminal", lambda: True)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    with RawInputCapture() as _cap:
        out = buf.getvalue()
        assert "\x1b[?2004h" in out, f"expected bracketed paste enable, got: {out!r}"


def test_enter_skips_bracketed_paste_enable_when_vt_not_safe(monkeypatch):
    """Legacy Windows consoles must not see bracketed paste control bytes."""
    buf = io.StringIO()
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.sys.stdout", buf)
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.stdout_supports_virtual_terminal", lambda: False)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    with RawInputCapture() as _cap:
        assert buf.getvalue() == ""


def test_exit_writes_bracketed_paste_disable_only_if_enabled(monkeypatch):
    """W-I3: __exit__ must write ESC [ ? 2004 l."""
    buf = io.StringIO()
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.sys.stdout", buf)
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.stdout_supports_virtual_terminal", lambda: True)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    cap.__enter__()
    buf.truncate(0)
    buf.seek(0)
    cap.__exit__(None, None, None)
    out = buf.getvalue()
    assert "\x1b[?2004l" in out, f"expected bracketed paste disable, got: {out!r}"


def test_exit_skips_bracketed_paste_disable_when_not_enabled(monkeypatch):
    """A skipped enable must not be paired with a raw disable sequence."""
    buf = io.StringIO()
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.sys.stdout", buf)
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.stdout_supports_virtual_terminal", lambda: False)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    cap.__enter__()
    cap.__exit__(None, None, None)
    assert buf.getvalue() == ""


def test_exit_clears_bracketed_paste_enabled_after_disable(monkeypatch):
    """Repeated exit calls must not emit duplicate disable sequences."""
    buf = io.StringIO()
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.sys.stdout", buf)
    monkeypatch.setattr("iac_code.ui.core.raw_input_win.stdout_supports_virtual_terminal", lambda: True)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    cap.__enter__()
    buf.truncate(0)
    buf.seek(0)

    cap.__exit__(None, None, None)
    cap.__exit__(None, None, None)

    assert buf.getvalue() == "\x1b[?2004l"


def test_paste_sequence_yields_single_paste_event(fake_msvcrt):
    """W-I3: ESC [ 2 0 0 ~ <body> ESC [ 2 0 1 ~ must produce one paste event."""
    chars = ["\x1b", "[", "2", "0", "0", "~", "你", "好", "世", "界", "\x1b", "[", "2", "0", "1", "~"]
    fake_msvcrt.extend(chars)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    ev = cap.read_key()
    assert ev.key == "paste", f"expected paste event, got {ev.key!r}"
    assert ev.char == "你好世界", f"expected paste body 你好世界, got {ev.char!r}"


def test_paste_body_with_ascii(fake_msvcrt):
    """Ascii paste body should also round-trip correctly."""
    chars = ["\x1b", "[", "2", "0", "0", "~", "h", "e", "l", "l", "o", "\x1b", "[", "2", "0", "1", "~"]
    fake_msvcrt.extend(chars)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    ev = cap.read_key()
    assert ev.key == "paste"
    assert ev.char == "hello"


def test_paste_normalizes_crlf(fake_msvcrt):
    chars = [
        "\x1b",
        "[",
        "2",
        "0",
        "0",
        "~",
        "a",
        "\r",
        "\n",
        "b",
        "\x1b",
        "[",
        "2",
        "0",
        "1",
        "~",
    ]
    fake_msvcrt.extend(chars)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    ev = RawInputCapture().read_key()

    assert ev.key == "paste"
    assert ev.char == "a\nb"


def test_paste_normalizes_bare_cr(fake_msvcrt):
    chars = ["\x1b", "[", "2", "0", "0", "~", "a", "\r", "b", "\x1b", "[", "2", "0", "1", "~"]
    fake_msvcrt.extend(chars)
    from iac_code.ui.core.raw_input_win import RawInputCapture

    ev = RawInputCapture().read_key()

    assert ev.key == "paste"
    assert ev.char == "a\nb"
