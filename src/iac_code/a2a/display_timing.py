"""Content-bearing A2A events consumed by the ROS conversation UI.

This allowlist observes delivery, not browser painting. Protocol lifecycle events,
input acknowledgements and empty placeholders are intentionally absent. Keep the
rules aligned with ROS's normalSSE projector, AIBuy service and flow reducer.
"""

from __future__ import annotations

import json
import re
from typing import Any

from iac_code.services.request_progress import is_progress_metadata

_INTERNAL_TOOLS = frozenset({"create_plan", "finish_plan", "update_subtask_state", "finish_subtask"})
_TOOL_STATES = frozenset({"started", "input_delta", "input_complete", "completed", "failed"})
_CANCELED = frozenset({"TASK_STATE_CANCELED", "canceled", "cancelled"})
_DSML_MARKER = "<|DSML|"
_DSML_OPEN = re.compile(r"^<\|DSML\|([A-Za-z0-9_:-]{1,128})(?:\s[^>]*)?>")
_DSML_CLOSE = re.compile(r"</\|DSML\|[A-Za-z0-9_:-]+>")


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _permission(value: dict[str, Any]) -> bool:
    return (_text(value.get("inputId")) or _text(value.get("permissionId"))) and (
        value.get("required") is True or value.get("pending") is True or _text(value.get("inputId"))
    )


def _input(value: dict[str, Any], owner: dict[str, Any]) -> str | None:
    kind = value.get("kind")
    if kind is not None and not isinstance(kind, str):
        return None
    if value.get("accepted") is True or kind in {"permission_ack", "cloud_resource_selection_ack"}:
        return None
    if kind == "permission":
        return "permission" if _permission(value) else None
    if kind == "cloud_resource_selection":
        selector = _record(value.get("selector"))
        valid = all(_text(value.get(key)) for key in ("inputId", "toolUseId"))
        valid = valid and all(_text(selector.get(key)) for key in ("id", "associationProperty", "outputKind"))
        valid = valid and all(_text(owner.get(key)) for key in ("taskId", "contextId"))
        return "resource_selection" if valid else None
    has_prompt = any(_text(value.get(key)) for key in ("question", "prompt"))
    options = value.get("options")
    has_options = isinstance(options, list) and any(
        any(_text(_record(option).get(key)) for key in ("label", "name", "summary")) for option in options[:1024]
    )
    if kind == "ask_user_question":
        return "question" if has_prompt or has_options else None
    if kind in {None, "candidate_selection"}:
        return "candidate_selection" if has_options else None
    if kind == "deployment_confirmation" and any(
        value.get(key)
        for key in (
            "candidate",
            "selectedCandidate",
            "selected_candidate",
            "summary",
            "description",
            "prompt",
            "templateContent",
            "template_content",
            "pricing",
            "quote",
            "parameters",
            "parameterGroups",
            "parameter_groups",
            "deployment_parameters",
            "solution_summary",
            "template_url",
            "cost",
            "effective_deployment_parameters",
            "parameter_overrides",
        )
    ):
        return "deployment_confirmation"
    return None


class _VisibleText:
    """Discard split DSML protocol blocks without retaining their bodies."""

    def __init__(self) -> None:
        self.pending = ""
        self.closing = ""
        self.truncated = False

    @staticmethod
    def _suffix(text: str, marker: str) -> str:
        for length in range(min(len(text), len(marker) - 1), 0, -1):
            if text.endswith(marker[:length]):
                return text[-length:]
        return ""

    def feed(self, text: str) -> bool:
        self.pending += text
        visible = ""
        operations = 0
        while self.pending:
            operations += 1
            if operations > 512:
                self.truncated = True
                self.pending = ""
                break
            if self.closing:
                index = self.pending.find(self.closing)
                if index < 0:
                    self.pending = self._suffix(self.pending, self.closing)
                    break
                self.pending = self.pending[index + len(self.closing) :]
                self.closing = ""
            else:
                index = self.pending.find(_DSML_MARKER)
                if index < 0:
                    suffix = self._suffix(self.pending, _DSML_MARKER)
                    visible += self.pending[: len(self.pending) - len(suffix)]
                    self.pending = suffix
                    break
                visible += self.pending[:index]
                self.pending = self.pending[index:]
                opening = _DSML_OPEN.match(self.pending)
                if opening is None:
                    # An unclosed opening tag is also hidden; bound its retained prefix.
                    self.truncated = self.truncated or len(self.pending) > 512
                    self.pending = self.pending[:512]
                    break
                self.closing = f"</|DSML|{opening[1]}>"
                self.pending = self.pending[opening.end() :]
        return _text(_DSML_CLOSE.sub("", visible))


class DisplayEventClassifier:
    def __init__(self) -> None:
        self._pipeline_text: dict[tuple[str, ...], _VisibleText] = {}
        self.incomplete = False

    def _pipeline(self, event: dict[str, Any]) -> str | None:
        kind = event.get("eventType")
        if not isinstance(kind, str):
            return None
        data = _record(event.get("data"))
        if kind in {"text_delta", "thinking_delta"}:
            text = data.get("text", data.get("delta"))
            if not _text(text) or data.get("is_metadata_only", data.get("isMetadataOnly", False)):
                return None
            key = tuple(str(event.get(key, ""))[:256] for key in ("pipelineRunId",)) + (
                str(
                    _record(event.get("candidateStep")).get("runId")
                    or _record(event.get("candidate")).get("runId")
                    or ""
                )[:256],
            )
            if key not in self._pipeline_text and len(self._pipeline_text) >= 512:
                self.incomplete = True
                return None
            stream = self._pipeline_text.setdefault(key, _VisibleText())
            visible = stream.feed(text)
            self.incomplete = self.incomplete or stream.truncated
            if visible:
                return "text" if kind == "text_delta" else "thinking"
        elif kind == "input_required":
            return _input({**data, **_record(event.get("input"))}, event)
        elif kind == "permission_requested":
            permission = _record(event.get("permission")) or _record(data.get("permission")) or data
            return "permission" if _permission(permission) else None
        elif kind == "candidate_detail_shown":
            detail = _record(data.get("detail"))
            if any(detail.get(key) for key in ("candidateName", "summary", "costItems", "totalMonthlyCost")):
                return "candidate_detail"
        elif kind == "diagram_shown":
            if any(_text(data.get(key)) for key in ("mermaidSource", "templateContent")):
                return "diagram"
        elif kind in {"stack_current_changed", "stack_progress"} and _text(data.get("stackId")):
            return "deployment_result"
        elif kind == "tool_result":
            if data.get("isError") is True:
                return "tool_error"
            if data.get("toolName") == "ros_stack":
                result = data.get("result")
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (ValueError, RecursionError):
                        result = None
                if "progress_percentage" in _record(result):
                    return "deployment_result"
        elif kind in {"candidate_step_completed", "candidate_completed"}:
            conclusion = _record(data.get("conclusion" if kind == "candidate_step_completed" else "conclusions"))
            step_id = _record(event.get("candidateStep")).get("id", data.get("candidateStepId"))
            if kind == "candidate_step_completed" and step_id not in ("template_generating", "cost_estimating"):
                return None
            if any(conclusion.get(key) for key in ("template", "cost", "monthly_estimate", "deployment_parameters")):
                return "candidate_detail"
        elif kind == "step_completed":
            conclusion = _record(data.get("conclusion"))
            candidates = conclusion.get("candidates")
            if isinstance(candidates, list) and candidates:
                return "candidate_detail"
            if _text(conclusion.get("summary")):
                return "step_result"
            if _record(event.get("step")).get("id") == "deploying" and any(
                conclusion.get(key)
                for key in ("message", "error", "resources_created", "outputs", "stack_id", "status")
            ):
                return "deployment_result"
        return None

    def classify(self, value: Any, *, budget: int = 16, status_message: bool = False) -> str | None:
        if not isinstance(value, dict) or budget <= 0:
            return None
        # Initial/replayed Task snapshots and artifacts are not new conversation content.
        kind = value.get("kind")
        if (
            "task" in value
            or isinstance(kind, str)
            and kind in {"task", "artifact-update"}
            or "id" in value
            and "status" in value
        ):
            return None
        for key in ("result", "statusUpdate", "status_update"):
            if isinstance(value.get(key), dict):
                return self.classify(value[key], budget=budget - 1)
        if value.get("object") == "ros.client_context.status":
            return None
        error = _record(value.get("error"))
        if error and (error.get("code") is not None or _text(error.get("message"))):
            return "error"
        iac = _record(_record(value.get("metadata")).get("iac_code"))
        if is_progress_metadata(iac):
            return None
        batch = _record(iac.get("pipelineBatch")).get("events")
        if isinstance(batch, list):
            self.incomplete = self.incomplete or len(batch) > 1024
            for event in batch[:1024]:
                if kind := self._pipeline(_record(event)):
                    return kind
            return None
        if isinstance(iac.get("pipeline"), dict):
            return self._pipeline(iac["pipeline"])
        if "eventType" in value:
            return self._pipeline(value)
        input_value = _record(iac.get("input"))
        if input_value.get("kind") in ("permission", "cloud_resource_selection"):
            if kind := _input(input_value, value):
                return kind
        if iac.get("inputReceived") or iac.get("mcpWarning"):
            return None
        status = _record(value.get("status"))
        state = status.get("state")
        if isinstance(state, str) and state in _CANCELED:
            return None
        thinking = _record(iac.get("thinking"))
        if _text(thinking.get("text")) and not thinking.get("is_metadata_only", thinking.get("isMetadataOnly", False)):
            return "thinking"
        tool = _record(iac.get("tool"))
        tool_state = tool.get("status")
        if (
            isinstance(tool_state, str)
            and tool_state in _TOOL_STATES
            and _text(tool.get("toolUseId"))
            and _text(tool.get("name"))
        ):
            return "tool" if tool["name"] not in _INTERNAL_TOOLS else None
        if status:
            return self.classify(status, budget=budget - 1, status_message=True)
        if isinstance(value.get("message"), dict):
            message = value["message"]
            message_id = message.get("messageId", message.get("message_id", ""))
            state = value.get("state")
            if status_message and not (
                isinstance(message_id, str)
                and message_id.endswith("-message")
                or isinstance(state, str)
                and state in {"TASK_STATE_COMPLETED", "completed", "TASK_STATE_FAILED", "failed"}
            ):
                return None
            return self.classify(message, budget=budget - 1)
        parts = value.get("parts")
        role = value.get("role")
        if not isinstance(role, str) or role not in {"agent", "ROLE_AGENT"} or not isinstance(parts, list):
            return None
        if any(
            _record(_record(part).get("data")).get("kind") in ("permission_ack", "cloud_resource_selection_ack")
            for part in parts
        ):
            return None
        return "text" if any(_text(_record(part).get("text")) for part in parts) else None
