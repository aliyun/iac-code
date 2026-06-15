"""CompleteStepTool — model calls this to signal step completion."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import jsonschema

from iac_code.i18n import _
from iac_code.pipeline.display_names import display_step_name
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.utils.public_errors import sanitize_public_text

if TYPE_CHECKING:
    from iac_code.pipeline.engine.types import StepConfig

logger = logging.getLogger(__name__)

MAX_PARALLEL_CANDIDATES = 5
MAX_ROLLBACK_TARGETS = 5
_SENSITIVE_VALIDATION_FIELD_PATTERN = re.compile(
    r"(?i)(auth|authorization|cookie|credential|credentials|passphrase|password|passwd|private[_-]?key|pwd|secret|"
    r"session|signature|token|api[_-]?key|access[_-]?key)"
)


class CompleteStepTool(Tool):
    """Tool used by the step LLM to signal step completion and validate the conclusion.

    Lifecycle: a fresh instance is created at the start of each step. The
    ``_validation_attempts`` counter therefore resets per step —
    ``max_conclusion_retries`` is enforced *within a step*, not across the
    pipeline. If a step is re-entered (e.g. after a rollback), a new
    ``CompleteStepTool`` is constructed and the retry budget starts over.
    """

    def __init__(
        self,
        step_config: StepConfig,
        *,
        completion_guards: list[dict] | None = None,
        completion_guard_state: dict[str, Any] | None = None,
        user_message: str = "",
    ) -> None:
        self._step_config = step_config
        self._completion_guards = completion_guards or []
        self._completion_guard_state = completion_guard_state if completion_guard_state is not None else {}
        self._user_message = user_message or ""
        # P-I17: _validation_attempts resets each new step (see class docstring) —
        # max_conclusion_retries is a per-step budget, not a pipeline-wide one.
        self._validation_attempts = 0

    @property
    def name(self) -> str:
        return "complete_step"

    @property
    def description(self) -> str:
        return _(
            "Complete the current step by calling this tool to submit the conclusion. "
            "If you need to roll back to an earlier step, set rollback_request."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        conclusion_prop = self._build_conclusion_property()
        properties: dict[str, Any] = {"conclusion": conclusion_prop}
        required = ["conclusion"]

        if self._step_config.rollback_targets and len(self._step_config.rollback_targets) <= MAX_ROLLBACK_TARGETS:
            properties["rollback_request"] = {
                "type": "object",
                "description": _("Set this field when you need to roll back to an earlier step"),
                "properties": {
                    "target_step": {
                        "type": "string",
                        "enum": self._step_config.rollback_targets,
                        "description": _("Target step ID to roll back to"),
                    },
                    "reason": {"type": "string", "description": _("Reason for rollback")},
                },
                "required": ["target_step", "reason"],
                "additionalProperties": False,
            }

        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def _build_conclusion_property(self) -> dict[str, Any]:
        if self._step_config.conclusion_schema:
            return self._step_config.conclusion_schema
        return {
            "type": "object",
            "description": _("Structured conclusion for the current step. Required and non-empty."),
        }

    def normalize_input(self, tool_input: dict[str, Any]) -> None:
        """Normalize conclusion before input/schema validation."""
        conclusion = tool_input.get("conclusion")
        if isinstance(conclusion, dict):
            for key in [k for k, v in conclusion.items() if v is None]:
                del conclusion[key]
            self._copy_guard_tool_results_to_conclusion(conclusion)

    def _copy_guard_tool_results_to_conclusion(self, conclusion: dict[str, Any]) -> None:
        tool_results = self._completion_guard_state.get("tool_results", {})
        for guard in self._completion_guards:
            required_tool = guard.get("require_tool")
            if not required_tool:
                continue
            tool_result = tool_results.get(required_tool)
            mapping = guard.get("copy_tool_result_to_conclusion") or {}
            if not isinstance(tool_result, dict) or not isinstance(mapping, dict):
                continue
            for source_field, target_field in mapping.items():
                source_value = conclusion.get(source_field, tool_result.get(source_field))
                if source_value not in (None, "", [], {}) and self._resolve_dotted(conclusion, target_field) is None:
                    conclusion[target_field] = source_value
                if source_field != target_field and source_field in conclusion:
                    del conclusion[source_field]

    def validate_input(self, tool_input: dict[str, Any]) -> tuple[bool, str]:
        """Validate input and return a model-actionable schema hint on failure."""
        self.normalize_input(tool_input)
        rollback_target_error = self._validate_rollback_target_limit()
        if rollback_target_error is not None:
            return False, rollback_target_error
        try:
            jsonschema.validate(instance=tool_input, schema=self.input_schema)
            return True, ""
        except jsonschema.ValidationError as e:
            return False, self._format_input_validation_error(self._public_validation_error(e), tool_input)

    def _format_input_validation_error(self, error: str, tool_input: dict[str, Any]) -> str:
        invalid_json = sanitize_public_text(json.dumps(tool_input or {}, ensure_ascii=False))
        example = json.dumps(
            {"conclusion": self._example_from_schema(self._step_config.conclusion_schema)},
            ensure_ascii=False,
        )
        return _(
            "{error}\n"
            "Current step: {step_id}\n"
            "Do not repeat the previous invalid arguments: {invalid_json}\n"
            'complete_step arguments must be {{"conclusion": {{...}}}}; do not submit empty arguments '
            "or put conclusion fields at the top level.\n"
            "{schema_hint}\n"
            "Outer argument example: {example}"
        ).format(
            error=error,
            step_id=display_step_name(self._step_config.step_id),
            invalid_json=invalid_json,
            schema_hint=self._complete_step_schema_hint(),
            example=example,
        )

    @classmethod
    def _public_validation_error(cls, error: jsonschema.ValidationError) -> str:
        message = sanitize_public_text(error.message)
        if not any(_SENSITIVE_VALIDATION_FIELD_PATTERN.search(str(part)) for part in error.path):
            return message

        instance = error.instance
        replacements: set[str] = set()
        if isinstance(instance, str):
            replacements.add(repr(instance))
            replacements.add(json.dumps(instance, ensure_ascii=False))
        elif isinstance(instance, (int, float, bool)) or instance is None:
            replacements.add(repr(instance))
            replacements.add(json.dumps(instance, ensure_ascii=False))
        for value in replacements:
            message = message.replace(value, "[REDACTED]")
        return sanitize_public_text(message)

    def _complete_step_schema_hint(self) -> str:
        if not self._step_config.conclusion_schema:
            return _("conclusion must be a non-empty object; fill the structured conclusion required by this step.")
        compact = self._compact_schema(self._step_config.conclusion_schema)
        return _("conclusion must match this schema summary:\n") + json.dumps(compact, ensure_ascii=False)

    @classmethod
    def _compact_schema(cls, schema: Any, *, depth: int = 0) -> Any:
        if depth > 4 or not isinstance(schema, dict):
            return schema

        compact: dict[str, Any] = {}
        for key in ("type", "required", "enum", "description", "minItems"):
            if key in schema:
                compact[key] = schema[key]

        properties = schema.get("properties")
        if isinstance(properties, dict):
            compact["properties"] = {
                name: cls._compact_schema(value, depth=depth + 1) for name, value in properties.items()
            }

        items = schema.get("items")
        if isinstance(items, dict):
            compact["items"] = cls._compact_schema(items, depth=depth + 1)

        return compact or schema

    @classmethod
    def _example_from_schema(cls, schema: Any) -> Any:
        if not isinstance(schema, dict):
            return {"result": _("<fill according to the current step requirements>")}

        schema_type = schema.get("type")
        if schema_type == "object" or "properties" in schema:
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            keys = required or list(properties)[:3]
            if not keys:
                return {"result": _("<fill according to the current step requirements>")}
            return {str(key): cls._example_from_schema(properties.get(key)) for key in keys}
        if schema_type == "array":
            return [cls._example_from_schema(schema.get("items"))]
        if schema_type == "string":
            return "<string>"
        if schema_type == "integer":
            return 0
        if schema_type == "number":
            return 0
        if schema_type == "boolean":
            return True
        return "<value>"

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def _validate_conclusion(self, conclusion: dict) -> str | None:
        """Validate conclusion against schema. Returns error message or None."""
        schema = self._step_config.conclusion_schema
        if not schema:
            return None
        try:
            jsonschema.validate(conclusion, schema)
            return None
        except jsonschema.ValidationError as e:
            public_message = self._public_validation_error(e)
            logger.warning("Schema validation failed for step %s: %s", self._step_config.step_id, public_message)
            return public_message

    def _validate_completion_guards(self, conclusion: dict) -> str | None:
        for guard in self._completion_guards:
            if not self._guard_applies(guard, conclusion):
                continue

            required_tool = guard.get("require_tool")
            required_field = guard.get("required_conclusion_field")
            required_any_of = guard.get("required_conclusion_any_of") or []
            successful_tools = self._completion_guard_state.get("successful_tools", set())
            if required_tool and required_tool not in successful_tools:
                message = guard.get("message") or _("Clarification is required before completing the current step.")
                return _(
                    "{message} Call {required_tool} first, then call complete_step after receiving the tool result."
                ).format(
                    message=message,
                    required_tool=required_tool,
                )
            if required_field and self._resolve_dotted(conclusion, required_field) in (None, "", [], {}):
                message = guard.get("message") or _(
                    "Clarification output is required before completing the current step."
                )
                return _("{message} complete_step.conclusion must include {required_field}.").format(
                    message=message,
                    required_field=required_field,
                )
            if required_any_of and all(
                self._resolve_dotted(conclusion, field) in (None, "", [], {}) for field in required_any_of
            ):
                message = guard.get("message") or _(
                    "Clarification output is required before completing the current step."
                )
                fields = _(" or ").join(str(field) for field in required_any_of)
                return _("{message} complete_step.conclusion must include one of these fields: {fields}.").format(
                    message=message,
                    fields=fields,
                )
        return None

    def _guard_applies(self, guard: dict, conclusion: dict) -> bool:
        unless_patterns = guard.get("unless_user_message_matches_any") or []
        if any(self._matches(pattern, self._user_message) for pattern in unless_patterns):
            return False

        user_patterns = guard.get("when_user_message_matches_any") or []
        if user_patterns and any(self._matches(pattern, self._user_message) for pattern in user_patterns):
            return True

        conclusion_equals = guard.get("when_conclusion_field_equals") or {}
        return any(self._resolve_dotted(conclusion, field) == value for field, value in conclusion_equals.items())

    @staticmethod
    def _validate_candidate_limit(conclusion: dict) -> str | None:
        candidates = conclusion.get("candidates")
        if not isinstance(candidates, list) or len(candidates) <= MAX_PARALLEL_CANDIDATES:
            return None
        return _("Candidate count cannot exceed {limit}; {count} were submitted.").format(
            limit=MAX_PARALLEL_CANDIDATES,
            count=len(candidates),
        )

    def _validate_rollback_target_limit(self) -> str | None:
        target_count = len(self._step_config.rollback_targets)
        if target_count <= MAX_ROLLBACK_TARGETS:
            return None
        return _(
            "Rollback target count cannot exceed {limit}; there are {count}. "
            "Ask the user for help or narrow the rollback targets before calling complete_step."
        ).format(limit=MAX_ROLLBACK_TARGETS, count=target_count)

    @staticmethod
    def _matches(pattern: str, value: str) -> bool:
        try:
            return re.search(pattern, value, flags=re.IGNORECASE) is not None
        except re.error:
            return pattern in value

    @staticmethod
    def _resolve_dotted(value: dict, path: str) -> Any:
        current: Any = value
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None
        return current

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        self.normalize_input(tool_input)
        rollback_target_error = self._validate_rollback_target_limit()
        if rollback_target_error is not None:
            return ToolResult(content=rollback_target_error, is_error=True)

        conclusion = tool_input["conclusion"]
        rollback = tool_input.get("rollback_request")
        rollback_tuple = (rollback["target_step"], rollback["reason"]) if rollback else None

        logger.debug("[complete_step] step=%s input=%r", self._step_config.step_id, tool_input)

        if rollback_tuple and self._step_config.rollback_count >= self._step_config.max_rollbacks:
            max_rollbacks = self._step_config.max_rollbacks
            return ToolResult(
                content=_(
                    "Rollback count cannot exceed {max_rollbacks}. Complete the current step or ask the user for help."
                ).format(max_rollbacks=max_rollbacks),
                is_error=True,
            )

        validation_error = self._validate_conclusion(conclusion)
        if validation_error is None:
            validation_error = self._validate_completion_guards(conclusion)
        if validation_error is None:
            validation_error = self._validate_candidate_limit(conclusion)
        if validation_error:
            self._validation_attempts += 1
            if self._validation_attempts > self._step_config.max_conclusion_retries:
                step_result = StepResult(
                    step_id=self._step_config.step_id,
                    status=StepStatus.FAILED,
                    error=_("Schema validation failed after {attempts} attempts: {error}").format(
                        attempts=self._validation_attempts,
                        error=validation_error,
                    ),
                )
                max_retries = self._step_config.max_conclusion_retries
                return ToolResult(
                    content=_(
                        "conclusion validation failed after exceeding the maximum retry count ({max_retries}): {error}"
                    ).format(max_retries=max_retries, error=validation_error),
                    is_error=True,
                    metadata={"step_result": step_result},
                )
            return ToolResult(
                content=_("conclusion validation failed; fix it and call complete_step again: {error}").format(
                    error=validation_error
                ),
                is_error=True,
            )

        step_result = StepResult(
            step_id=self._step_config.step_id,
            status=StepStatus.COMPLETED,
            conclusion=conclusion,
            rollback_request=rollback_tuple,
        )

        logger.debug("[complete_step] step=%s validation=OK conclusion=%r", self._step_config.step_id, conclusion)
        return ToolResult(
            content=_("Step {step_id} completed. Conclusion submitted.").format(
                step_id=display_step_name(self._step_config.step_id)
            ),
            metadata={"step_result": step_result},
        )
