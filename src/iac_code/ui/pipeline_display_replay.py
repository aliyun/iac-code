"""Static renderer for semantic pipeline display replay."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

from rich.console import Console, RenderableType
from rich.markdown import Markdown
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from iac_code.agent.message import ContentBlock, Message, ToolResultBlock, ToolUseBlock
from iac_code.i18n import _
from iac_code.pipeline.display_names import display_pipeline_name, display_step_name, display_tool_name
from iac_code.pipeline.engine.display_replay import (
    DisplayAttempt,
    DisplayCandidate,
    DisplayCandidateSelection,
    DisplayReplayModel,
    DisplaySubPipeline,
    DisplaySubStepAttempt,
)
from iac_code.ui.pipeline_styles import pipeline_step_header, pipeline_title


class PipelineDisplayReplayRenderer:
    """Render historical pipeline display state without Rich Live."""

    def __init__(
        self,
        console: Console,
        *,
        history_replayer: Callable[[list[Message]], None] | None = None,
        history_renderable_factory: Callable[[list[Message]], RenderableType | None] | None = None,
        transcript_loader: Callable[[str], list[Message]] | None = None,
    ) -> None:
        self.console = console
        self._history_replayer = history_replayer
        self._history_renderable_factory = history_renderable_factory
        self._transcript_loader = transcript_loader

    def render(self, model: DisplayReplayModel) -> None:
        if not model.attempts:
            return
        self.console.print()
        title = _("AI {name} Pipeline").format(name=display_pipeline_name(model.pipeline_name))
        self.console.print(pipeline_title(title))
        self.console.print()
        for attempt in model.attempts:
            self._render_attempt(attempt)
        if model.interrupted and not any(attempt.status == "interrupted" for attempt in model.attempts):
            self.console.print(Text("  " + _("Interrupted"), style="yellow"))
        if model.completed:
            self._render_pipeline_completed(model)

    def _render_attempt(self, attempt: DisplayAttempt) -> None:
        label = f"● {display_step_name(attempt.step_id)}"
        if attempt.index is not None and attempt.total is not None:
            label += f" ({attempt.index}/{attempt.total})"
        if attempt.attempt_no > 1:
            label += f" #{attempt.attempt_no}"
        self.console.print(pipeline_step_header(label))
        self.console.print()

        uses_structured_ui = self._uses_structured_ui(attempt)
        transcript_replayed = False
        if not uses_structured_ui:
            transcript_replayed = self._replay_transcript(attempt.transcript_id)

        if not transcript_replayed and not uses_structured_ui:
            for tool in attempt.tools:
                self.console.print(f"   ● {display_tool_name(tool.name)}")

        if attempt.step_type == "parallel_sub_pipeline" or attempt.sub_pipelines:
            self._render_sub_pipelines(attempt)

        if attempt.candidate_selection.state != "none":
            self._render_candidate_selection(attempt.candidate_selection)

        if self._should_render_attempt_status(attempt, transcript_replayed):
            self._render_attempt_status(attempt)
        self.console.print()

    def _render_sub_pipelines(self, attempt: DisplayAttempt) -> None:
        if attempt.status == "completed":
            for sub in attempt.sub_pipelines.values():
                self._render_completed_sub_pipeline_summary(sub)
            return
        if attempt.step_type == "parallel_sub_pipeline" and self._render_parallel_tabs_snapshot(attempt):
            return

        for sub in attempt.sub_pipelines.values():
            assert isinstance(sub, DisplaySubPipeline)
            name = sub.candidate_name or sub.sub_pipeline_id
            pieces = [f"   - {name}"]
            if sub.status == "running" and not sub.steps:
                pieces.append(_("Running"))
            elif sub.status == "completed":
                pieces.append(_("Completed"))
            elif sub.status == "failed":
                pieces.append(_("Failed") + (f": {sub.error}" if sub.error else ""))
            self.console.print(" | ".join(pieces))
            for sub_step in sub.steps:
                self._render_sub_step_attempt(sub_step)

    def _render_completed_sub_pipeline_summary(self, sub: DisplaySubPipeline) -> None:
        name = sub.candidate_name or sub.sub_pipeline_id
        if sub.status == "failed":
            message = _("  ✗ {name}: Failed").format(name=name)
            if sub.error:
                message += f" ({sub.error})"
            self.console.print(Text(message, style="red"))
        else:
            self.console.print(Text(_("  ✓ {name}: Completed").format(name=name), style="green"))

    def _render_parallel_tabs_snapshot(self, attempt: DisplayAttempt) -> bool:
        if not attempt.sub_pipelines:
            return False
        from iac_code.ui.components.parallel_tabs import CandidateState, CandidateStatus, ParallelTabsRenderer

        candidates: list[CandidateState] = []
        sub_pipelines = sorted(
            attempt.sub_pipelines.values(),
            key=lambda sub: (
                sub.candidate_index is None,
                sub.candidate_index if sub.candidate_index is not None else sub.candidate_name,
            ),
        )
        for fallback_index, sub in enumerate(sub_pipelines):
            status = CandidateStatus.RUNNING
            if sub.status == "completed":
                status = CandidateStatus.DONE
            elif sub.status == "failed":
                status = CandidateStatus.FAILED
            completed_steps = sum(1 for step in sub.steps if step.status == "completed")
            current_step = (
                display_step_name(self._current_sub_step_name(sub)) if status == CandidateStatus.RUNNING else ""
            )
            candidates.append(
                CandidateState(
                    sub_pipeline_id=sub.sub_pipeline_id,
                    candidate_index=sub.candidate_index if sub.candidate_index is not None else fallback_index,
                    name=sub.candidate_name or sub.sub_pipeline_id,
                    total_steps=sub.total_steps or max(completed_steps, len({step.step_id for step in sub.steps}), 1),
                    current_step=current_step,
                    completed_steps=completed_steps,
                    status=status,
                    error=sub.error or None,
                )
            )
        renderer = ParallelTabsRenderer(candidates=candidates, console=self.console)
        active_content = self._render_active_parallel_sub_pipeline_content(sub_pipelines[0])
        self.console.print(renderer.render_with_content(active_content))
        return True

    def _render_active_parallel_sub_pipeline_content(self, sub: DisplaySubPipeline) -> RenderableType:
        if self._history_renderable_factory is None or self._transcript_loader is None:
            return Text("")

        messages: list[Message] = []
        for step in sub.steps:
            if not step.transcript_id:
                continue
            loaded = self._transcript_loader(step.transcript_id)
            messages.extend(self._filter_step_transcript_messages(loaded))
        if not messages:
            return Text("")
        return self._history_renderable_factory(messages) or Text("")

    @staticmethod
    def _current_sub_step_name(sub: DisplaySubPipeline) -> str:
        for step in reversed(sub.steps):
            if step.status != "completed":
                return step.step_id
        return ""

    def _render_sub_step_attempt(self, sub_step: DisplaySubStepAttempt) -> None:
        suffix = f" #{sub_step.attempt_no}" if sub_step.attempt_no > 1 else ""
        status = {
            "completed": _("Completed"),
            "failed": _("Failed"),
            "interrupted": _("Interrupted"),
            "rolled_back": _("Rolled back"),
            "running": _("Running"),
        }.get(sub_step.status, sub_step.status or _("Running"))
        message = f"     · {display_step_name(sub_step.step_id)}{suffix}: {status}"
        if sub_step.error:
            message += f": {sub_step.error}"
        self.console.print(message)
        self._replay_transcript(sub_step.transcript_id)

    def _render_candidate_selection(self, selection: DisplayCandidateSelection) -> None:
        if selection.state in {"preparing", "waiting"}:
            self._render_candidate_selection_snapshot(selection)
            return
        elif selection.state == "selected":
            self._render_selected_candidate_static(selection)
            return
        elif selection.state == "completed":
            self._render_selected_candidate_static(selection)
            return

    def _render_candidate_selection_snapshot(self, selection: DisplayCandidateSelection) -> None:
        from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer

        renderer = CandidateSelectionRenderer(console=self.console)
        seed_candidates = [option for option in selection.options if isinstance(option, dict)]
        seed_candidates.extend(
            {
                "name": self._candidate_display_name(candidate),
                "candidate_index": candidate.candidate_index,
            }
            for candidate in self._ordered_candidates(selection)
        )
        renderer.seed_candidates(seed_candidates)

        for fallback_index, candidate in enumerate(self._ordered_candidates(selection)):
            candidate_name = self._candidate_display_name(candidate)
            if candidate.mermaid_source:
                renderer.add_diagram(
                    candidate_name,
                    candidate.mermaid_source,
                    candidate_index=candidate.candidate_index,
                )
            if candidate.summary or candidate.cost_items or candidate.total_monthly_cost:
                renderer.add_detail(
                    f"replay_candidate_{fallback_index}",
                    candidate_name,
                    candidate.summary,
                    candidate.cost_items,
                    candidate.total_monthly_cost,
                    candidate_index=candidate.candidate_index,
                )

        if selection.state == "waiting":
            renderer.enter_selection_mode()
        self.console.print(renderer.render())

    @staticmethod
    def _ordered_candidates(selection: DisplayCandidateSelection) -> list[DisplayCandidate]:
        return sorted(
            selection.candidates.values(),
            key=lambda candidate: (
                candidate.candidate_index is None,
                candidate.candidate_index if candidate.candidate_index is not None else candidate.name,
            ),
        )

    @staticmethod
    def _candidate_display_name(candidate: DisplayCandidate) -> str:
        return candidate.name or _("Candidate")

    def _render_selected_candidate_static(self, selection: DisplayCandidateSelection) -> None:
        candidate = self._selected_candidate(selection)
        selected_name = selection.selected_name or (candidate.name if candidate is not None else "")
        if selected_name:
            self.console.print(_("  ✓ Selected: {name}").format(name=selected_name))
        else:
            self.console.print(_("  Candidate selection completed"))
        if candidate is None:
            return
        if candidate.mermaid_source:
            self.console.print()
            self.console.print(self._render_diagram(candidate.mermaid_source))
            self.console.print(Rule(style="dim"))
        if candidate.summary:
            self.console.print(candidate.summary)
        if candidate.cost_items:
            self.console.print()
            self.console.print(self._render_cost_table(candidate.cost_items, candidate.total_monthly_cost))

    @staticmethod
    def _selected_candidate(selection: DisplayCandidateSelection) -> DisplayCandidate | None:
        if selection.selected_index is not None:
            for candidate in selection.candidates.values():
                if candidate.candidate_index == selection.selected_index:
                    return candidate
        if selection.selected_name:
            for candidate in selection.candidates.values():
                if candidate.name == selection.selected_name:
                    return candidate
        candidates = PipelineDisplayReplayRenderer._ordered_candidates(selection)
        return candidates[0] if candidates else None

    @staticmethod
    def _render_diagram(mermaid_source: str):
        try:
            from importlib import import_module

            render_rich = import_module("termaid").render_rich
            return render_rich(mermaid_source)
        except Exception:
            return Markdown(f"```mermaid\n{mermaid_source}\n```")

    @staticmethod
    def _render_cost_table(cost_items: list[dict], total: str):
        table = Table(title=_("Cost details"), show_header=True, border_style="dim")
        table.add_column(_("Product"), style="cyan")
        table.add_column(_("Specification"), style="dim")
        table.add_column(_("Monthly cost"), justify="right", style="green")
        for item in cost_items:
            table.add_row(
                str(item.get("name", "")),
                str(item.get("spec", "")),
                str(item.get("monthly_cost", "")),
            )
        table.add_section()
        table.add_row("", _("Total"), total, style="bold")
        return table

    @staticmethod
    def _should_render_attempt_status(attempt: DisplayAttempt, transcript_replayed: bool) -> bool:
        if attempt.status == "completed" and (
            transcript_replayed or PipelineDisplayReplayRenderer._uses_structured_ui(attempt)
        ):
            return False
        return True

    def _render_attempt_status(self, attempt: DisplayAttempt) -> None:
        if attempt.status == "completed":
            detail = f" ({attempt.summary})" if attempt.summary else ""
            self.console.print(Text(_("   ✓ Completed{detail}").format(detail=detail), style="green"))
        elif attempt.status == "failed":
            message = _("   ✗ Failed")
            if attempt.error:
                message += f": {attempt.error}"
            self.console.print(Text(message, style="red"))
        elif attempt.status == "rolled_back":
            target = display_step_name(attempt.rollback_to) if attempt.rollback_to else _("previous step")
            message = _("   ↩ Rolled back to {target}").format(target=target)
            if attempt.rollback_reason:
                message += f": {attempt.rollback_reason}"
            self.console.print(Text(message, style="yellow"))
        elif attempt.status == "interrupted":
            self.console.print(Text("   " + _("Interrupted"), style="yellow"))
        elif attempt.status == "waiting_input":
            self.console.print(Text("   " + _("Waiting for user input"), style="yellow"))

    @staticmethod
    def _uses_structured_ui(attempt: DisplayAttempt) -> bool:
        return attempt.step_type == "parallel_sub_pipeline" or attempt.ui_mode == "candidate_selection"

    def _replay_transcript(self, transcript_id: str) -> bool:
        if not transcript_id or self._history_replayer is None or self._transcript_loader is None:
            return False
        messages = self._transcript_loader(transcript_id)
        if not messages:
            return False
        return self._replay_transcript_messages(messages)

    def _replay_transcript_messages(self, messages: list[Message]) -> bool:
        history_replayer = self._history_replayer
        if history_replayer is None:
            return False
        ask_tool_uses = self._ask_user_question_tool_uses(messages)
        ask_tool_results = self._tool_results_by_id(messages)
        special_ask_tool_ids = {
            tool_use_id
            for tool_use_id in ask_tool_uses
            if (result := ask_tool_results.get(tool_use_id)) is not None and not result.is_error
        }
        excluded_tool_result_ids = special_ask_tool_ids
        replayed = False
        pending: list[Message] = []
        rendered_blocks = 0

        def flush_pending() -> None:
            nonlocal rendered_blocks, replayed
            if not pending:
                return
            for chunk in self._history_chunks(pending):
                if rendered_blocks:
                    self.console.print()
                history_replayer(chunk)
                rendered_blocks += 1
            pending.clear()
            replayed = True

        for message in messages:
            if message.role == "user":
                filtered = self._filter_tool_result_message(message, excluded_tool_result_ids)
                if filtered is not None:
                    pending.append(filtered)
                continue

            pending.append(message)
            for tool_use in self._message_ask_tool_uses(message):
                if tool_use.id not in special_ask_tool_ids:
                    continue
                flush_pending()
                if rendered_blocks:
                    self.console.print()
                self._render_ask_user_question(tool_use, ask_tool_results.get(tool_use.id))
                rendered_blocks += 1
                replayed = True

        flush_pending()
        return replayed

    @staticmethod
    def _history_chunks(messages: list[Message]) -> list[list[Message]]:
        tool_result_blocks: list[ToolResultBlock] = []
        chunks: list[list[Message]] = []
        for message in messages:
            if isinstance(message.content, list):
                tool_result_blocks.extend(block for block in message.content if isinstance(block, ToolResultBlock))
        support_content: list[ContentBlock] = list(tool_result_blocks)
        support_message = Message(role="user", content=support_content) if support_content else None
        for message in messages:
            if message.role == "assistant":
                chunk = [message]
                if support_message is not None:
                    chunk.append(support_message)
                chunks.append(chunk)
            elif message.role == "user" and not PipelineDisplayReplayRenderer._is_tool_result_only(message):
                chunks.append([message])
        if not chunks and messages:
            chunks.append(messages)
        return chunks

    @staticmethod
    def _ask_user_question_tool_uses(messages: list[Message]) -> dict[str, ToolUseBlock]:
        uses: dict[str, ToolUseBlock] = {}
        for message in messages:
            for tool_use in PipelineDisplayReplayRenderer._message_ask_tool_uses(message):
                uses[tool_use.id] = tool_use
        return uses

    @staticmethod
    def _message_ask_tool_uses(message: Message) -> list[ToolUseBlock]:
        if message.role != "assistant" or not isinstance(message.content, list):
            return []
        return [
            block for block in message.content if isinstance(block, ToolUseBlock) and block.name == "ask_user_question"
        ]

    @staticmethod
    def _tool_results_by_id(messages: list[Message]) -> dict[str, ToolResultBlock]:
        results: dict[str, ToolResultBlock] = {}
        for message in messages:
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        results[block.tool_use_id] = block
        return results

    @staticmethod
    def _filter_tool_result_message(message: Message, excluded_tool_result_ids: set[str]) -> Message | None:
        if message.role == "user" and not PipelineDisplayReplayRenderer._is_tool_result_only(message):
            return None
        if not isinstance(message.content, list):
            return message
        filtered = [
            block
            for block in message.content
            if not isinstance(block, ToolResultBlock) or block.tool_use_id not in excluded_tool_result_ids
        ]
        if not filtered:
            return None
        return Message(
            role=message.role,
            content=filtered,
            token_count=message.token_count,
            elapsed_seconds=message.elapsed_seconds,
        )

    def _render_ask_user_question(self, tool_use: ToolUseBlock, result: ToolResultBlock | None) -> None:
        tool_input = tool_use.input if isinstance(tool_use.input, dict) else {}
        question = str(tool_input.get("question") or "").strip()
        if question:
            self.console.print(Text(question, style="bold"))
        options = tool_input.get("options")
        if isinstance(options, list):
            for index, raw_option in enumerate(options, 1):
                if not isinstance(raw_option, dict):
                    continue
                option = cast(dict[str, Any], raw_option)
                label = str(option.get("label") or option.get("id") or "")
                if label:
                    self.console.print(Text(f"{index}. {label}", style="cyan"))
                description = str(option.get("description") or "").strip()
                if description:
                    self.console.print(Text(f"   {description}", style="dim"))
        if tool_input.get("allow_free_text", True):
            free_text_prompt = str(tool_input.get("free_text_prompt") or "").strip()
            if free_text_prompt:
                self.console.print(Text(free_text_prompt, style="dim"))
        answer = self._ask_user_answer_text(result)
        if answer:
            self.console.print(f"  > {answer}")

    @staticmethod
    def _ask_user_answer_text(result: ToolResultBlock | None) -> str:
        if result is None:
            return ""
        if result.is_error:
            return ""
        try:
            parsed = json.loads(result.content)
        except (TypeError, ValueError):
            return result.content
        if not isinstance(parsed, dict):
            return result.content
        for key in ("free_text", "selected_label", "selected_id"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _render_pipeline_completed(self, model: DisplayReplayModel) -> None:
        self.console.print()
        line = "  " + _("✔ Pipeline completed")
        if model.duration_s is not None:
            line += " " + _("── total time {duration}").format(duration=self._format_duration(model.duration_s))
        self.console.print(Text(line, style="green"))
        self.console.print(_("Pipeline completed. Normal chat is now active."))

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds >= 60:
            return f"{seconds / 60:.1f}m"
        return f"{seconds:.1f}s"

    @staticmethod
    def _filter_step_transcript_messages(messages: list[Message]) -> list[Message]:
        filtered: list[Message] = []
        for message in messages:
            if message.role == "user" and not PipelineDisplayReplayRenderer._is_tool_result_only(message):
                continue
            filtered.append(message)
        return filtered

    @staticmethod
    def _is_tool_result_only(message: Message) -> bool:
        return isinstance(message.content, list) and all(
            isinstance(block, ToolResultBlock) for block in message.content
        )
