"""Small Vim-like editor for instruction memory files."""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.text import Text

from iac_code.i18n import _
from iac_code.ui.core.key_event import KeyEvent


@dataclass(frozen=True)
class MemoryEditResult:
    status: str
    content: str


class FullscreenRenderer:
    """Alternate-screen renderer that preserves and displays the hardware cursor."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def __enter__(self) -> "FullscreenRenderer":
        out = self._console.file
        out.write("\x1b[?1049h\x1b[?25h\x1b[2J\x1b[H")
        out.flush()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        out = self._console.file
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()

    def render(self, renderable: RenderableType, cursor_to: tuple[int, int] | None = None) -> None:
        with self._console.capture() as capture:
            self._console.print(renderable)
        captured = capture.get().replace("\r\n", "\n")
        if captured.endswith("\n"):
            captured = captured[:-1]
        text = captured.replace("\n", "\r\n")
        out = self._console.file
        out.write("\x1b[H\x1b[2J")
        out.write(text)
        if cursor_to is not None:
            row, col = cursor_to
            out.write(f"\x1b[{row + 1};{col + 1}H")
        out.flush()


class VimMemoryEditor:
    """A focused Vim-like multiline editor for memory files.

    This intentionally implements a compact subset: normal/insert/command
    modes, movement, insertion, x, dd, :wq, and :q!.
    """

    def __init__(self, initial_text: str, *, title: str, path: str | None = None) -> None:
        self._original_text = initial_text
        self.title = title
        self.path = path or ""
        self.lines = initial_text.split("\n") if initial_text else [""]
        self.row = 0
        self.col = 0
        self.mode = "normal"
        self.command = ""
        self._pending_d = False
        self.done = False
        self.result: MemoryEditResult | None = None

    def run(self, *, renderer=None, input_capture=None) -> MemoryEditResult:
        from iac_code.ui.core.raw_input import RawInputCapture

        renderer_context = renderer or FullscreenRenderer(Console())
        input_context = input_capture or RawInputCapture()
        try:
            with renderer_context as active_renderer, input_context as cap:
                dirty = True
                while not self.done:
                    if dirty:
                        active_renderer.render(self.render(), cursor_to=self.cursor_position())
                        dirty = False
                    key_event = cap.read_key(timeout=0.1)
                    if key_event is not None:
                        dirty = self.handle_key(key_event) or dirty
        except OSError:
            return MemoryEditResult("cancelled", self._original_text)
        return self.result or MemoryEditResult("cancelled", self._original_text)

    def render(self) -> RenderableType:
        return Group(*self._render_rows())

    def render_lines(self) -> list[str]:
        return [row.plain for row in self._render_rows()]

    def _render_rows(self) -> list[Text]:
        width = self._terminal_width()
        visible_from = self._visible_from()
        body = self.lines[visible_from : visible_from + self._visible_height()]
        rows = [Text(self._top_line(width), style="bold white on #202b36")]
        for row_index in range(self._visible_height()):
            source_index = visible_from + row_index
            if row_index < len(body):
                rows.append(self._body_line(source_index + 1, body[row_index], width))
            else:
                rows.append(self._body_line(None, "", width))
        rows.append(self._bottom_line(width))
        return rows

    def handle_key(self, key_event: KeyEvent) -> bool:
        if self.done:
            return True
        if self.mode == "insert":
            return self._handle_insert(key_event)
        if self.mode == "command":
            return self._handle_command(key_event)
        return self._handle_normal(key_event)

    def content(self) -> str:
        return "\n".join(self.lines)

    def cursor_position(self) -> tuple[int, int]:
        visible_from = self._visible_from()
        if self.mode == "command":
            return 1 + self._visible_height(), 2 + cell_len(self.command)
        visual_col = cell_len(self.lines[self.row][: self.col])
        return 1 + max(0, self.row - visible_from), self._content_column() + visual_col

    def _handle_insert(self, key_event: KeyEvent) -> bool:
        key = key_event.key
        if key == "escape":
            self.mode = "normal"
            self._clamp_cursor()
            return True
        if key == "enter":
            current = self.lines[self.row]
            self.lines[self.row] = current[: self.col]
            self.lines.insert(self.row + 1, current[self.col :])
            self.row += 1
            self.col = 0
            return True
        if key == "backspace":
            self._backspace()
            return True
        if key == "delete":
            self._delete_char()
            return True
        if key in {"left", "right", "up", "down"}:
            self._move(key)
            return True
        if key_event.char and not key_event.ctrl:
            self._insert_text(key_event.char)
            return True
        return False

    def _handle_command(self, key_event: KeyEvent) -> bool:
        key = key_event.key
        if key == "escape":
            self.mode = "normal"
            self.command = ""
            return True
        if key == "backspace":
            self.command = self.command[:-1]
            return True
        if key == "enter":
            command = self.command.strip()
            if command == "wq":
                content = self.content()
                status = "unchanged" if content == self._original_text else "saved"
                self.result = MemoryEditResult(status, content)
                self.done = True
            elif command == "q!":
                self.result = MemoryEditResult("cancelled", self._original_text)
                self.done = True
            else:
                self.command = ""
                self.mode = "normal"
            return True
        if key_event.char and not key_event.ctrl:
            self.command += key_event.char
            return True
        return False

    def _handle_normal(self, key_event: KeyEvent) -> bool:
        key = key_event.key
        if key in {"left", "right", "up", "down"}:
            self._pending_d = False
            self._move(key)
            return True
        if key in {"h", "j", "k", "l"}:
            self._pending_d = False
            self._move({"h": "left", "j": "down", "k": "up", "l": "right"}[key])
            return True
        if key == "i":
            self._pending_d = False
            self.mode = "insert"
            return True
        if key == "a":
            self._pending_d = False
            self.col = min(len(self.lines[self.row]), self.col + 1)
            self.mode = "insert"
            return True
        if key == "o":
            self._pending_d = False
            self.lines.insert(self.row + 1, "")
            self.row += 1
            self.col = 0
            self.mode = "insert"
            return True
        if key == "x":
            self._pending_d = False
            self._delete_char()
            return True
        if key == "d":
            if self._pending_d:
                self._delete_line()
                self._pending_d = False
            else:
                self._pending_d = True
            return True
        if key == ":":
            self._pending_d = False
            self.command = ""
            self.mode = "command"
            return True
        self._pending_d = False
        return False

    def _insert_text(self, text: str) -> None:
        line = self.lines[self.row]
        self.lines[self.row] = line[: self.col] + text + line[self.col :]
        self.col += len(text)

    def _backspace(self) -> None:
        if self.col > 0:
            line = self.lines[self.row]
            self.lines[self.row] = line[: self.col - 1] + line[self.col :]
            self.col -= 1
            return
        if self.row > 0:
            previous_len = len(self.lines[self.row - 1])
            self.lines[self.row - 1] += self.lines.pop(self.row)
            self.row -= 1
            self.col = previous_len

    def _delete_char(self) -> None:
        line = self.lines[self.row]
        if self.col < len(line):
            self.lines[self.row] = line[: self.col] + line[self.col + 1 :]

    def _delete_line(self) -> None:
        if len(self.lines) == 1:
            self.lines[0] = ""
            self.row = 0
            self.col = 0
            return
        self.lines.pop(self.row)
        self.row = min(self.row, len(self.lines) - 1)
        self._clamp_cursor()

    def _move(self, direction: str) -> None:
        if direction == "left":
            self.col = max(0, self.col - 1)
        elif direction == "right":
            self.col = min(len(self.lines[self.row]), self.col + 1)
        elif direction == "up":
            self.row = max(0, self.row - 1)
            self._clamp_cursor()
        elif direction == "down":
            self.row = min(len(self.lines) - 1, self.row + 1)
            self._clamp_cursor()

    def _clamp_cursor(self) -> None:
        self.col = min(self.col, len(self.lines[self.row]))

    def _visible_height(self) -> int:
        return max(1, shutil.get_terminal_size((80, 24)).lines - 2)

    def _visible_from(self) -> int:
        height = self._visible_height()
        if self.row < height:
            return 0
        return min(self.row, max(0, len(self.lines) - height))

    def _status_label(self) -> str:
        if self.mode == "insert":
            return _("INSERT")
        if self.mode == "command":
            return ":" + self.command
        return _("NORMAL")

    def _top_line(self, width: int) -> str:
        left = " " + self.title
        if not self.path:
            return _fit_cells(left, width)
        right = self.path
        gap = max(1, width - cell_len(left) - cell_len(right))
        return _fit_cells(left + (" " * gap) + right, width)

    def _body_line(self, line_number: int | None, content: str, width: int) -> Text:
        gutter = self._line_number_width()
        prefix = "{line_number:>{gutter}} │ ".format(
            line_number="" if line_number is None else line_number,
            gutter=gutter,
        )
        prefix_width = min(width, cell_len(prefix))
        body_width = max(0, width - prefix_width)
        return Text.assemble(
            (_fit_cells(prefix, prefix_width), "bold #667786 on #141b22"),
            (_fit_cells(content, body_width), "#dce8ef on #17202a"),
        )

    def _bottom_line(self, width: int) -> Text:
        status = self._status_label()
        hint = _("{status}   :wq save · :q! discard").format(status=status)
        return Text(_fit_cells(" " + hint, width), style="#b8c8d5 on #10161c")

    def _content_column(self) -> int:
        return self._line_number_width() + cell_len(" │ ")

    def _line_number_width(self) -> int:
        return max(2, len(str(len(self.lines))))

    def _terminal_width(self) -> int:
        return max(1, shutil.get_terminal_size((80, 24)).columns - 1)


def _fit_cells(text: str, width: int) -> str:
    used = 0
    output = []
    for char in text:
        char_width = cell_len(char)
        if used + char_width > width:
            break
        output.append(char)
        used += char_width
    return "".join(output) + (" " * max(0, width - used))
