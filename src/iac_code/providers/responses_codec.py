"""Responses items and SSE conversion for the existing provider contract."""

from __future__ import annotations

import copy
import json
from typing import Any

from iac_code.providers.base import Message, NonStreamingResponse, ToolDefinition
from iac_code.types.stream_events import (
    StreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

RESPONSES_METADATA_KEY = "responses"
_OUTPUT_TYPES = {"message", "reasoning", "function_call"}


class ResponsesProtocolError(RuntimeError):
    """A response cannot be represented by the local agent contract."""


class ResponsesContextLimitError(RuntimeError):
    """Ask the owning agent to compact local history before one retry."""

    context_limit_exceeded = True


def plain(value: Any) -> Any:
    """Keep SDK extension fields (e.g. phase) when persisting output items."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return plain(vars(value))
    return value


def _call(item: dict[str, Any], names: set[str] | None = None) -> dict[str, Any]:
    call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise ResponsesProtocolError("Responses function call is missing its call_id or name.")
    if names is not None and name not in names:
        raise ResponsesProtocolError("Responses returned an unknown local function.")
    if item.get("status") not in {None, "completed"}:
        raise ResponsesProtocolError("Responses returned an unfinished function call.")
    if not isinstance(arguments, str):
        raise ResponsesProtocolError("Responses function arguments are not complete JSON.")
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError) as exc:
        raise ResponsesProtocolError("Responses function arguments are not complete JSON.") from exc
    if not isinstance(parsed, dict):
        raise ResponsesProtocolError("Responses function arguments must be a JSON object.")
    return {"id": call_id, "name": name, "input": parsed}


def _pair_functions(items: list[dict[str, Any]], *, adjacent: bool) -> list[dict[str, Any]]:
    """Validate tool linkage; Bailian needs each result immediately after its call."""
    calls: dict[str, int] = {}
    results: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        if item.get("type") == "function_call":
            call_id = _call(item)["id"]
            if call_id in calls:
                raise ResponsesProtocolError("Responses history contains duplicate call IDs.")
            calls[call_id] = index
        elif item.get("type") == "function_call_output":
            call_id = item.get("call_id")
            if call_id not in calls or call_id in results:
                raise ResponsesProtocolError("Responses tool result has no unique preceding call.")
            results[call_id] = item
    if calls.keys() != results.keys():
        raise ResponsesProtocolError("Responses history contains a function call without its result.")
    if not adjacent:
        return items
    paired: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") == "function_call_output":
            continue
        paired.append(item)
        if item.get("type") == "function_call":
            paired.append(results[item["call_id"]])
    return paired


def encode_input(messages: list[Message], identity: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        native = message.metadata.get(RESPONSES_METADATA_KEY)
        if (
            message.role == "assistant"
            and isinstance(native, dict)
            and all(native.get(key) == value for key, value in identity.items())
            and isinstance(native.get("output"), list)
        ):
            for item in native["output"]:
                if not isinstance(item, dict) or item.get("type") not in _OUTPUT_TYPES:
                    raise ResponsesProtocolError("Responses history contains an unsupported output item.")
                items.append(copy.deepcopy(item))
            continue
        if isinstance(message.content, str):
            items.append({"role": message.role, "content": message.content})
            continue
        content: list[dict[str, Any]] = []

        def flush() -> None:
            if content:
                # EasyInputMessage accepts assistant text strings on both profiles;
                # Bailian reserves input_text content blocks for user messages.
                value = "".join(part["text"] for part in content) if message.role == "assistant" else list(content)
                items.append({"role": message.role, "content": value})
                content.clear()

        for block in message.content:
            if block.type == "text":
                content.append({"type": "input_text", "text": block.text or ""})
            elif block.type == "image":
                if message.role != "user" or not block.media_type or not block.data:
                    raise ResponsesProtocolError("Responses image input requires a user image data URL.")
                content.append({"type": "input_image", "image_url": f"data:{block.media_type};base64,{block.data}"})
            elif block.type == "tool_use":
                flush()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.tool_use_id,
                        "name": block.name,
                        "arguments": json.dumps(block.input or {}, ensure_ascii=False),
                    }
                )
            elif block.type == "tool_result":
                flush()
                items.append(
                    {"type": "function_call_output", "call_id": block.tool_use_id, "output": block.content or ""}
                )
            elif block.type not in {"thinking", "redacted_thinking"}:
                raise ResponsesProtocolError("Responses received an unsupported input content block.")
        flush()
    return _pair_functions(items, adjacent=identity["profile"] == "dashscope")


def encode_tools(tools: list[ToolDefinition] | None, *, strict: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools or []:
        item: dict[str, Any] = {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": copy.deepcopy(tool.input_schema),
        }
        if strict:
            item["strict"] = False
        result.append(item)
    return result


def decode_response(
    response: Any, identity: dict[str, Any], tools: list[ToolDefinition] | None
) -> NonStreamingResponse:
    data = plain(response)
    if not isinstance(data, dict) or not isinstance(data.get("output"), list) or not data.get("id"):
        raise ResponsesProtocolError("Responses returned an invalid response.")
    status = data.get("status")
    incomplete = status == "incomplete" and (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
    if status != "completed" and not incomplete:
        raise ResponsesProtocolError("Responses did not complete successfully.")
    names = {tool.name for tool in tools or []}
    text: list[str] = []
    thinking: list[str] = []
    calls: list[dict[str, Any]] = []
    refusal = False
    for item in data["output"]:
        if not isinstance(item, dict) or item.get("type") not in _OUTPUT_TYPES:
            raise ResponsesProtocolError("Responses returned an unsupported output item type.")
        if item["type"] == "function_call":
            calls.append(_call(item, names))
        elif item["type"] == "reasoning":
            thinking.extend(part.get("text", "") for part in item.get("summary", []) if isinstance(part, dict))
        else:
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    refusal = True
                    text.append(part.get("refusal", ""))
                else:
                    raise ResponsesProtocolError("Responses returned unsupported message content.")
    if len({call["id"] for call in calls}) != len(calls):
        raise ResponsesProtocolError("Responses returned duplicate function call IDs.")
    if incomplete and calls:
        # Only text-only truncation can be committed without native replay items.
        raise ResponsesProtocolError("Responses returned an incomplete tool turn.")
    if not data["output"] or (status == "completed" and not text and not calls and not refusal):
        raise ResponsesProtocolError("Responses returned no assistant content or function calls.")
    usage = data.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    metadata = {} if incomplete or refusal else {RESPONSES_METADATA_KEY: {**identity, "output": data["output"]}}
    return NonStreamingResponse(
        message_id=data["id"],
        text="".join(text),
        thinking="".join(thinking),
        tool_uses=[] if refusal else calls,
        stop_reason="refusal" if refusal else "max_tokens" if incomplete else "tool_use" if calls else "end_turn",
        usage=Usage(
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
            cache_read_input_tokens=details.get("cached_tokens", 0) or 0,
            reported=data.get("usage") is not None,
        ),
        provider_metadata=metadata,
    )


class ResponsesStreamAdapter:
    """Request-local item assembly, validated before the terminal event."""

    def __init__(self, identity: dict[str, Any], tools: list[ToolDefinition] | None) -> None:
        self.identity = identity
        self.tools = tools
        self.items: dict[int, dict[str, Any]] = {}
        self.started: set[str] = set()
        self.ended: dict[str, dict[str, Any]] = {}
        self.text = ""
        self.thinking = ""
        self.terminal: NonStreamingResponse | None = None

    def _finish_call(self, item: dict[str, Any]) -> list[StreamEvent]:
        call = _call(item, {tool.name for tool in self.tools or []})
        call_id = call["id"]
        if call_id in self.ended:
            if self.ended[call_id] != call:
                raise ResponsesProtocolError("Responses changed a completed function call.")
            return []
        events: list[StreamEvent] = []
        if call_id not in self.started:
            self.started.add(call_id)
            events.append(ToolUseStartEvent(tool_use_id=call_id, name=call["name"]))
        self.ended[call_id] = call
        events.append(ToolUseEndEvent(tool_use_id=call_id, name=call["name"], input=call["input"]))
        return events

    def feed(self, event: Any) -> list[StreamEvent]:
        data = plain(event)
        kind = data.get("type", "")
        if self.terminal is not None:
            raise ResponsesProtocolError("Responses sent events after its terminal response.")
        if kind in {"response.completed", "response.incomplete"}:
            response = decode_response(data.get("response"), self.identity, self.tools)
            output = plain(data["response"])["output"]
            for index, previous in self.items.items():
                if index < 0 or index >= len(output):
                    raise ResponsesProtocolError("Responses terminal response dropped a streamed item.")
                final = output[index]
                if previous.get("type") != final.get("type") or previous.get("id") != final.get("id"):
                    raise ResponsesProtocolError("Responses terminal item identity differs from its stream.")
                if previous.get("type") == "function_call":
                    if previous.get("call_id") != final.get("call_id") or previous.get("name") != final.get("name"):
                        raise ResponsesProtocolError("Responses changed a function identity.")
                    if not final.get("arguments", "").startswith(previous.get("arguments") or ""):
                        raise ResponsesProtocolError("Responses final function arguments differ from their stream.")
            events: list[StreamEvent] = []
            if not response.text.startswith(self.text) or not response.thinking.startswith(self.thinking):
                raise ResponsesProtocolError("Responses terminal content differs from its stream.")
            if response.text[len(self.text) :]:
                events.append(TextDeltaEvent(text=response.text[len(self.text) :]))
            if response.thinking[len(self.thinking) :]:
                events.append(ThinkingDeltaEvent(text=response.thinking[len(self.thinking) :]))
            if response.stop_reason != "refusal":
                if not self.started.issubset({call["id"] for call in response.tool_uses}):
                    raise ResponsesProtocolError("Responses terminal response dropped a streamed function call.")
                for item in output:
                    if item.get("type") == "function_call":
                        events.extend(self._finish_call(item))
            self.terminal = response
            return events
        if kind in {"response.failed", "error"}:
            error = (data.get("response") or {}).get("error") if kind == "response.failed" else data
            if isinstance(error, dict) and error.get("code") in {
                "context_length_exceeded",
                "context_window_exceeded",
                "max_input_tokens",
            }:
                raise ResponsesContextLimitError("Responses input exceeds the model context limit.")
            raise ResponsesProtocolError("Responses generation failed.")
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            delta = data.get("delta") or ""
            self.text += delta
            return [TextDeltaEvent(text=delta)] if delta else []
        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            delta = data.get("delta") or ""
            self.thinking += delta
            return [ThinkingDeltaEvent(text=delta)] if delta else []
        if kind == "response.output_item.added":
            index, item = data.get("output_index"), data.get("item")
            if not isinstance(index, int) or index in self.items or not isinstance(item, dict):
                raise ResponsesProtocolError("Responses stream contains an invalid or duplicate item index.")
            if item.get("type") not in _OUTPUT_TYPES:
                raise ResponsesProtocolError("Responses returned an unsupported output item type.")
            self.items[index] = item
            if item["type"] == "function_call":
                call_id, name = item.get("call_id"), item.get("name")
                if not call_id or not name or name not in {tool.name for tool in self.tools or []}:
                    raise ResponsesProtocolError("Responses stream contains an invalid local function call.")
                if call_id in self.started:
                    raise ResponsesProtocolError("Responses stream contains duplicate function call IDs.")
                self.started.add(call_id)
                return [ToolUseStartEvent(tool_use_id=call_id, name=name)]
        if kind.startswith("response.function_call_arguments."):
            item = self.items.get(data.get("output_index"))
            if item is None or item.get("type") != "function_call" or item.get("id") != data.get("item_id"):
                raise ResponsesProtocolError("Responses arguments do not match a preceding function item.")
            if kind.endswith(".delta"):
                delta = data.get("delta") or ""
                if item["call_id"] in self.ended:
                    raise ResponsesProtocolError("Responses arguments changed after function completion.")
                item["arguments"] = (item.get("arguments") or "") + delta
                return [ToolInputDeltaEvent(tool_use_id=item["call_id"], partial_json=delta)]
            if kind.endswith(".done"):
                arguments = data.get("arguments")
                if item.get("arguments") and item["arguments"] != arguments:
                    raise ResponsesProtocolError("Responses final function arguments differ from their stream.")
                item["arguments"] = arguments
                return self._finish_call({**item, "status": "completed"})
        if kind == "response.output_item.done":
            item = data.get("item") or {}
            previous = self.items.get(data.get("output_index"))
            if previous is None or previous.get("id") != item.get("id") or previous.get("type") != item.get("type"):
                raise ResponsesProtocolError("Responses completed an unknown output item.")
            if item.get("type") == "function_call":
                if previous.get("call_id") != item.get("call_id") or previous.get("name") != item.get("name"):
                    raise ResponsesProtocolError("Responses changed a function identity.")
                if previous.get("arguments") and previous["arguments"] != item.get("arguments"):
                    raise ResponsesProtocolError("Responses final function arguments differ from their stream.")
                return self._finish_call(item)
        return []
