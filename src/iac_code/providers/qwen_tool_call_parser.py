"""Strict native and conservative XML tool-call parsing for Qwen responses."""

from __future__ import annotations

import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from iac_code.i18n import _
from iac_code.providers.base import ToolDefinition

_MAX_XML_CHARS = 128 * 1024
_MAX_PARAMETER_CHARS = 32 * 1024


class ToolCallProtocolError(ValueError):
    """A structured Qwen tool call cannot be safely assembled."""

    def __init__(self, message_id: str) -> None:
        self.i18n_message_id = message_id
        self.i18n_message_args: dict[str, Any] | None = None
        super().__init__(_(message_id))


def _localizable_message_id(message_id: str) -> str:
    """Mark a deferred protocol error for catalog extraction without translating it early."""
    return message_id


def strict_tool_arguments(raw: Any, *, present: bool = True) -> dict[str, Any]:
    """Parse one native call's arguments without the legacy ``{}`` fallback."""
    if not present or raw is None or raw == "":
        return {}
    if not isinstance(raw, str) or not raw.strip():
        raise ToolCallProtocolError("Qwen tool arguments must be a non-empty JSON object.")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ToolCallProtocolError("Qwen tool arguments are malformed JSON.") from exc
    if not isinstance(parsed, dict):
        raise ToolCallProtocolError("Qwen tool arguments must decode to an object.")
    return parsed


def parse_non_streaming_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for call in tool_calls or []:
        call_id = _field(call, "id")
        function = _field(call, "function")
        name = _field(function, "name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise ToolCallProtocolError("Qwen native tool call is missing its ID or name.")
        arguments = _field(function, "arguments", _MISSING)
        parsed.append(
            {
                "id": call_id,
                "name": name,
                "input": strict_tool_arguments(arguments, present=arguments is not _MISSING),
            }
        )
    return parsed


@dataclass
class _Slot:
    order: int
    index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    arguments_present: bool = False
    depth: int = 0
    in_string: bool = False
    escape: bool = False

    def has_complete_object(self) -> bool:
        if not self.arguments_present or self.arguments == "":
            return True
        if self.depth != 0 or self.in_string:
            return False
        try:
            return isinstance(json.loads(self.arguments), dict)
        except (TypeError, ValueError):
            return False

    def can_accept_anonymous_arguments(self) -> bool:
        return not self.arguments_present or self.arguments == "" or not self.has_complete_object()


class StrictToolCallAssembler:
    """Request-local assembler that refuses ambiguous or malformed calls."""

    def __init__(self) -> None:
        self._slots: list[_Slot] = []
        self._invalid_reason: str | None = None

    @property
    def has_fragments(self) -> bool:
        return bool(self._slots) or self._invalid_reason is not None

    def feed(self, fragments: Any) -> None:
        for fragment in fragments or []:
            index = _field(fragment, "index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                self._invalid_reason = _localizable_message_id("Invalid Qwen tool-call index.")
                continue
            function = _field(fragment, "function")
            call_id = _string_or_empty(_field(fragment, "id"))
            name = _string_or_empty(_field(function, "name"))
            arguments = _field(function, "arguments", _MISSING)
            if not call_id and not name and (arguments is _MISSING or arguments is None or arguments == ""):
                # DashScope emits identity-free empty delimiter chunks before
                # and after real argument deltas. They carry no protocol state;
                # treating the trailing delimiter as a new slot creates a
                # spurious anonymous call after the actual call is complete.
                continue
            slot = self._select_slot(index, call_id, name, arguments)
            if slot is None:
                continue
            if call_id:
                if slot.call_id and slot.call_id != call_id:
                    self._invalid_reason = _localizable_message_id("Conflicting Qwen tool-call identity.")
                    continue
                slot.call_id = call_id
            if name:
                if slot.name and slot.name != name:
                    self._invalid_reason = _localizable_message_id("Conflicting Qwen tool-call name.")
                    continue
                slot.name = name
            if arguments is not _MISSING and arguments is not None:
                if not isinstance(arguments, str):
                    self._invalid_reason = _localizable_message_id("Qwen tool-call arguments must be a string.")
                    continue
                if call_id and slot.call_id == call_id and slot.arguments and slot.has_complete_object():
                    # Some compatible endpoints replay a completed chunk with
                    # the same stable id. It is not a second invocation.
                    continue
                slot.arguments_present = True
                slot.arguments += arguments
                self._scan_argument_fragment(slot, arguments)

    def _select_slot(self, index: int, call_id: str, name: str, arguments: Any) -> _Slot | None:
        if call_id:
            known = [slot for slot in self._slots if slot.call_id == call_id]
            if len(known) == 1:
                return known[0]
            if len(known) > 1:
                self._invalid_reason = _localizable_message_id("Duplicate Qwen tool-call identity.")
                return None
        same_index = [slot for slot in self._slots if slot.index == index]
        if call_id:
            compatible = [
                slot for slot in same_index if not slot.call_id and (not name or not slot.name or slot.name == name)
            ]
            if len(compatible) == 1:
                return compatible[0]
            if len(compatible) > 1:
                self._invalid_reason = _localizable_message_id("Ambiguous anonymous Qwen tool-call identity.")
                return None
            global_compatible = []
            if not _is_complete_argument_object(arguments):
                global_compatible = [
                    slot
                    for slot in self._slots
                    if not slot.call_id
                    and slot.can_accept_anonymous_arguments()
                    and (not name or not slot.name or slot.name == name)
                ]
            if len(global_compatible) == 1:
                return global_compatible[0]
            if len(global_compatible) > 1:
                self._invalid_reason = _localizable_message_id("Ambiguous anonymous Qwen tool-call identity.")
                return None
            conflicting = [slot for slot in same_index if slot.call_id and slot.call_id != call_id]
            if conflicting and any(not slot.has_complete_object() for slot in conflicting):
                self._invalid_reason = _localizable_message_id("Conflicting Qwen tool-call identity.")
        else:
            if name:
                named = [slot for slot in same_index if slot.name == name]
                if len(named) == 1:
                    return named[0]
                unnamed = [slot for slot in same_index if not slot.name]
                if len(unnamed) == 1:
                    return unnamed[0]
                if len(named) > 1 or len(unnamed) > 1:
                    self._invalid_reason = _localizable_message_id("Ambiguous anonymous Qwen tool-call identity.")
                    return None
                if same_index and any(not slot.has_complete_object() for slot in same_index):
                    self._invalid_reason = _localizable_message_id("Conflicting Qwen tool-call name.")
                    return None
            compatible = [slot for slot in same_index if slot.can_accept_anonymous_arguments()]
            if len(compatible) == 1:
                return compatible[0]
            if len(compatible) > 1:
                self._invalid_reason = _localizable_message_id("Ambiguous anonymous Qwen tool-call fragment.")
                return None
            incomplete = [slot for slot in self._slots if slot.can_accept_anonymous_arguments()]
            if len(incomplete) == 1:
                return incomplete[0]
            if len(incomplete) > 1:
                self._invalid_reason = _localizable_message_id("Ambiguous anonymous Qwen tool-call fragment.")
                return None
        slot = _Slot(order=len(self._slots), index=index)
        self._slots.append(slot)
        return slot

    def _scan_argument_fragment(self, slot: _Slot, fragment: str) -> None:
        for character in fragment:
            if not slot.in_string:
                if character in "[{":
                    slot.depth += 1
                elif character in "]}":
                    slot.depth -= 1
                    if slot.depth < 0:
                        self._invalid_reason = _localizable_message_id("Invalid Qwen tool-call JSON structure.")
            if character == '"' and not slot.escape:
                slot.in_string = not slot.in_string
            slot.escape = character == "\\" and not slot.escape

    def finalize(self, finish_reason: str | None) -> list[dict[str, Any]]:
        if self._invalid_reason is not None:
            raise ToolCallProtocolError(self._invalid_reason)
        if finish_reason == "tool_calls" and not self._slots:
            raise ToolCallProtocolError("Qwen response ended with tool_calls but no complete tool call.")
        result: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for slot in sorted(self._slots, key=lambda item: item.order):
            if not slot.call_id or not slot.name:
                raise ToolCallProtocolError("Qwen tool call is anonymous or nameless.")
            if slot.call_id in seen_ids:
                raise ToolCallProtocolError("Duplicate Qwen tool-call ID.")
            if slot.depth != 0 or slot.in_string:
                raise ToolCallProtocolError("Qwen tool-call arguments ended before JSON was complete.")
            seen_ids.add(slot.call_id)
            result.append(
                {
                    "id": slot.call_id,
                    "name": slot.name,
                    "raw_arguments": slot.arguments,
                    "input": strict_tool_arguments(slot.arguments, present=slot.arguments_present),
                }
            )
        return result


@dataclass(frozen=True)
class XmlToolCallRecovery:
    calls: list[dict[str, Any]]
    remaining_text: str


_INVOKE_PATTERN = re.compile(
    r"<invoke\s+name=(?P<quote>[\"'])(?P<name>[^\"']+)(?P=quote)>(?P<body>[\s\S]*?)</invoke>",
)
_FUNCTION_CALLS_PATTERN = re.compile(r"<function_calls>(?P<body>[\s\S]*?)</function_calls>")
_FUNCTION_CALLS_TOKEN_PATTERN = re.compile(r"</?function_calls>")
_FUNCTION_CALLS_HINT_PATTERN = re.compile(r"<\s*/?\s*function_calls\b", re.IGNORECASE)
_FENCE_PATTERN = re.compile(r"^ {0,3}((`{3,})|(~{3,}))")
_ENTITY_PATTERN = re.compile(r"&(?:#(?:x[0-9a-fA-F]+|[0-9]+)|[A-Za-z][A-Za-z0-9]+);")
_SUPPORTED_ENTITIES = {"&amp;", "&lt;", "&gt;", "&quot;", "&apos;"}


def recover_xml_tool_calls(text: str, tools: list[ToolDefinition] | None) -> XmlToolCallRecovery | None:
    """Recover only the observed parameterized ``<invoke>`` dialect."""
    if not tools or not text or len(text) > _MAX_XML_CHARS or "<invoke" not in text:
        return None
    matches = list(_INVOKE_PATTERN.finditer(text))
    if not matches:
        return None
    invoke_ranges = [(match.start(), match.end()) for match in matches]
    allowed_names = {tool.name for tool in tools}
    recovered: list[dict[str, Any]] = []
    recovered_ranges: list[tuple[int, int]] = []
    for match in matches:
        if _position_inside_markdown_fence(text, match.start(), invoke_ranges) or _position_inside_markdown_quote(
            text, match.start()
        ):
            continue
        parsed = _parse_invoke_block(match.group(0), allowed_names)
        if parsed is None:
            return None
        recovered.append(parsed)
        recovered_ranges.append((match.start(), match.end()))
    if not recovered:
        return None
    if _has_unmatched_invoke_start(text, invoke_ranges):
        return None

    wrapper_ranges = _complete_function_calls_wrapper_ranges(text)
    if wrapper_ranges is None:
        return None
    for wrapper_start, wrapper_end in wrapper_ranges:
        wrapper = _FUNCTION_CALLS_PATTERN.fullmatch(text[wrapper_start:wrapper_end])
        if wrapper is None:
            return None
        contained = [
            item
            for item in recovered_ranges
            if wrapper_start + wrapper.start("body") <= item[0]
            and item[1] <= wrapper_start + wrapper.end("body")
        ]
        if not contained:
            return None
        body_without_invokes = _remove_ranges(
            text,
            contained,
            start=wrapper_start + wrapper.start("body"),
            end=wrapper_start + wrapper.end("body"),
        )
        if body_without_invokes.strip():
            return None

    removal_ranges = list(wrapper_ranges)
    removal_ranges.extend(
        item
        for item in recovered_ranges
        if not any(wrapper_start <= item[0] and item[1] <= wrapper_end for wrapper_start, wrapper_end in wrapper_ranges)
    )
    remaining = _remove_ranges(text, removal_ranges).strip()
    prose = re.sub(r"\n{3,}", "\n\n", remaining).strip()
    if len(prose) / len(text) > 0.8:
        return None
    return XmlToolCallRecovery(
        calls=recovered,
        remaining_text=re.sub(r"\n{3,}", "\n\n", remaining),
    )


def _parse_invoke_block(block: str, allowed_names: set[str]) -> dict[str, Any] | None:
    if any(entity.group(0) not in _SUPPORTED_ENTITIES for entity in _ENTITY_PATTERN.finditer(block)):
        return None
    try:
        invoke = ET.fromstring(block)
    except ET.ParseError:
        return None
    if invoke.tag != "invoke" or set(invoke.attrib) != {"name"} or (invoke.text or "").strip():
        return None
    name = invoke.attrib.get("name", "")
    if name not in allowed_names:
        return None
    arguments: dict[str, Any] = {}
    parameters = list(invoke)
    if not parameters:
        return None
    for parameter in parameters:
        if parameter.tag != "parameter" or set(parameter.attrib) != {"name"} or list(parameter):
            return None
        key = parameter.attrib.get("name", "")
        if not key or key in arguments or (parameter.tail or "").strip():
            return None
        value = parameter.text or ""
        if len(value) > _MAX_PARAMETER_CHARS:
            return None
        value = _trim_one_boundary_newline(value)
        parsed_value: Any = value
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed_value = json.loads(value)
            except ValueError:
                parsed_value = value
        arguments[key] = parsed_value
    return {"id": f"call_{uuid.uuid4().hex[:24]}", "name": name, "input": arguments}


def _position_inside_markdown_fence(
    text: str,
    index: int,
    invoke_ranges: list[tuple[int, int]],
) -> bool:
    open_fence: tuple[str, int] | None = None
    line_start = 0
    for line in text[:index].split("\n"):
        line_end = line_start + len(line)
        inside_invoke = any(start <= line_start and line_end <= end for start, end in invoke_ranges)
        line_start = line_end + 1
        if inside_invoke:
            continue
        match = _FENCE_PATTERN.match(line)
        if match is None:
            continue
        delimiter = "`" if match.group(2) else "~"
        length = len(match.group(1))
        if open_fence is None:
            open_fence = (delimiter, length)
        elif (
            open_fence[0] == delimiter
            and length >= open_fence[1]
            and not line[match.end() :].strip()
        ):
            open_fence = None
    return open_fence is not None


def _position_inside_markdown_quote(text: str, index: int) -> bool:
    """Return whether the invoke starts on a Markdown blockquote line."""
    line_start = text.rfind("\n", 0, index) + 1
    return re.match(r" {0,3}>", text[line_start:index]) is not None


def _has_unmatched_invoke_start(text: str, matched_ranges: list[tuple[int, int]]) -> bool:
    for match in re.finditer(r"<invoke(?:\s|>)", text):
        if not any(start <= match.start() < end for start, end in matched_ranges):
            return True
    return False


def _complete_function_calls_wrapper_ranges(text: str) -> list[tuple[int, int]] | None:
    hints = list(_FUNCTION_CALLS_HINT_PATTERN.finditer(text))
    tokens = list(_FUNCTION_CALLS_TOKEN_PATTERN.finditer(text))
    if not hints:
        return []
    if len(hints) != len(tokens) or any(hint.start() != token.start() for hint, token in zip(hints, tokens)):
        return None
    ranges: list[tuple[int, int]] = []
    opening: int | None = None
    for token in tokens:
        if token.group(0).startswith("</"):
            if opening is None:
                return None
            ranges.append((opening, token.end()))
            opening = None
        else:
            if opening is not None:
                return None
            opening = token.start()
    return ranges if opening is None else None


def _remove_ranges(
    text: str,
    ranges: list[tuple[int, int]],
    *,
    start: int = 0,
    end: int | None = None,
) -> str:
    limit = len(text) if end is None else end
    cursor = start
    chunks: list[str] = []
    for range_start, range_end in sorted(ranges):
        if range_end <= start or range_start >= limit:
            continue
        clipped_start = max(range_start, start)
        clipped_end = min(range_end, limit)
        chunks.append(text[cursor:clipped_start])
        cursor = clipped_end
    chunks.append(text[cursor:limit])
    return "".join(chunks)


def _trim_one_boundary_newline(value: str) -> str:
    if value.startswith("\r\n"):
        value = value[2:]
    elif value.startswith("\n"):
        value = value[1:]
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return value


_MISSING = object()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _is_complete_argument_object(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return isinstance(json.loads(value), dict)
    except ValueError:
        return False
