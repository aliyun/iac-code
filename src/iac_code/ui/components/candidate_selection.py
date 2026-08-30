"""CandidateSelectionRenderer — tab-switching UI for candidate comparison and selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO
from typing import Any

from loguru import logger
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from iac_code.i18n import _
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.diagram_rendering import style_attachment_lines

SCROLL_INDICATOR_LINES = 2


@dataclass
class CandidateDetailEntry:
    """One ``show_candidate_detail`` invocation's payload.

    U-I14: stored per ``tool_use_id`` on the tab so multiple invocations for the
    same ``candidate_name`` don't silently clobber each other in the accumulator.
    """

    summary: str
    cost_items: list[dict]
    total_monthly_cost: str


@dataclass
class CandidateDiagramView:
    """One architecture diagram view inside a candidate tab."""

    view_id: str
    title: str
    mermaid_source: str
    purpose: str = ""


@dataclass(frozen=True)
class CandidateSelection:
    """Structured selection returned by the candidate selection UI."""

    selected_candidate_name: str
    selected_candidate_index: int | None
    selected_evaluated_candidate_index: int | None = None
    display_label: str = ""


@dataclass
class CandidateTab:
    """Data for one candidate tab."""

    candidate_key: str
    candidate_name: str
    candidate_index: int | None = None
    mermaid_source: str | None = None
    diagram_stage: str = "optimized"
    diagram_views: list[CandidateDiagramView] = field(default_factory=list)
    selected_diagram_view_index: int = 0
    summary: str | None = None
    cost_items: list[dict] = field(default_factory=list)
    total_monthly_cost: str = ""
    # U-I14: preserve every detail entry keyed by the originating tool_use_id so
    # duplicate ``show_candidate_detail`` calls for the same candidate don't lose
    # state. The convenience fields above hold the most recent entry for display.
    details_by_tool_use_id: dict[str, CandidateDetailEntry] = field(default_factory=dict)


class CandidateSelectionRenderer:
    """Renders candidate tabs with architecture diagrams and cost details."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._tabs: list[CandidateTab] = []
        self._by_key: dict[str, CandidateTab] = {}
        self._selected = 0
        self._selecting = False
        self._scroll_offset = 0
        self._status_message: str = ""
        self._selection_option_keys: list[str] = []

    def set_status_message(self, msg: str) -> None:
        self._status_message = msg

    @property
    def tab_count(self) -> int:
        return len(self._tabs)

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def is_selecting(self) -> bool:
        return self._selecting

    @staticmethod
    def _candidate_key(candidate_name: str, candidate_index: int | None) -> str:
        if candidate_index is not None:
            return f"index:{candidate_index}"
        return f"name:{candidate_name}"

    @staticmethod
    def _coerce_candidate_index(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return None

    def seed_candidates(self, candidates: list[dict[str, Any]]) -> None:
        """Create placeholder tabs for expected candidates before selection."""
        self._selection_option_keys = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            name = candidate.get("name") or candidate.get("candidate_name")
            if not isinstance(name, str) or not name.strip():
                continue
            candidate_index = self._coerce_candidate_index(candidate.get("candidate_index"))
            normalized_name = name.strip()
            named_tabs = [tab for tab in self._tabs if tab.candidate_name == normalized_name]
            tab = (
                named_tabs[0]
                if candidate_index is None and len(named_tabs) == 1
                else self._get_or_create_tab(normalized_name, candidate_index)
            )
            self._selection_option_keys.append(tab.candidate_key)

    def _merge_tab_data(self, target: CandidateTab, source: CandidateTab) -> None:
        if target.mermaid_source is None and source.mermaid_source is not None:
            target.mermaid_source = source.mermaid_source
        if not target.diagram_views and source.diagram_views:
            target.diagram_views = list(source.diagram_views)
            target.selected_diagram_view_index = min(
                source.selected_diagram_view_index,
                max(len(target.diagram_views) - 1, 0),
            )
            target.diagram_stage = source.diagram_stage
        if target.summary is None and source.summary is not None:
            target.summary = source.summary
        if not target.cost_items and source.cost_items:
            target.cost_items = source.cost_items
        if not target.total_monthly_cost and source.total_monthly_cost:
            target.total_monthly_cost = source.total_monthly_cost
        target.details_by_tool_use_id.update(source.details_by_tool_use_id)

    def _remove_tab(self, tab: CandidateTab) -> None:
        source_index = self._tabs.index(tab)
        source_was_selected = source_index == self._selected
        self._tabs.pop(source_index)
        self._by_key.pop(tab.candidate_key, None)
        if not self._tabs:
            self._selected = 0
        elif source_was_selected:
            self._selected = min(source_index, len(self._tabs) - 1)
        elif source_index < self._selected:
            self._selected -= 1

    def _get_or_create_tab(self, candidate_name: str, candidate_index: int | None = None) -> CandidateTab:
        key = self._candidate_key(candidate_name, candidate_index)
        if key in self._by_key:
            tab = self._by_key[key]
            if candidate_index is not None:
                name_key = self._candidate_key(candidate_name, None)
                name_tab = self._by_key.get(name_key)
                if name_tab is not None and name_tab is not tab:
                    name_tab_was_selected = self._tabs.index(name_tab) == self._selected
                    self._merge_tab_data(tab, name_tab)
                    self._remove_tab(name_tab)
                    if name_tab_was_selected:
                        self._selected = self._tabs.index(tab)
            return tab
        if candidate_index is not None:
            name_key = self._candidate_key(candidate_name, None)
            name_tab = self._by_key.pop(name_key, None)
            if name_tab is not None:
                name_tab.candidate_key = key
                name_tab.candidate_index = candidate_index
                self._by_key[key] = name_tab
                return name_tab
        tab = CandidateTab(candidate_key=key, candidate_name=candidate_name, candidate_index=candidate_index)
        self._tabs.append(tab)
        self._by_key[key] = tab
        return tab

    def add_diagram(
        self,
        candidate_name: str,
        mermaid_source: str,
        candidate_index: int | None = None,
        *,
        views: list[dict[str, Any]] | None = None,
        diagram_stage: str = "optimized",
    ) -> None:
        tab = self._get_or_create_tab(candidate_name, candidate_index)
        previous_view_id = self._active_diagram_view(tab).view_id if tab.diagram_views else ""
        tab.mermaid_source = mermaid_source
        tab.diagram_stage = "draft" if diagram_stage == "draft" else "optimized"
        tab.diagram_views = self._normalize_diagram_views(views, mermaid_source)
        tab.selected_diagram_view_index = self._view_index_by_id(tab.diagram_views, previous_view_id)

    def update_streaming_summary(
        self,
        candidate_name: str,
        partial_summary: str,
        candidate_index: int | None = None,
    ) -> None:
        """Set a partially-streamed summary (replaced by *add_detail* when the tool completes)."""
        tab = self._get_or_create_tab(candidate_name, candidate_index)
        tab.summary = partial_summary

    def add_detail(
        self,
        tool_use_id: str,
        candidate_name: str,
        summary: str,
        cost_items: list[dict],
        total_monthly_cost: str,
        candidate_index: int | None = None,
    ) -> None:
        """Store a candidate detail keyed by ``tool_use_id``.

        U-I14: ``tool_use_id`` is the unique identifier for the storage so two
        ``show_candidate_detail`` calls with the same ``candidate_name`` (but
        different ``tool_use_id``) preserve both payloads instead of clobbering.
        ``candidate_name`` continues to act as the display label / tab grouping.
        """
        tab = self._get_or_create_tab(candidate_name, candidate_index)
        tab.summary = summary
        tab.cost_items = cost_items
        tab.total_monthly_cost = total_monthly_cost
        tab.details_by_tool_use_id[tool_use_id] = CandidateDetailEntry(
            summary=summary,
            cost_items=cost_items,
            total_monthly_cost=total_monthly_cost,
        )

    def handle_key(self, key_event: KeyEvent) -> bool:
        k = key_event.key
        if k == "right":
            new = min(self._selected + 1, len(self._tabs) - 1)
            if new != self._selected:
                self._selected = new
                self._scroll_offset = 0
            return True
        if k == "left":
            new = max(self._selected - 1, 0)
            if new != self._selected:
                self._selected = new
                self._scroll_offset = 0
            return True
        if k == "up":
            self._scroll_offset = max(self._scroll_offset - 3, 0)
            return True
        if k == "down":
            self._scroll_offset += 3
            return True
        if k == "[" or (k == "tab" and key_event.shift):
            return self._select_relative_diagram_view(-1)
        if k in {"]", "tab"}:
            return self._select_relative_diagram_view(1)
        if len(k) == 1 and k.isdigit() and k != "0":
            idx = int(k) - 1
            if 0 <= idx < len(self._tabs):
                self._selected = idx
                self._scroll_offset = 0
                return True
            return False
        return False

    def enter_selection_mode(self) -> None:
        self._selecting = True

    def confirm_selection(self) -> CandidateSelection:
        if self._tabs:
            tab = self._tabs[self._selected]
            try:
                display_index = self._selection_option_keys.index(tab.candidate_key)
            except ValueError:
                display_index = self._selected if not self._selection_option_keys else None
            return CandidateSelection(
                selected_candidate_name=tab.candidate_name,
                selected_candidate_index=display_index,
                selected_evaluated_candidate_index=tab.candidate_index,
                display_label=self._display_label(tab),
            )
        return CandidateSelection(selected_candidate_name="", selected_candidate_index=None)

    def render_selected_static(self) -> RenderableType | None:
        """Return the selected tab's content for static (non-Live) display."""
        if not self._tabs:
            return None
        return self._render_content()

    def render(self) -> RenderableType:
        if not self._tabs:
            return Text(_("Waiting for candidate data..."), style="dim")
        tab_bar = self._render_tab_bar()
        rule = Rule(style="dim")
        content = self._render_content()
        hint = self._render_hint()
        warning = self._render_completeness_warning() if self._selecting else None
        prefix_parts: list[RenderableType] = [tab_bar, rule]
        suffix_parts: list[RenderableType] = [Text()]
        if warning is not None:
            suffix_parts.append(warning)
            suffix_parts.append(Text())
        if self._status_message:
            suffix_parts.append(Text(self._status_message, style="dim italic"))
            suffix_parts.append(Text())
        suffix_parts.append(hint)
        reserved_lines = len(self._render_lines(Group(*prefix_parts, *suffix_parts)))
        viewport = self._apply_viewport(content, reserved_lines=reserved_lines)
        return Group(*prefix_parts, viewport, *suffix_parts)

    def missing_display_items(self) -> list[str]:
        missing: list[str] = []
        for tab in self._tabs:
            parts: list[str] = []
            if not tab.mermaid_source:
                parts.append(_("architecture diagram"))
            if not tab.details_by_tool_use_id:
                parts.append(_("details"))
            if parts:
                missing.append(
                    _("{label} missing {items}").format(label=self._display_label(tab), items="/".join(parts))
                )
        return missing

    def _render_completeness_warning(self) -> Text | None:
        missing = self.missing_display_items()
        if not missing:
            return None
        text = Text(_("Some candidate display data is incomplete: "), style="yellow")
        text.append("；".join(missing), style="yellow")
        return text

    def _apply_viewport(self, content: RenderableType, *, reserved_lines: int) -> RenderableType:
        available_height = max(self._console.height - reserved_lines, 1)
        lines = self._render_lines(content)
        total = len(lines)
        if total <= available_height:
            self._scroll_offset = 0
            return content
        indicator_budget = min(SCROLL_INDICATOR_LINES, max(available_height - 1, 0))
        viewport_height = max(available_height - indicator_budget, 1)
        max_offset = max(total - viewport_height, 0)
        self._scroll_offset = min(self._scroll_offset, max_offset)
        visible = lines[self._scroll_offset : self._scroll_offset + viewport_height]
        show_scroll_up = self._scroll_offset > 0 and indicator_budget > 0
        show_scroll_down = self._scroll_offset < max_offset and indicator_budget > int(show_scroll_up)
        parts: list[RenderableType] = []
        if show_scroll_up:
            parts.append(Text("  " + _("Scroll up to view more"), style="dim"))
        parts.append(Text.from_ansi("\n".join(visible)))
        if show_scroll_down:
            parts.append(Text("  " + _("Scroll down to view more"), style="dim"))
        return Group(*parts)

    def _render_lines(self, renderable: RenderableType) -> list[str]:
        buf = StringIO()
        temp = Console(file=buf, width=self._console.width, force_terminal=True, no_color=False)
        temp.print(renderable, end="")
        return buf.getvalue().splitlines()

    def _render_tab_bar(self) -> Text:
        text = Text()
        for i, tab in enumerate(self._tabs):
            if i > 0:
                text.append(" | ", style="dim")
            label = self._display_label(tab)
            if i == self._selected:
                text.append(f" {label} ", style="bold bright_cyan on color(24)")
            else:
                text.append(f" {label} ", style="dim")
        return text

    def _display_label(self, tab: CandidateTab) -> str:
        if sum(1 for item in self._tabs if item.candidate_name == tab.candidate_name) <= 1:
            return tab.candidate_name
        if tab.candidate_index is not None:
            return f"{tab.candidate_name} #{tab.candidate_index + 1}"
        return f"{tab.candidate_name} #{self._tabs.index(tab) + 1}"

    def _render_content(self) -> RenderableType:
        tab = self._tabs[self._selected]
        parts: list[RenderableType] = []

        if tab.diagram_views:
            if len(tab.diagram_views) > 1:
                parts.append(self._render_diagram_view_tab_bar(tab))
            active_view = self._active_diagram_view(tab)
            parts.append(self._render_diagram(active_view.mermaid_source))
            if tab.diagram_stage == "draft":
                parts.append(Text(_("Optimizing architecture diagram..."), style="dim italic"))
        elif tab.mermaid_source:
            parts.append(self._render_diagram(tab.mermaid_source))
            if tab.diagram_stage == "draft":
                parts.append(Text(_("Optimizing architecture diagram..."), style="dim italic"))
        else:
            parts.append(Text(_("Loading architecture diagram..."), style="dim italic"))

        parts.append(Rule(style="dim"))

        if tab.summary:
            parts.append(Text(tab.summary))
        else:
            parts.append(Text(_("Loading candidate details..."), style="dim italic"))

        if tab.cost_items:
            parts.append(Text())
            parts.append(self._render_cost_table(tab.cost_items, tab.total_monthly_cost))

        return Group(*parts)

    @staticmethod
    def _normalize_diagram_views(
        views: list[dict[str, Any]] | None,
        fallback_mermaid_source: str,
    ) -> list[CandidateDiagramView]:
        normalized: list[CandidateDiagramView] = []
        if views:
            for index, raw in enumerate(views, start=1):
                if not isinstance(raw, dict):
                    continue
                mermaid_source = raw.get("mermaid_source") or raw.get("mermaidSource")
                if not isinstance(mermaid_source, str) or not mermaid_source.strip():
                    continue
                view_id = raw.get("id")
                title = raw.get("title")
                purpose = raw.get("purpose")
                normalized.append(
                    CandidateDiagramView(
                        view_id=str(view_id or f"view_{index}"),
                        title=str(title or view_id or _("Diagram {index}").format(index=index)),
                        mermaid_source=mermaid_source,
                        purpose=str(purpose or ""),
                    )
                )
        if not normalized:
            normalized.append(
                CandidateDiagramView(
                    view_id="overview",
                    title=_("Architecture overview"),
                    mermaid_source=fallback_mermaid_source,
                )
            )
        return normalized

    @staticmethod
    def _view_index_by_id(views: list[CandidateDiagramView], view_id: str) -> int:
        if view_id:
            for index, view in enumerate(views):
                if view.view_id == view_id:
                    return index
        return 0

    def _select_relative_diagram_view(self, delta: int) -> bool:
        if not self._tabs:
            return False
        tab = self._tabs[self._selected]
        if len(tab.diagram_views) <= 1:
            return False
        tab.selected_diagram_view_index = (tab.selected_diagram_view_index + delta) % len(tab.diagram_views)
        self._scroll_offset = 0
        return True

    @staticmethod
    def _active_diagram_view(tab: CandidateTab) -> CandidateDiagramView:
        tab.selected_diagram_view_index = min(
            max(tab.selected_diagram_view_index, 0),
            max(len(tab.diagram_views) - 1, 0),
        )
        return tab.diagram_views[tab.selected_diagram_view_index]

    def _render_diagram_view_tab_bar(self, tab: CandidateTab) -> Text:
        text = Text()
        for index, view in enumerate(tab.diagram_views):
            if index > 0:
                text.append(" / ", style="dim")
            label = view.title
            if index == tab.selected_diagram_view_index:
                text.append(f" {label} ", style="bold cyan")
            else:
                text.append(f" {label} ", style="dim")
        return text

    def _render_diagram(self, mermaid_source: str) -> RenderableType:
        try:
            from importlib import import_module

            render_rich = import_module("termaid").render_rich
            return style_attachment_lines(render_rich(mermaid_source))
        except ImportError:
            pass  # Silent degrade — termaid optional dependency missing.
        except Exception as exc:
            logger.warning("termaid render failed: {}", exc)
        return Markdown(f"```mermaid\n{mermaid_source}\n```")

    @staticmethod
    def _render_cost_table(cost_items: list[dict], total: str) -> RenderableType:
        table = Table(title=_("Cost details"), show_header=True, border_style="dim")
        table.add_column(_("Product"), style="cyan")
        table.add_column(_("Specification"), style="dim")
        table.add_column(_("Monthly cost"), justify="right", style="green")
        for item in cost_items:
            table.add_row(
                item.get("name", ""),
                item.get("spec", ""),
                item.get("monthly_cost", ""),
            )
        table.add_section()
        table.add_row("", _("Total"), total, style="bold")
        return table

    def _render_hint(self) -> Text:
        if self._selecting:
            hint = Text(
                _("Press number keys to select a candidate, Enter to confirm | ← → switch candidates | ↑ ↓ scroll"),
                style="dim",
            )
        elif len(self._tabs) > 1:
            hint = Text(_("← → switch candidates | ↑ ↓ scroll"), style="dim")
            if len(self._tabs) <= 9:
                hint.append(_(" | 1-{count} jump directly").format(count=len(self._tabs)), style="dim")
        else:
            hint = Text(_("↑ ↓ scroll"), style="dim")
        if self._tabs and len(self._tabs[self._selected].diagram_views) > 1:
            hint.append(" | Tab/[ ] " + _("switch diagrams"), style="dim")
        return hint
