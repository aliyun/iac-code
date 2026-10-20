"""Project completed Pipeline inputs into the handoff without deleting public history."""

from __future__ import annotations

import json

from iac_code.agent.message import Message
from iac_code.services.session_storage import PIPELINE_USER_HISTORY_KEY

_HANDOFF_METADATA_TYPE = "pipeline_handoff_context"


def _pipeline_inputs(messages: list[Message]) -> list[Message]:
    if not isinstance(messages, list):
        return []
    return [
        message
        for message in messages
        if isinstance(message, Message)
        and message.role == "user"
        and isinstance(message.content, str)
        and message.content.strip()
        and isinstance(message.metadata.get(PIPELINE_USER_HISTORY_KEY), dict)
        and isinstance(message.metadata[PIPELINE_USER_HISTORY_KEY].get("message_id"), str)
        and message.metadata[PIPELINE_USER_HISTORY_KEY]["message_id"]
    ]


def _included_context(summary: str) -> tuple[str, dict, str] | None:
    marker = "Included context:\n"
    if not summary.startswith("[Pipeline Handoff Context]\n") or marker not in summary:
        return None
    prefix, tail = summary.split(marker, 1)
    try:
        included, end = json.JSONDecoder().raw_decode(tail)
    except ValueError:
        return None
    if not isinstance(included, dict):
        return None
    return prefix + marker, included, tail[end:]


def include_original_user_request(summary: str, messages: list[Message]) -> str:
    """Add the full accepted inputs; preserve every byte outside the context JSON."""
    inputs = _pipeline_inputs(messages)
    context = _included_context(summary)
    if not inputs or context is None:
        return summary
    prefix, included, suffix = context
    included["original_user_request"] = "\n\n".join(str(message.content) for message in inputs)
    return prefix + json.dumps(included, ensure_ascii=False, indent=2) + suffix


def pipeline_handoff_message(summary: str, messages: list[Message]) -> Message:
    enriched = include_original_user_request(summary, messages)
    inputs = _pipeline_inputs(messages)
    metadata = {}
    # Only a server-generated, structured handoff can cover public inputs.
    if inputs and _included_context(enriched) is not None:
        metadata = {
            "type": _HANDOFF_METADATA_TYPE,
            "pipeline_user_input_ids": [
                message.metadata[PIPELINE_USER_HISTORY_KEY]["message_id"] for message in inputs
            ],
        }
    return Message(role="user", content=enriched, metadata=metadata)


def fold_pipeline_user_history(messages: list[Message]) -> list[Message]:
    """Hide only earlier inputs explicitly covered by a trusted completion summary."""
    if not any(message.metadata.get("type") == _HANDOFF_METADATA_TYPE for message in messages):
        return messages
    covered: set[str] = set()
    visible: list[Message] = []
    for message in reversed(messages):
        if message.role == "user" and message.metadata.get("type") == _HANDOFF_METADATA_TYPE:
            ids = message.metadata.get("pipeline_user_input_ids", [])
            if isinstance(ids, list):
                covered.update(value for value in ids if isinstance(value, str))
        identity = message.metadata.get(PIPELINE_USER_HISTORY_KEY)
        message_id = identity.get("message_id") if isinstance(identity, dict) else None
        if message.role == "user" and isinstance(message_id, str) and message_id in covered:
            continue
        visible.append(message)
    return list(reversed(visible))
