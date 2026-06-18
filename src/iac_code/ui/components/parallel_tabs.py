"""ParallelTabsRenderer — tab-switching UI for parallel sub-pipeline execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from io import StringIO

from rich.console import Console, Group, RenderableType
from rich.rule import Rule
from rich.text import Text

from iac_code.i18n import _
from iac_code.ui.components.status_icon import Status, status_symbol
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.spinner import ShimmerSpinner, random_spinner_verb

CHROME_LINES = 5


class CandidateStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class CandidateState:
    """Tracks one candidate's execution state."""

    sub_pipeline_id: str
    candidate_index: int
    name: str
    total_steps: int
    current_step: str = ""
    completed_steps: int = 0
    status: CandidateStatus = CandidateStatus.RUNNING
    error: str | None = None

    @property
    def progress_label(self) -> str:
        if self.status == CandidateStatus.DONE:
            icon, _style = status_symbol(Status.SUCCESS)
            return _("{name}: {icon} Done").format(name=self.name, icon=icon)
        if self.status == CandidateStatus.FAILED:
            icon, _style = status_symbol(Status.ERROR)
            return _("{name}: {icon} Failed [{completed}/{total}]").format(
                name=self.name,
                icon=icon,
                completed=self.completed_steps,
                total=self.total_steps,
            )
        if self.current_step:
            return f"{self.name}: {self.current_step} [{self.completed_steps}/{self.total_steps}]"
        return f"{self.name}: [0/{self.total_steps}]"


class ParallelTabsRenderer:
    """Renders parallel sub-pipeline execution with tab switching."""

    def __init__(self, candidates: list[CandidateState], console: Console) -> None:
        self._candidates = candidates
        self._console = console
        self._selected = 0
        self._by_id: dict[str, CandidateState] = {c.sub_pipeline_id: c for c in candidates}
        self._input_line: str | None = None
        self._tab_verbs: dict[str, str] = {}
        self._tab_spinners: dict[str, ShimmerSpinner] = {}
        self._scroll_offsets: list[int] = [0 for _ in candidates]

    def _verb_for(self, name: str) -> str:
        if name not in self._tab_verbs:
            self._tab_verbs[name] = random_spinner_verb()
        return self._tab_verbs[name]

    def _spinner_for(self, name: str) -> ShimmerSpinner:
        if name not in self._tab_spinners:
            self._tab_spinners[name] = ShimmerSpinner(status=self._verb_for(name))
        return self._tab_spinners[name]

    def set_input_line(self, text: str | None) -> None:
        self._input_line = text

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def all_done(self) -> bool:
        return all(c.status in (CandidateStatus.DONE, CandidateStatus.FAILED) for c in self._candidates)

    def get_candidate(self, sub_pipeline_id: str) -> CandidateState:
        return self._by_id[sub_pipeline_id]

    def handle_key(self, key_event: KeyEvent) -> bool:
        k = key_event.key

        if k == "right":
            self._selected = min(self._selected + 1, len(self._candidates) - 1)
            return True
        if k == "left":
            self._selected = max(self._selected - 1, 0)
            return True

        if k == "up":
            if self._candidates:
                self._scroll_offsets[self._selected] = max(self._scroll_offsets[self._selected] - 3, 0)
            return True
        if k == "down":
            if self._candidates:
                self._scroll_offsets[self._selected] += 3
            return True

        # Number keys 1-9
        if len(k) == 1 and k.isdigit() and k != "0":
            idx = int(k) - 1
            if 0 <= idx < len(self._candidates):
                self._selected = idx
                return True
            return False

        return False

    def update_step(self, sub_pipeline_id: str, step_name: str, completed: int) -> None:
        if sub_pipeline_id in self._by_id:
            state = self._by_id[sub_pipeline_id]
            state.current_step = step_name
            state.completed_steps = completed

    def mark_done(self, sub_pipeline_id: str) -> None:
        if sub_pipeline_id in self._by_id:
            state = self._by_id[sub_pipeline_id]
            state.status = CandidateStatus.DONE
            state.completed_steps = state.total_steps

    def mark_failed(self, sub_pipeline_id: str, error: str = "") -> None:
        if sub_pipeline_id in self._by_id:
            state = self._by_id[sub_pipeline_id]
            state.status = CandidateStatus.FAILED
            state.error = error

    def render(self) -> RenderableType:
        tab_bar = self._render_tab_bar()
        rule = Rule(style="dim")
        content = self._apply_viewport(self._render_content())
        hint = self._render_hint()
        parts: list[RenderableType] = [tab_bar, rule, content, Text()]
        if self._input_line is not None:
            parts.append(Text(f"✎ {self._input_line}█", style="bold"))
        parts.append(hint)
        return Group(*parts)

    def render_with_content(self, content: RenderableType) -> RenderableType:
        """Render tab chrome (tab bar + hint) with externally provided content."""
        tab_bar = self._render_tab_bar()
        rule = Rule(style="dim")
        hint = self._render_hint()
        viewport = self._apply_viewport(content)
        parts: list[RenderableType] = [tab_bar, rule, viewport, Text()]
        if self._input_line is not None:
            parts.append(Text(f"✎ {self._input_line}█", style="bold"))
        parts.append(hint)
        return Group(*parts)

    def _apply_viewport(self, content: RenderableType) -> RenderableType:
        viewport_height = max(self._console.height - CHROME_LINES, 4)
        buf = StringIO()
        temp = Console(file=buf, width=self._console.width, force_terminal=True, no_color=False)
        temp.print(content, end="")
        lines = buf.getvalue().splitlines()
        total = len(lines)
        if not self._candidates or total <= viewport_height:
            if self._candidates:
                self._scroll_offsets[self._selected] = 0
            return content

        max_offset = max(total - viewport_height, 0)
        offset = min(self._scroll_offsets[self._selected], max_offset)
        self._scroll_offsets[self._selected] = offset
        visible = lines[offset : offset + viewport_height]

        parts: list[RenderableType] = []
        if offset > 0:
            parts.append(Text("  " + _("Scroll up to view more"), style="dim"))
        parts.append(Text.from_ansi("\n".join(visible)))
        if offset < max_offset:
            parts.append(Text("  " + _("Scroll down to view more"), style="dim"))
        return Group(*parts)

    def _render_tab_bar(self) -> Text:
        text = Text()

        for i, c in enumerate(self._candidates):
            if i > 0:
                text.append(" ", style="dim")

            is_active = i == self._selected

            if c.status == CandidateStatus.FAILED:
                style = "bold red on color(52)" if is_active else "red"
                label = c.progress_label
                suffix = ""
            elif c.status == CandidateStatus.DONE:
                style = "bold green on color(22)" if is_active else "green"
                label = c.progress_label
                suffix = ""
            else:
                style = "bold bright_cyan on color(24)" if is_active else "dim"
                if c.current_step:
                    label = c.progress_label
                else:
                    verb = self._verb_for(c.name)
                    label = f"{c.name}: {verb} [{c.completed_steps}/{c.total_steps}]"
                spinner_char = self._spinner_for(c.name).frame()
                suffix = f" {spinner_char}"

            text.append(f" {label}{suffix} ", style=style)

        return text

    def _render_content(self) -> RenderableType:
        if not self._candidates:
            return Text("")
        c = self._candidates[self._selected]

        if c.status == CandidateStatus.FAILED:
            content = Text()
            content.append(_("━━ {name}: execution failed ━━\n").format(name=c.name), style="bold red")
            content.append("\n")
            if c.error:
                content.append(_("Failure reason: {error}\n").format(error=c.error), style="dim")
            return content

        content = Text()
        step_info = f": {c.current_step}" if c.current_step else ""
        content.append(f"━━ {c.name}{step_info} ━━\n", style="bold cyan")
        if c.status == CandidateStatus.RUNNING:
            content.append(_("Waiting for output...\n"), style="dim")

        return content

    def _render_hint(self) -> Text:
        n = len(self._candidates)
        if n <= 1:
            return Text("")
        hint = Text(_("← → switch candidates | ↑ ↓ scroll"), style="dim")
        if n <= 9:
            hint.append(_(" | 1-{count} jump directly").format(count=n), style="dim")
        return hint
