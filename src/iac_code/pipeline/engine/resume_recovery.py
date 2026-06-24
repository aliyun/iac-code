"""Helpers for reconciling durable transcript recovery with sidecar resume state."""

from __future__ import annotations

import json

from iac_code.agent.message import ContentBlock, Message, ToolResultBlock


def _message_key(message: Message) -> str:
    return json.dumps(message.to_dict(), ensure_ascii=False, sort_keys=True)


def _tool_result_ids(message: Message) -> set[str]:
    if not isinstance(message.content, list):
        return set()
    return {block.tool_use_id for block in message.content if isinstance(block, ToolResultBlock) and block.tool_use_id}


def _without_seen_tool_results(message: Message, seen_tool_result_ids: set[str]) -> Message | None:
    if not isinstance(message.content, list):
        return message
    content = [
        block
        for block in message.content
        if not isinstance(block, ToolResultBlock) or block.tool_use_id not in seen_tool_result_ids
    ]
    if not content:
        return None
    if len(content) == len(message.content):
        return message
    return message.model_copy(update={"content": content})


def reconcile_resume_messages(
    transcript_messages: list[Message] | None,
    sidecar_messages: list[Message] | None,
) -> list[Message] | None:
    """Merge sidecar resume messages into repaired transcript messages without duplicating tool results."""
    merged = list(transcript_messages or [])
    if not sidecar_messages:
        return merged or None
    if not merged:
        return list(sidecar_messages)

    seen_keys = {_message_key(message) for message in merged}
    seen_tool_result_ids: set[str] = set()
    for message in merged:
        seen_tool_result_ids.update(_tool_result_ids(message))

    for message in sidecar_messages:
        key = _message_key(message)
        if key in seen_keys:
            continue
        filtered = _without_seen_tool_results(message, seen_tool_result_ids)
        if filtered is None:
            continue
        merged.append(filtered)
        seen_keys.add(_message_key(filtered))
        seen_tool_result_ids.update(_tool_result_ids(filtered))
    return merged or None


def user_message_already_in_resume(
    user_message: str | list[ContentBlock] | None,
    resume_messages: list[Message] | None,
) -> bool:
    if user_message is None or not resume_messages:
        return False
    candidate = Message(role="user", content=user_message)
    candidate_key = _message_key(candidate)
    return any(_message_key(message) == candidate_key for message in resume_messages)
