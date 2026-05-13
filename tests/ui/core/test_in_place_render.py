"""Tests for InPlaceRenderer."""

import io

from rich.console import Console
from rich.text import Text

from iac_code.ui.core.in_place_render import InPlaceRenderer


def make_renderer() -> tuple["InPlaceRenderer", io.StringIO, Console]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=80, height=40)
    return InPlaceRenderer(console), buf, console


class TestInPlaceRenderer:
    def test_first_frame_writes_crlf_separators(self):
        renderer, buf, _ = make_renderer()
        renderer.render(Text("line one\nline two"))
        out = buf.getvalue()
        # \r\n separator means each line returns to col 0 even with OPOST off.
        assert "line one\r\n" in out
        # No first-frame erase: no UP+ERASE sequence.
        assert "\x1b[A\x1b[2K" not in out

    def test_first_frame_height_matches_line_count(self):
        renderer, _, _ = make_renderer()
        renderer.render(Text("a\nb\nc"))
        assert renderer.last_height == 3

    def test_second_frame_erases_previous_height(self):
        renderer, buf, _ = make_renderer()
        renderer.render(Text("a\nb\nc\nd"))
        first_height = renderer.last_height
        buf.seek(0)
        buf.truncate()
        renderer.render(Text("x\ny"))
        out = buf.getvalue()
        assert out.startswith("\r\x1b[2K")
        # First erase covers the cursor's line, then UP+ERASE walks up
        # (first_height - 1) more lines.
        assert out.count("\x1b[A\x1b[2K") == first_height - 1
        assert "x\r\ny" in out

    def test_clear_erases_last_frame(self):
        renderer, buf, _ = make_renderer()
        renderer.render(Text("a\nb\nc"))
        last = renderer.last_height
        buf.seek(0)
        buf.truncate()
        renderer.clear()
        out = buf.getvalue()
        assert out.startswith("\r\x1b[2K")
        assert out.count("\x1b[A\x1b[2K") == last - 1
        assert renderer.last_height == 0

    def test_clear_is_idempotent(self):
        renderer, buf, _ = make_renderer()
        renderer.render(Text("a\nb"))
        renderer.clear()
        buf.seek(0)
        buf.truncate()
        renderer.clear()
        assert buf.getvalue() == ""

    def test_clear_without_prior_render_is_noop(self):
        renderer, buf, _ = make_renderer()
        renderer.clear()
        assert buf.getvalue() == ""
        assert renderer.last_height == 0

    def test_cursor_to_moves_to_first_row(self):
        renderer, buf, _ = make_renderer()
        renderer.render(Text("aaa\nbbb\nccc"), cursor_to=(0, 2))
        out = buf.getvalue()
        # 3-row render → cursor lands on the bottom row by default.
        # Asking for row 0 means walk up 2 rows, then right 2 cols.
        assert "\r\x1b[2A\x1b[2C" in out

    def test_cursor_to_zero_column_omits_horizontal_move(self):
        renderer, buf, _ = make_renderer()
        renderer.render(Text("aaa\nbbb"), cursor_to=(0, 0))
        out = buf.getvalue()
        # Walk up 1 row, no \x1b[*C since col=0 (CR alone is enough).
        assert "\r\x1b[1A" in out
        assert "\x1b[0C" not in out

    def test_cursor_to_clamps_out_of_range_row(self):
        renderer, buf, _ = make_renderer()
        # Asking for row 99 in a 2-line render should clamp to row 1 (last) —
        # i.e. no UP escape because we're already there.
        renderer.render(Text("aaa\nbbb"), cursor_to=(99, 0))
        out = buf.getvalue()
        # The cursor-positioning block emits at least \r and no \x1b[*A.
        # We assert by checking there's no UP after the content was written.
        # Easiest: the trailing reposition is just \r.
        assert out.endswith("bbb\r") or "\x1b[A" not in out.split("bbb", 1)[1]

    def test_after_cursor_to_next_render_walks_cursor_down_first(self):
        # If cursor_to parked the cursor at row 0 of a 3-row frame, the
        # next render must \x1b[2B back to the last row before the
        # CR+ERASE / UP+ERASE walk-up — otherwise erase only clears the
        # top of the frame and the bottom of the old frame leaks into
        # the next render.
        renderer, buf, _ = make_renderer()
        renderer.render(Text("aaa\nbbb\nccc"), cursor_to=(0, 0))
        buf.seek(0)
        buf.truncate()
        renderer.render(Text("xxx\nyyy\nzzz"))
        out = buf.getvalue()
        # 3-row frame, cursor parked at row 0 → walk down 2 before erasing.
        assert out.startswith("\x1b[2B\r\x1b[2K")

    def test_clear_after_cursor_to_walks_down_first(self):
        renderer, buf, _ = make_renderer()
        renderer.render(Text("aaa\nbbb\nccc\nddd"), cursor_to=(1, 0))
        buf.seek(0)
        buf.truncate()
        renderer.clear()
        out = buf.getvalue()
        # 4-row frame, cursor parked at row 1 → walk down 2 (4-1-1) first.
        assert out.startswith("\x1b[2B\r\x1b[2K")

    def test_render_then_clear_then_render_again(self):
        # After a clear, the next render should start fresh — no UP+ERASE.
        renderer, buf, _ = make_renderer()
        renderer.render(Text("a\nb\nc"))
        renderer.clear()
        buf.seek(0)
        buf.truncate()
        renderer.render(Text("hello"))
        out = buf.getvalue()
        assert "\x1b[A\x1b[2K" not in out
        assert "hello" in out
