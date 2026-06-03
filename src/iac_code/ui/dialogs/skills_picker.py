"""Interactive picker for managing skills."""

from __future__ import annotations

from math import ceil
from typing import Literal

from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.text import Text

from iac_code.i18n import _
from iac_code.skills.management import SkillManagementItem
from iac_code.skills.settings import normalize_skill_name
from iac_code.types.skill_source import SkillSource
from iac_code.ui.components.fuzzy_picker import fuzzy_match
from iac_code.ui.components.search_box import SearchBox
from iac_code.ui.core.key_event import KeyEvent

SortMode = Literal["name", "source", "size"]
_SORT_MODES: tuple[SortMode, ...] = ("name", "source", "size")
_SOURCE_ORDER = {
    SkillSource.BUNDLED: 0,
    SkillSource.PROJECT: 1,
    SkillSource.USER: 2,
}


class SkillsPicker:
    """Interactive skill enable/disable picker."""

    def __init__(
        self,
        items: list[SkillManagementItem],
        keybinding_manager: object | None = None,
        visible_count: int = 10,
    ) -> None:
        self._all_items = list(items)
        self._km = keybinding_manager
        self._visible_count = visible_count
        self._sort_mode: SortMode = "name"
        self._disabled: set[str] = {
            normalize_skill_name(item.name) for item in items if not item.enabled and not item.locked
        }
        self._filtered: list[SkillManagementItem] = []
        self._focused_index = 0
        self._visible_from = 0
        self._done = False
        self._result: set[str] | None = None
        self._status_message = ""
        self._description_matched_names: set[str] = set()
        self._search_box = SearchBox(placeholder=_("Search skills..."), on_change=self._on_query_change)
        self._apply_filter()

    @property
    def disabled_skill_names(self) -> set[str]:
        return set(self._disabled)

    @property
    def filtered_items(self) -> list[SkillManagementItem]:
        return list(self._filtered)

    @property
    def result(self) -> set[str] | None:
        return None if self._result is None else set(self._result)

    @property
    def done(self) -> bool:
        return self._done

    @property
    def status_message(self) -> str:
        return self._status_message

    @property
    def sort_mode(self) -> SortMode:
        return self._sort_mode

    def run(self) -> set[str] | None:
        """Run the blocking terminal picker."""
        from iac_code.ui.core.in_place_render import InPlaceRenderer
        from iac_code.ui.core.raw_input import RawInputCapture

        console = Console()
        renderer = InPlaceRenderer(console)
        self._done = False
        self._result = None

        def cursor_pos() -> tuple[int, int]:
            sb = self._search_box
            col = 2 if not sb.value else 2 + cell_len(sb.value[: sb.cursor])
            return (3, col)

        try:
            with RawInputCapture() as cap:
                while not self._done:
                    renderer.render(self.render(), cursor_to=cursor_pos())
                    key_event = cap.read_key(timeout=0.1)
                    if key_event is not None:
                        self.handle_key(key_event)
        except OSError:
            return None
        finally:
            renderer.clear()

        return self.result

    def handle_key(self, key_event: KeyEvent) -> bool:
        key = key_event.key
        ctrl = key_event.ctrl

        if ctrl and key == "c":
            self._done = True
            self._result = None
            return True

        if key == "escape":
            self._done = True
            self._result = None
            return True

        if key == "enter":
            self._done = True
            self._result = set(self._disabled)
            return True

        if key == "up" or (ctrl and key == "p"):
            self._move_focus(-1)
            return True
        if key == "down" or (ctrl and key == "n"):
            self._move_focus(1)
            return True
        if key == "pageup":
            self._move_focus(-self._visible_count)
            return True
        if key == "pagedown":
            self._move_focus(self._visible_count)
            return True

        if key == " ":
            self._toggle_focused()
            return True

        if key == "tab":
            self._cycle_sort()
            return True

        consumed = self._search_box.handle_key(key_event)
        if consumed:
            self._status_message = ""
        return consumed

    def render(self) -> RenderableType:
        parts: list[RenderableType] = []
        total = len(self._filtered)
        focus_pos = (self._focused_index + 1) if total else 0

        header = Text()
        header.append(_("Skills"), style="bold cyan")
        if total:
            header.append(" (" + _("{current} of {total}").format(current=focus_pos, total=total) + ")", style="dim")
        parts.append(header)
        parts.append(
            Text(
                _("{count} skills - Space to toggle, Enter to save, Tab to sort, Esc to cancel").format(
                    count=len(self._all_items)
                ),
                style="dim",
            )
        )
        parts.append(Text(_("Sort: {mode}").format(mode=_sort_mode_label(self._sort_mode)), style="dim"))
        parts.append(self._search_box.render())
        parts.append(Text(""))

        if not self._filtered:
            parts.append(Text(_("No skills found"), style="dim"))
        else:
            for item in self._filtered[self._visible_from : self._visible_from + self._visible_count]:
                parts.append(self._render_item(item, item == self._filtered[self._focused_index]))

        parts.append(Text(""))
        if self._status_message:
            parts.append(Text(self._status_message, style="yellow"))
        return Group(*parts)

    def _on_query_change(self, _query: str) -> None:
        self._apply_filter()

    def _apply_filter(self, keep_focus_name: str | None = None) -> None:
        query = self._search_box.value.strip()
        candidates = list(self._all_items)
        self._description_matched_names = set()
        if query:
            scored: list[tuple[float, SkillManagementItem]] = []
            for item in candidates:
                haystack = f"{item.name} {item.description}"
                score = fuzzy_match(query, haystack)
                if score is not None:
                    scored.append((score, item))
                    if fuzzy_match(query, item.name) is None:
                        self._description_matched_names.add(item.name)
            scored.sort(key=lambda pair: pair[0], reverse=True)
            candidates = [item for _, item in scored]

        self._filtered = self._sort_items(candidates)
        if keep_focus_name is not None:
            for index, item in enumerate(self._filtered):
                if item.name == keep_focus_name:
                    self._focused_index = index
                    break
            else:
                self._focused_index = 0
        else:
            self._focused_index = 0
        self._visible_from = 0

    def _sort_items(self, items: list[SkillManagementItem]) -> list[SkillManagementItem]:
        if self._sort_mode == "source":
            return sorted(items, key=lambda item: (_SOURCE_ORDER.get(item.source, 99), item.name))
        if self._sort_mode == "size":
            return sorted(items, key=lambda item: (item.content_length, item.name))
        return sorted(items, key=lambda item: item.name)

    def _cycle_sort(self) -> None:
        current = _SORT_MODES.index(self._sort_mode)
        self._sort_mode = _SORT_MODES[(current + 1) % len(_SORT_MODES)]
        focused = self._filtered[self._focused_index].name if self._filtered else None
        self._apply_filter(keep_focus_name=focused)

    def _move_focus(self, delta: int) -> None:
        if not self._filtered:
            return
        self._focused_index = max(0, min(self._focused_index + delta, len(self._filtered) - 1))
        if self._focused_index < self._visible_from:
            self._visible_from = self._focused_index
        elif self._focused_index >= self._visible_from + self._visible_count:
            self._visible_from = self._focused_index - self._visible_count + 1

    def _toggle_focused(self) -> None:
        if not self._filtered:
            return
        item = self._filtered[self._focused_index]
        name = normalize_skill_name(item.name)
        if item.locked:
            self._status_message = _("Bundled skills cannot be disabled.")
            return
        if name in self._disabled:
            self._disabled.remove(name)
        else:
            self._disabled.add(name)
        self._status_message = ""
        self._apply_filter(keep_focus_name=item.name)

    def _render_item(self, item: SkillManagementItem, is_focused: bool) -> Text:
        text = Text()
        text.append("> " if is_focused else "  ", style="bold cyan" if is_focused else "")
        enabled = item.locked or normalize_skill_name(item.name) not in self._disabled
        state_marker = "- " if enabled else "x "
        state_label = _("on") if enabled else _("off")
        text.append("{}{} ".format(state_marker, state_label), style="green" if enabled else "red")
        text.append(f" {item.name:<18}", style="bold" if is_focused else "")

        details = [_source_label(item.source)]
        if item.locked:
            details.append(_("locked"))
        details.append(_format_token_estimate(item.content_length))
        if item.name in self._description_matched_names:
            details.append(_("matched description"))
        if item.source != SkillSource.BUNDLED and item.path:
            details.append(item.path)
        text.append(" - " + " - ".join(details), style="dim")
        return text


def _sort_mode_label(mode: SortMode) -> str:
    if mode == "source":
        return _("source")
    if mode == "size":
        return _("size")
    return _("name")


def _source_label(source: SkillSource) -> str:
    if source == SkillSource.BUNDLED:
        return _("bundled")
    if source == SkillSource.PROJECT:
        return _("project")
    if source == SkillSource.USER:
        return _("user")
    return source.value


def _format_token_estimate(content_length: int) -> str:
    tokens = max(1, ceil(content_length / 4))
    if tokens >= 1000:
        return _("~{count}k tokens").format(count=f"{tokens / 1000:.1f}")
    return _("~{count} tokens").format(count=tokens)
