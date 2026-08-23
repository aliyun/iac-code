"""Idempotent read-only tool result cache for candidate sub-pipelines.

Candidate sub-pipelines (``evaluate_candidates``) spend most of their time on
read-only lookups such as ROS resource type schemas (``aliyun_api``) and
reference files (``read_file``). Sidecar checkpoints are only written at
sub-step boundaries, so an interrupt in the middle of a sub-step used to
discard every lookup done so far and force the resumed run to repeat them.

This module keys successful read-only tool results by
``(candidate_index, sub_step_id, tool_name, normalized_input)`` so a resumed run
can replay them as ``precompleted_tools`` instead of calling the tool again.
Only read-only tools are cached — anything with side effects must re-run.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

CACHEABLE_TOOL_NAMES = frozenset(
    {
        "aliyun_api",
        "aliyun_api_doc",
        "aliyun_doc_search",
        "read_file",
        "grep",
        "glob",
        "list_files",
        "ros_get_template_parameter_constraints",
        "ros_estimate_template_cost",
        "ros_preview_template",
    }
)


def _canonical_input(tool_input: Any) -> Any:
    if isinstance(tool_input, dict):
        return {str(key): _canonical_input(tool_input[key]) for key in sorted(tool_input, key=str)}
    if isinstance(tool_input, list):
        return [_canonical_input(item) for item in tool_input]
    return tool_input


def input_fingerprint(tool_input: dict[str, Any] | None) -> str:
    """Return a stable digest for a tool invocation's parameters."""
    canonical = json.dumps(_canonical_input(tool_input or {}), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_cacheable_tool(tool_name: str, tool_input: dict[str, Any] | None = None) -> bool:
    """Whether a tool result may be replayed on resume without re-calling it."""
    if tool_name not in CACHEABLE_TOOL_NAMES:
        return False
    if tool_name == "aliyun_api":
        # Only read-only Aliyun actions are safe to replay; writes must re-run.
        return _is_read_only_aliyun_action(tool_input)
    return True


def _is_read_only_aliyun_action(tool_input: dict[str, Any] | None) -> bool:
    action = (tool_input or {}).get("action")
    if not isinstance(action, str) or not action:
        return False
    return action.startswith(("Describe", "Get", "List", "Query", "Check", "Preview", "Validate"))


class CandidateToolResultCache:
    """Per-candidate cache of replayable read-only tool results."""

    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self._entries: dict[str, dict[str, Any]] = dict(entries or {})

    @staticmethod
    def _entry_key(candidate_index: int, sub_step_id: str, tool_name: str, fingerprint: str) -> str:
        return f"{candidate_index}|{sub_step_id}|{tool_name}|{fingerprint}"

    def record(
        self,
        *,
        candidate_index: int,
        sub_step_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None,
        result: Any,
    ) -> bool:
        """Cache one successful read-only tool result. Returns whether it was stored."""
        if not is_cacheable_tool(tool_name, tool_input):
            return False
        if not isinstance(result, dict):
            return False
        fingerprint = input_fingerprint(tool_input)
        key = self._entry_key(candidate_index, sub_step_id, tool_name, fingerprint)
        self._entries[key] = {
            "candidate_index": candidate_index,
            "sub_step_id": sub_step_id,
            "tool_name": tool_name,
            "fingerprint": fingerprint,
            "result": result,
        }
        return True

    def precompleted_tools_for(self, candidate_index: int, sub_step_id: str) -> dict[str, dict[str, Any]]:
        """Return cached results for one candidate sub-step, keyed by tool name.

        ``StepExecutor`` consumes this as ``precompleted_tools`` so completion
        guards accept the already-known results and the agent does not need to
        repeat the lookups.
        """
        precompleted: dict[str, dict[str, Any]] = {}
        for entry in self._entries.values():
            if entry.get("candidate_index") != candidate_index or entry.get("sub_step_id") != sub_step_id:
                continue
            tool_name = entry.get("tool_name")
            result = entry.get("result")
            if isinstance(tool_name, str) and isinstance(result, dict):
                precompleted[tool_name] = result
        return precompleted

    def cached_result_count(self, candidate_index: int | None = None) -> int:
        if candidate_index is None:
            return len(self._entries)
        return sum(1 for entry in self._entries.values() if entry.get("candidate_index") == candidate_index)

    def to_snapshot(self) -> dict[str, dict[str, Any]]:
        return {key: dict(entry) for key, entry in self._entries.items()}

    @classmethod
    def from_snapshot(cls, snapshot: Any) -> CandidateToolResultCache:
        if not isinstance(snapshot, dict):
            return cls()
        entries: dict[str, dict[str, Any]] = {}
        for key, entry in snapshot.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            tool_name = entry.get("tool_name")
            result = entry.get("result")
            candidate_index = entry.get("candidate_index")
            sub_step_id = entry.get("sub_step_id")
            if (
                not isinstance(tool_name, str)
                or not isinstance(result, dict)
                or not isinstance(sub_step_id, str)
                or not isinstance(candidate_index, int)
                or isinstance(candidate_index, bool)
            ):
                continue
            entries[key] = {
                "candidate_index": candidate_index,
                "sub_step_id": sub_step_id,
                "tool_name": tool_name,
                "fingerprint": str(entry.get("fingerprint") or ""),
                "result": result,
            }
        return cls(entries)
