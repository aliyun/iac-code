"""Request-local OpenAI-compatible stream response adapters."""

from __future__ import annotations

import re
import uuid
from typing import Any

from iac_code.i18n import _
from iac_code.providers.base import ToolDefinition
from iac_code.providers.qwen_tool_call_parser import (
    StrictToolCallAssembler,
    ToolCallProtocolError,
    recover_xml_tool_calls,
)
from iac_code.types.stream_events import (
    StreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from iac_code.utils.tool_input_parser import parse_tool_input_events


class UnsafeStreamProtocolError(RuntimeError):
    """A stream contains Qwen content that must never use non-stream fallback."""

    def __init__(self, message_id: str) -> None:
        self.i18n_message_id = message_id
        self.i18n_message_args: dict[str, Any] | None = None
        super().__init__(_(message_id))


class CumulativeDeltaNormalizer:
    """Normalize a channel that may transition from incremental to cumulative."""

    EXACT_REPEAT_THRESHOLD = 64
    DETECTION_WINDOW = 1024

    def __init__(self) -> None:
        self._mode = "incremental"
        self._emitted_text = ""
        self._emitted_length = 0

    @property
    def mode(self) -> str:
        return self._mode

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        if not self._emitted_text:
            self._emitted_text = chunk
            self._emitted_length = len(chunk)
            return chunk
        if self._mode == "cumulative":
            if chunk.startswith(self._emitted_text):
                suffix = chunk[len(self._emitted_text) :]
                self._emitted_text = chunk
                self._emitted_length = len(chunk)
                return suffix
            if self._emitted_text.startswith(chunk):
                return ""
            self._mode = "incremental"
            self._emitted_text = chunk
            self._emitted_length += len(chunk)
            return chunk
        if len(chunk) > len(self._emitted_text) and chunk.startswith(self._emitted_text):
            baseline_length = len(self._emitted_text)
            baseline_frozen = baseline_length >= self.DETECTION_WINDOW and self._emitted_length > baseline_length
            slice_from = self._emitted_length if baseline_frozen else baseline_length
            if len(chunk) > slice_from:
                suffix = chunk[slice_from:]
                self._emitted_text = chunk
                self._emitted_length = len(chunk)
                self._mode = "cumulative"
                return suffix
        if chunk == self._emitted_text:
            if len(chunk) >= self.EXACT_REPEAT_THRESHOLD:
                self._mode = "cumulative"
                return ""
            self._emitted_length += len(chunk)
            return chunk
        if len(self._emitted_text) < self.DETECTION_WINDOW:
            self._emitted_text += chunk
        self._emitted_length += len(chunk)
        return chunk


class OpenAIStreamResponseAdapter:
    """Default adapter preserving the existing OpenAI event contract."""

    def __init__(self, provider: Any, tools: list[ToolDefinition] | None) -> None:
        self._provider = provider
        self._tools = tools
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._message_provider_metadata: dict[str, Any] = {}
        self._terminal_stop_reason_override: str | None = None

    @property
    def terminal_stop_reason_override(self) -> str | None:
        return self._terminal_stop_reason_override

    def feed(self, delta: Any, finish_reason: str | None) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        provider_metadata = self._provider._message_provider_metadata(delta)
        if provider_metadata and provider_metadata != self._message_provider_metadata:
            self._message_provider_metadata = provider_metadata
            events.append(ThinkingDeltaEvent(text="", provider_metadata=provider_metadata))
        reasoning = self._provider._extract_reasoning_text(delta)
        if reasoning:
            events.append(ThinkingDeltaEvent(text=reasoning))
        content = getattr(delta, "content", None)
        if content:
            events.append(TextDeltaEvent(text=content))
        tool_calls = getattr(delta, "tool_calls", None)
        if tool_calls:
            events.extend(self._feed_legacy_tool_calls(tool_calls))
        return events

    def _feed_legacy_tool_calls(self, tool_calls: Any) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for tc_delta in tool_calls:
            idx = tc_delta.index
            if idx not in self._tool_calls:
                self._tool_calls[idx] = {
                    "id": tc_delta.id or "",
                    "name": "",
                    "arguments": "",
                    "argument_deltas": [],
                    "arguments_waited_for_id": False,
                    "ready_to_start": False,
                    "started": False,
                    "provider_metadata": {},
                }
            current = self._tool_calls[idx]
            if tc_delta.id and not current["id"]:
                current["id"] = tc_delta.id
            provider_metadata = self._provider._tool_provider_metadata(tc_delta)
            if provider_metadata:
                current["provider_metadata"].update(provider_metadata)
            has_name_delta = bool(tc_delta.function and tc_delta.function.name)
            if has_name_delta:
                current["name"] += tc_delta.function.name
            if tc_delta.function and tc_delta.function.arguments:
                if not current["id"]:
                    current["arguments_waited_for_id"] = True
                current["arguments"] += tc_delta.function.arguments
                current["argument_deltas"].append(tc_delta.function.arguments)
            if (
                current["id"]
                and current["name"]
                and current["arguments"]
                and not current["started"]
                and not has_name_delta
            ):
                current["ready_to_start"] = True
        for start_idx in sorted(self._tool_calls):
            pending = self._tool_calls[start_idx]
            if pending["started"]:
                continue
            if not pending["ready_to_start"]:
                break
            pending["started"] = True
            events.append(
                ToolUseStartEvent(
                    tool_use_id=pending["id"],
                    name=pending["name"],
                    provider_metadata=pending["provider_metadata"] or None,
                )
            )
        for flush_idx in sorted(self._tool_calls):
            pending = self._tool_calls[flush_idx]
            if not pending["started"]:
                continue
            deltas = pending["argument_deltas"]
            pending["argument_deltas"] = []
            if pending["arguments_waited_for_id"]:
                deltas = ["".join(deltas)]
                pending["arguments_waited_for_id"] = False
            events.extend(ToolInputDeltaEvent(tool_use_id=pending["id"], partial_json=item) for item in deltas)
        return events

    def finalize(self, finish_reason: str | None) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for idx in sorted(self._tool_calls):
            call = self._tool_calls[idx]
            if not call["id"]:
                call["id"] = f"call_{uuid.uuid4().hex[:24]}"
            if not call["started"]:
                events.append(
                    ToolUseStartEvent(
                        tool_use_id=call["id"],
                        name=call["name"],
                        provider_metadata=call["provider_metadata"] or None,
                    )
                )
            deltas = call["argument_deltas"]
            if call["arguments_waited_for_id"]:
                deltas = ["".join(deltas)]
            events.extend(ToolInputDeltaEvent(tool_use_id=call["id"], partial_json=item) for item in deltas)
            for event in parse_tool_input_events(call["id"], call["name"], call["arguments"]):
                if isinstance(event, ToolUseEndEvent) and event.tool_use_id == call["id"]:
                    event.provider_metadata = call["provider_metadata"] or None
                events.append(event)
        return events


class _QwenContentGuard:
    _XML_STARTS = ("<invoke", "<function_calls")
    _MAX_CANDIDATE = 128 * 1024
    _LEADING_TAG = re.compile(r"^\s*</?think(?:ing)?\s*>", re.IGNORECASE)
    _ANY_TAG = re.compile(r"</?think(?:ing)?\s*>", re.IGNORECASE)
    _STANDALONE_CLOSING = re.compile(r"^\s*</(think|thinking)\s*>\s*$", re.IGNORECASE)
    _FENCE = re.compile(r"^ {0,3}((`{3,})|(~{3,}))")
    _BLOCKQUOTE = re.compile(r"^ {0,3}>")

    def __init__(self) -> None:
        self._thinking_state = "probe"
        self._thinking_buffer = ""
        self._tag_name: str | None = None
        self._tag_closing = False
        self._xml_pending = ""
        self._xml_candidate: str | None = None
        self._fence_delimiter: str | None = None
        self._fence_length = 0
        self._fence_line = ""

    def feed_tags(self, text: str) -> list[str]:
        if not text:
            return []
        if self._thinking_state == "tag":
            self._thinking_buffer += text
            if len(self._thinking_buffer) > self._MAX_CANDIDATE:
                raise UnsafeStreamProtocolError("Qwen thinking-tag candidate exceeded the safety limit.")
            return []
        if self._thinking_state == "probe":
            self._thinking_buffer += text
            stripped = self._thinking_buffer.lstrip()
            complete = self._LEADING_TAG.match(self._thinking_buffer)
            if complete is not None:
                self._thinking_state = "tag"
                literal = complete.group(0).strip().lower()
                self._tag_closing = literal.startswith("</")
                self._tag_name = "thinking" if "thinking" in literal else "think"
                return []
            if self._is_possible_tag_prefix(stripped):
                return []
            self._thinking_state = "pass"
            released = self._thinking_buffer
            self._thinking_buffer = ""
            return [released]
        return [text]

    @staticmethod
    def _is_possible_tag_prefix(text: str) -> bool:
        candidate = text.lower()
        if not candidate:
            return True
        for tag in ("<think", "<thinking", "</think", "</thinking"):
            if tag.startswith(candidate):
                return True
            if candidate.startswith(tag) and re.fullmatch(r"\s*(?:>\s*)?", candidate[len(tag) :]):
                return True
        return False

    def feed_xml(self, text: str) -> list[str]:
        if self._xml_candidate is not None:
            self._xml_candidate += text
            if len(self._xml_candidate) > self._MAX_CANDIDATE:
                candidate = self._xml_candidate
                self._xml_candidate = None
                self._update_fence(candidate)
                return [candidate]
            return []
        combined = self._xml_pending + text
        self._xml_pending = ""
        output: list[str] = []
        while combined:
            positions = [(combined.find(start), start) for start in self._XML_STARTS]
            positions = [(position, start) for position, start in positions if position >= 0]
            if positions:
                position, _ = min(positions)
                prefix = combined[:position]
                self._update_fence(prefix)
                if self._inside_fence() or self._inside_blockquote():
                    emitted = prefix + combined[position]
                    output.append(emitted)
                    self._update_fence(combined[position])
                    combined = combined[position + 1 :]
                    continue
                if prefix:
                    output.append(prefix)
                self._xml_candidate = combined[position:]
                return output
            suffix_length = self._longest_candidate_prefix_suffix(combined)
            if suffix_length:
                released = combined[:-suffix_length]
                if released:
                    output.append(released)
                    self._update_fence(released)
                self._xml_pending = combined[-suffix_length:]
            else:
                output.append(combined)
                self._update_fence(combined)
            return output
        return output

    @classmethod
    def _longest_candidate_prefix_suffix(cls, text: str) -> int:
        return max(
            (size for start in cls._XML_STARTS for size in range(1, len(start)) if text.endswith(start[:size])),
            default=0,
        )

    def _update_fence(self, text: str) -> None:
        for character in text:
            if character == "\n":
                self._process_fence_line(self._fence_line)
                self._fence_line = ""
            else:
                self._fence_line += character

    def _process_fence_line(self, line: str) -> None:
        match = self._FENCE.match(line)
        if match is None:
            return
        delimiter = "`" if match.group(2) else "~"
        length = len(match.group(1))
        if self._fence_delimiter is None:
            self._fence_delimiter = delimiter
            self._fence_length = length
        elif (
            self._fence_delimiter == delimiter
            and length >= self._fence_length
            and not line[match.end() :].strip()
        ):
            self._fence_delimiter = None
            self._fence_length = 0

    def _inside_fence(self) -> bool:
        if self._fence_delimiter is not None:
            return True
        return self._FENCE.match(self._fence_line) is not None

    def _inside_blockquote(self) -> bool:
        return self._BLOCKQUOTE.match(self._fence_line) is not None

    def validate_pre_tools(self, *, has_reasoning: bool) -> None:
        if self._thinking_state != "tag" or self._tag_closing:
            return
        if has_reasoning or not self._is_balanced_content_tag_block(self._thinking_buffer):
            raise UnsafeStreamProtocolError("Qwen emitted an unsafe or conflicting thinking-tag block.")

    @property
    def has_closing_tag_candidate(self) -> bool:
        return self._thinking_state == "tag" and self._tag_closing

    @classmethod
    def _is_balanced_content_tag_block(cls, text: str) -> bool:
        leading = cls._LEADING_TAG.match(text)
        if leading is None or leading.group(0).lstrip().startswith("</"):
            return False
        depth = 0
        for match in cls._ANY_TAG.finditer(text):
            depth += -1 if match.group(0).lstrip().startswith("</") else 1
            if depth < 0:
                return False
        return depth == 0

    def finalize_tags(
        self,
        *,
        finish_reason: str | None,
        native_calls: list[dict[str, Any]],
        has_reasoning: bool,
        reasoning_has_tag: bool = False,
    ) -> tuple[list[str], bool]:
        if self._thinking_state == "probe":
            pending = self._thinking_buffer
            self._thinking_buffer = ""
            self._thinking_state = "pass"
            released = [pending] if pending else []
        else:
            released = []
        if self._thinking_state == "tag":
            literal = self._thinking_buffer
            if not self._tag_closing:
                if not has_reasoning and self._is_balanced_content_tag_block(literal):
                    return [literal], False
                raise UnsafeStreamProtocolError("Qwen emitted an unsafe or conflicting thinking-tag block.")
            closing = self._STANDALONE_CLOSING.fullmatch(literal)
            safe_closing = (
                finish_reason == "tool_calls"
                and bool(native_calls)
                and not reasoning_has_tag
                and closing is not None
            )
            if safe_closing:
                return [], True
            raise UnsafeStreamProtocolError("Qwen emitted a stray thinking closing tag.")
        return released, False

    def finalize_xml(self, *, native_calls: list[dict[str, Any]]) -> tuple[list[str], str | None]:
        released: list[str] = []
        if self._xml_candidate is not None:
            candidate = self._xml_candidate
            self._xml_candidate = None
            if native_calls:
                released.append(candidate)
                return released, None
            return released, candidate
        if self._xml_pending:
            released.append(self._xml_pending)
            self._xml_pending = ""
        return released, None


class QwenStreamResponseAdapter(OpenAIStreamResponseAdapter):
    """Qwen-only ordered normalizer, guards, strict tools, and XML recovery."""

    def __init__(self, provider: Any, tools: list[ToolDefinition] | None) -> None:
        super().__init__(provider, tools)
        self._content_normalizer = CumulativeDeltaNormalizer()
        self._reasoning_normalizer = CumulativeDeltaNormalizer()
        self._guard = _QwenContentGuard()
        self._strict_tools = StrictToolCallAssembler()
        self._has_reasoning = False
        self._reasoning_has_tag = False
        self._reasoning_tag_probe = ""

    def feed(self, delta: Any, finish_reason: str | None) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        provider_metadata = self._provider._message_provider_metadata(delta)
        if provider_metadata and provider_metadata != self._message_provider_metadata:
            self._message_provider_metadata = provider_metadata
            events.append(ThinkingDeltaEvent(text="", provider_metadata=provider_metadata))
        reasoning = self._provider._extract_reasoning_text(delta)
        normalized_reasoning = self._reasoning_normalizer.feed(reasoning) if reasoning else ""
        if normalized_reasoning:
            self._has_reasoning = True
            if not self._reasoning_has_tag:
                tag_probe = self._reasoning_tag_probe + normalized_reasoning
                self._reasoning_has_tag = self._guard._ANY_TAG.search(tag_probe) is not None
                if self._reasoning_has_tag:
                    self._reasoning_tag_probe = ""
                else:
                    last_open = tag_probe.rfind("<")
                    possible = tag_probe[last_open:] if last_open >= 0 else ""
                    self._reasoning_tag_probe = (
                        possible if self._guard._is_possible_tag_prefix(possible) else ""
                    )
            events.append(ThinkingDeltaEvent(text=normalized_reasoning))
        content = getattr(delta, "content", None)
        normalized_content = self._content_normalizer.feed(content) if isinstance(content, str) and content else ""
        tag_released = self._guard.feed_tags(normalized_content)
        tool_calls = getattr(delta, "tool_calls", None)
        if tool_calls:
            self._strict_tools.feed(tool_calls)
        for tagged_text in tag_released:
            for released in self._guard.feed_xml(tagged_text):
                if released:
                    events.append(TextDeltaEvent(text=released))
        return events

    def finalize(self, finish_reason: str | None) -> list[StreamEvent]:
        # Tag validation runs first so a malformed native call cannot downgrade
        # a confirmed tag leak into the generic non-streaming fallback path.
        self._guard.validate_pre_tools(has_reasoning=self._has_reasoning)
        try:
            native_calls = self._strict_tools.finalize(finish_reason)
        except ToolCallProtocolError as exc:
            if self._guard.has_closing_tag_candidate:
                raise UnsafeStreamProtocolError(
                    "Qwen emitted a closing thinking tag without a valid native tool call."
                ) from exc
            raise
        tag_released, _sanitized = self._guard.finalize_tags(
            finish_reason=finish_reason,
            native_calls=native_calls,
            has_reasoning=self._has_reasoning,
            reasoning_has_tag=self._reasoning_has_tag,
        )
        events: list[StreamEvent] = []
        for tagged_text in tag_released:
            events.extend(TextDeltaEvent(text=text) for text in self._guard.feed_xml(tagged_text) if text)
        released, xml_candidate = self._guard.finalize_xml(native_calls=native_calls)
        events.extend(TextDeltaEvent(text=text) for text in released if text)
        if native_calls:
            events.extend(_tool_use_events(native_calls))
            return events
        if xml_candidate is not None:
            recovered = recover_xml_tool_calls(xml_candidate, self._tools) if finish_reason is not None else None
            if recovered is None:
                events.append(TextDeltaEvent(text=xml_candidate))
            else:
                if recovered.remaining_text:
                    events.append(TextDeltaEvent(text=recovered.remaining_text))
                if recovered.calls:
                    self._terminal_stop_reason_override = "tool_use"
                events.extend(_tool_use_events(recovered.calls))
        return events


def _tool_use_events(calls: list[dict[str, Any]]) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    for call in calls:
        events.append(ToolUseStartEvent(tool_use_id=call["id"], name=call["name"]))
        raw_arguments = call.get("raw_arguments")
        if isinstance(raw_arguments, str) and raw_arguments:
            events.append(ToolInputDeltaEvent(tool_use_id=call["id"], partial_json=raw_arguments))
        events.append(ToolUseEndEvent(tool_use_id=call["id"], name=call["name"], input=call["input"]))
    return events
