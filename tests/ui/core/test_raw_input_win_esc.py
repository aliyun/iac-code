"""W-I2: Windows ESC race — timeout 100ms + SS3 sequence handling."""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def fake_msvcrt(monkeypatch):
    """Inject a fake msvcrt + time control. Each character optionally has a
    delay before it appears in the kbhit() / getwch() queue."""
    queue: list[str] = []
    delays: dict[int, float] = {}  # position-based delay
    elapsed = [0.0]

    class FakeMsvcrt:
        def kbhit(self):
            if not queue:
                return False
            # Position-0 char appears only after its scheduled delay.
            return elapsed[0] >= delays.get(0, 0.0)

        def getwch(self):
            ch = queue.pop(0)
            # Shift all delay keys down by one.
            new_delays = {k - 1: v for k, v in delays.items() if k - 1 >= 0}
            delays.clear()
            delays.update(new_delays)
            return ch

    fake = FakeMsvcrt()
    fake_module = types.SimpleNamespace(kbhit=fake.kbhit, getwch=fake.getwch)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_module)
    monkeypatch.setattr(
        "iac_code.ui.core.raw_input_win.time.monotonic",
        lambda: elapsed[0],
    )
    monkeypatch.setattr(
        "iac_code.ui.core.raw_input_win.time.sleep",
        lambda d: elapsed.__setitem__(0, elapsed[0] + d),
    )
    return queue, delays, elapsed


def test_esc_then_bracket_a_recognized_as_up_arrow(fake_msvcrt):
    """Baseline: ESC [ A within timeout → 'up' KeyEvent."""
    queue, _delays, _elapsed = fake_msvcrt
    queue.extend(["\x1b", "[", "A"])
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    ev = cap.read_key()
    assert ev.key == "up"


def test_esc_then_o_a_recognized_as_app_mode_up_arrow(fake_msvcrt):
    """W-I2: SS3 sequence (ESC O X) should map to arrow keys.

    Windows Terminal application keypad mode and some RDP combinations
    send arrows as ESC O A/B/C/D instead of CSI ESC [ A/B/C/D.
    Pre-fix: ESC O A → KeyEvent(key='escape') + 'O' pending → confused.
    """
    queue, _delays, _elapsed = fake_msvcrt
    queue.extend(["\x1b", "O", "A"])
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    ev = cap.read_key()
    assert ev.key == "up", f"SS3 ESC O A should map to 'up', got {ev.key!r}"


def test_lone_esc_returns_escape_after_timeout(fake_msvcrt):
    """Plain ESC with no follow-up → 'escape' KeyEvent after waiting."""
    queue, _delays, _elapsed = fake_msvcrt
    queue.extend(["\x1b"])
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    ev = cap.read_key()
    assert ev.key == "escape"


def test_esc_with_slow_followup_still_recognized(fake_msvcrt):
    """W-I2: follow-up byte delayed >50ms but <100ms must still be merged.

    Pre-fix: 50ms timeout would expire and ESC would be returned alone.
    Post-fix: 100ms timeout catches the delayed byte.
    """
    queue, delays, _elapsed = fake_msvcrt
    queue.extend(["\x1b", "[", "A"])
    # '[' appears only after 80ms — beyond old 50ms cutoff, within new 100ms.
    delays[1] = 0.08
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    ev = cap.read_key()
    assert ev.key == "up", (
        "slow-arriving [ (80ms after ESC) should still merge into arrow keypress; "
        "if it returns 'escape', the timeout is still too tight"
    )


def test_esc_o_with_unknown_letter_returns_unknown(fake_msvcrt):
    """SS3 with an unmapped letter → 'unknown' (or 'escape') KeyEvent, not a crash."""
    queue, _delays, _elapsed = fake_msvcrt
    queue.extend(["\x1b", "O", "Z"])
    from iac_code.ui.core.raw_input_win import RawInputCapture

    cap = RawInputCapture()
    ev = cap.read_key()
    # 'Z' is not in the SS3 map; expected unknown or escape but not a stack trace.
    assert ev.key in ("unknown", "escape", "Z"), f"unexpected key for unknown SS3 letter: {ev.key!r}"
