"""CompleteStepTool — model calls this to signal step completion."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

import jsonschema

from iac_code.i18n import _
from iac_code.pipeline.display_names import display_step_name
from iac_code.pipeline.engine.hard_constraints import collect_hard_constraints, validate_hard_constraint_checks
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.utils.public_errors import sanitize_strict_text

if TYPE_CHECKING:
    from iac_code.pipeline.engine.types import StepConfig

logger = logging.getLogger(__name__)

MAX_PARALLEL_CANDIDATES = 5
MAX_ROLLBACK_TARGETS = 5
_COMPLETION_GUARD_MESSAGE_TEXT_BY_KEY = {
    "reviewing_rerun_after_validate_template_write": (
        "reviewing ran write_file/edit_file after ros_validate_template; "
        "rerun ros_validate_template and infraguard_scan for the same file_path."
    ),
    "reviewing_validate_template_required": (
        "reviewing must validate the repaired template with ros_validate_template for the same file_path."
    ),
    "reviewing_rerun_after_final_infraguard_write": (
        "reviewing ran write_file/edit_file after the final InfraGuard scan; "
        "rerun ros_validate_template and infraguard_scan for the same file_path."
    ),
    "reviewing_final_infraguard_required": (
        "reviewing must finish with a passing InfraGuard scan for the same file_path."
    ),
    "intent_cloud_resource_clarification_required": (
        "This input still lacks a clear cloud resource, deployment target, or operations constraint; "
        "clarify with the user first."
    ),
    "intent_alibaba_cloud_only": (
        "The current flow only supports Alibaba Cloud deployment requests; ask the user to change the target "
        "to Alibaba Cloud or confirm that it should not be handled for now."
    ),
    "intent_low_confidence_clarification_required": (
        "Low-confidence intent cannot be completed directly; clarify with the user first."
    ),
    "intent_not_deployment_request": (
        "This input is not a deployment or cloud resource request; ask the user to provide a deployment target "
        "or confirm that it should not be handled for now."
    ),
    "deploy_wait_create_complete": ("A successful deployment must wait until ros_deploy returns CREATE_COMPLETE."),
    "hard_constraint_verification_required": (
        "Every explicit user hard constraint must be covered by a satisfied check with matching parameters "
        "and evidence."
    ),
}
_COMPLETION_GUARD_MESSAGE_KEY_BY_TEXT = {text: key for key, text in _COMPLETION_GUARD_MESSAGE_TEXT_BY_KEY.items()}


def _completion_guard_message_from_key(key: str) -> str | None:
    text = _COMPLETION_GUARD_MESSAGE_TEXT_BY_KEY.get(key)
    return _(text) if text is not None else None


def _completion_guard_message_i18n_markers() -> tuple[str, ...]:
    """Keep YAML-selected completion guard messages visible to Babel."""
    return (
        _(
            "reviewing ran write_file/edit_file after ros_validate_template; "
            "rerun ros_validate_template and infraguard_scan for the same file_path."
        ),
        _("reviewing must validate the repaired template with ros_validate_template for the same file_path."),
        _(
            "reviewing ran write_file/edit_file after the final InfraGuard scan; "
            "rerun ros_validate_template and infraguard_scan for the same file_path."
        ),
        _("reviewing must finish with a passing InfraGuard scan for the same file_path."),
        _(
            "This input still lacks a clear cloud resource, deployment target, or operations constraint; "
            "clarify with the user first."
        ),
        _(
            "The current flow only supports Alibaba Cloud deployment requests; ask the user to change the target "
            "to Alibaba Cloud or confirm that it should not be handled for now."
        ),
        _("Low-confidence intent cannot be completed directly; clarify with the user first."),
        _(
            "This input is not a deployment or cloud resource request; ask the user to provide a deployment target "
            "or confirm that it should not be handled for now."
        ),
        _("A successful deployment must wait until ros_deploy returns CREATE_COMPLETE."),
        _(
            "Every explicit user hard constraint must be covered by a satisfied check with matching parameters "
            "and evidence."
        ),
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
        invalid_json = json.dumps(tool_input or {}, ensure_ascii=False)
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
        return error.message

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

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        if not output or not is_error:
            return None
        if verbose:
            return output.strip()
        return _("complete_step validation failed.")

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
            logger.warning(
                "Schema validation failed for step %s (validator=%s)",
                sanitize_strict_text(self._step_config.step_id),
                sanitize_strict_text(str(e.validator)),
            )
            return public_message

    def _validate_completion_guards(self, conclusion: dict) -> str | None:
        for guard in self._completion_guards:
            if not self._guard_applies(guard, conclusion):
                continue

            required_tool = guard.get("require_tool")
            required_tool_result = guard.get("require_tool_result")
            required_conclusion_sha256 = guard.get("require_conclusion_sha256")
            required_constraint_coverage = guard.get("require_context_constraint_coverage")
            required_field = guard.get("required_conclusion_field")
            required_any_of = guard.get("required_conclusion_any_of") or []
            successful_tools = self._completion_guard_state.get("successful_tools", set())
            if required_tool and required_tool not in successful_tools:
                message = self._completion_guard_message(
                    guard,
                    _("Clarification is required before completing the current step."),
                )
                return _(
                    "{message} Call {required_tool} first, then call complete_step after receiving the tool result."
                ).format(
                    message=message,
                    required_tool=required_tool,
                )
            if required_field and self._resolve_dotted(conclusion, required_field) in (None, "", [], {}):
                message = self._completion_guard_message(
                    guard,
                    _("Clarification output is required before completing the current step."),
                )
                return _("{message} complete_step.conclusion must include {required_field}.").format(
                    message=message,
                    required_field=required_field,
                )
            if required_any_of and all(
                self._resolve_dotted(conclusion, field) in (None, "", [], {}) for field in required_any_of
            ):
                message = self._completion_guard_message(
                    guard,
                    _("Clarification output is required before completing the current step."),
                )
                fields = _(" or ").join(str(field) for field in required_any_of)
                return _("{message} complete_step.conclusion must include one of these fields: {fields}.").format(
                    message=message,
                    fields=fields,
                )
            if isinstance(required_tool_result, dict):
                validation_error = self._validate_required_tool_result(
                    required_tool_result,
                    conclusion,
                    self._completion_guard_message(guard, None),
                )
                if validation_error is not None:
                    return validation_error
            if isinstance(required_conclusion_sha256, dict):
                validation_error = self._validate_conclusion_sha256(
                    required_conclusion_sha256,
                    conclusion,
                    self._completion_guard_message(guard, None),
                )
                if validation_error is not None:
                    return validation_error
            if isinstance(required_constraint_coverage, dict):
                validation_error = self._validate_context_constraint_coverage(
                    required_constraint_coverage,
                    conclusion,
                    self._completion_guard_message(guard, None),
                )
                if validation_error is not None:
                    return validation_error
        return None

    def _validate_context_constraint_coverage(
        self,
        requirement: dict[str, Any],
        conclusion: dict[str, Any],
        message: str | None,
    ) -> str | None:
        source_fields = requirement.get("source_fields") or []
        if not isinstance(source_fields, list) or not all(isinstance(field, str) for field in source_fields):
            return message or _("A completion guard is misconfigured.")
        context_snapshot = self._completion_guard_state.get("context_snapshot")
        if not isinstance(context_snapshot, dict):
            context_snapshot = {}
        constraints, source_issues = collect_hard_constraints(context_snapshot, source_fields)
        checks = self._resolve_dotted(conclusion, str(requirement.get("checks_field") or "hard_constraint_checks"))
        parameters = self._resolve_dotted(
            conclusion,
            str(requirement.get("deployment_parameters_field") or "deployment_parameters"),
        )
        issues = source_issues + validate_hard_constraint_checks(
            constraints,
            checks,
            parameters,
            tool_result_records=self._completion_guard_state.get("tool_result_records") or [],
        )
        if not issues:
            return None
        base_message = message or _(
            "Every explicit user hard constraint must be covered by a satisfied check with matching parameters "
            "and evidence."
        )
        issue_summaries = []
        for issue in issues:
            specifics = ", ".join(value for value in (issue.constraint_id, issue.detail) if value)
            issue_summaries.append(f"{issue.code}[{specifics}]" if specifics else issue.code)
        code = issues[0].code if len(issues) == 1 else "multiple_constraint_issues"
        detail = "; ".join(issue_summaries)
        return _("{message} Validation issue: {code} ({detail}).").format(
            message=base_message,
            code=code,
            detail=detail,
        )

    @staticmethod
    def _completion_guard_message(config: dict[str, Any], default: str | None) -> str | None:
        message_key = config.get("message_key")
        if isinstance(message_key, str) and message_key:
            return _completion_guard_message_from_key(message_key) or message_key
        message = config.get("message")
        if isinstance(message, str) and message:
            known_key = _COMPLETION_GUARD_MESSAGE_KEY_BY_TEXT.get(message)
            if known_key is not None:
                return _completion_guard_message_from_key(known_key)
            return message
        return default

    def _validate_conclusion_sha256(
        self,
        requirement: dict[str, Any],
        conclusion: dict[str, Any],
        message: str | None,
    ) -> str | None:
        content_field = str(requirement.get("content_field") or requirement.get("field") or "")
        sha256_field = str(requirement.get("sha256_field") or "")
        base_message = message or _("A conclusion content hash is required before completing the current step.")
        if not content_field or not sha256_field:
            return _("{message} Completion guard is missing content_field or sha256_field.").format(
                message=base_message
            )
        content = self._resolve_dotted(conclusion, content_field)
        expected = self._resolve_dotted(conclusion, sha256_field)
        if not isinstance(content, str) or content == "":
            return _("{message} complete_step.conclusion.{field} must be a non-empty string.").format(
                message=base_message,
                field=content_field,
            )
        if not isinstance(expected, str) or expected == "":
            return _("{message} complete_step.conclusion.{field} must include the sha256 hash.").format(
                message=base_message,
                field=sha256_field,
            )
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if actual != expected.strip().lower():
            return _(
                "{message} complete_step.conclusion.{sha256_field} must equal the sha256 of "
                "complete_step.conclusion.{content_field}; actual hash was {actual}."
            ).format(
                message=base_message,
                sha256_field=sha256_field,
                content_field=content_field,
                actual=actual,
            )
        return None

    def _validate_required_tool_result(
        self,
        requirement: dict[str, Any],
        conclusion: dict[str, Any],
        message: str | None,
    ) -> str | None:
        tool_name = str(requirement.get("tool") or "")
        actions = self._expected_actions(requirement)
        expected_product = requirement.get("product")
        expected_success = requirement.get("is_success")
        status_in = {str(status) for status in requirement.get("status_in") or [] if status is not None}
        result_field_equals = requirement.get("result_field_equals") or {}
        required_result_fields = requirement.get("required_result_fields") or []
        base_message = message or _("A successful tool result is required before completing the current step.")

        records = self._completion_guard_state.get("tool_result_records") or []
        after_index = -1
        after_requirement = requirement.get("after_tool_result")
        if isinstance(after_requirement, dict):
            after_match_index = self._latest_satisfied_tool_result_index(records, after_requirement, conclusion)
            if after_match_index is None:
                after_tool = str(after_requirement.get("tool") or _("the required tool"))
                return _(
                    "{message} Call {after_tool} first and wait for a successful result before calling {tool}."
                ).format(
                    message=base_message,
                    after_tool=after_tool,
                    tool=tool_name or _("the required tool"),
                )
            after_index = after_match_index
        if bool(requirement.get("latest_match")):
            return self._validate_latest_required_tool_result(
                records,
                requirement,
                conclusion,
                base_message,
                after_index=after_index,
            )

        mismatch_message: str | None = None
        field_mismatch_message: str | None = None
        for index, record in enumerate(records):
            if index <= after_index:
                continue
            if not isinstance(record, dict):
                continue
            if tool_name and record.get("tool_name") != tool_name:
                continue
            tool_input = record.get("input") if isinstance(record.get("input"), dict) else {}
            if expected_product and not self._strings_equal_ignore_case(
                self._first_string(tool_input, ("product", "Product")),
                str(expected_product),
            ):
                continue
            if actions and self._first_string(tool_input, ("action", "Action")) not in actions:
                continue
            if record.get("is_error"):
                continue
            result = record.get("result")
            if not isinstance(result, dict):
                continue
            if expected_success is not None and self._bool_from_result(result) is not bool(expected_success):
                continue
            if status_in:
                status = self._status_from_result(result)
                if status not in status_in:
                    continue
            field_mismatch = self._first_match_field_mismatch(requirement, conclusion, tool_input, result, tool_name)
            if field_mismatch is not None:
                mismatch_message = self._format_match_field_mismatch(base_message, field_mismatch)
                continue
            if isinstance(result_field_equals, dict):
                failed_field = self._first_failed_result_field(result, result_field_equals)
                if failed_field is not None:
                    field, expected, actual = failed_field
                    field_mismatch_message = _(
                        "{message} The {tool} result field {field} must equal {expected}; actual value was {actual}."
                    ).format(
                        message=base_message,
                        tool=tool_name or _("tool"),
                        field=field,
                        expected=expected,
                        actual=actual,
                    )
                    continue
            missing_required_field = self._first_missing_result_field(result, required_result_fields)
            if missing_required_field is not None:
                field_mismatch_message = _("{message} The {tool} result must include non-empty field {field}.").format(
                    message=base_message,
                    tool=tool_name or _("tool"),
                    field=missing_required_field,
                )
                continue
            disallowed_message = self._validate_no_disallowed_tool_results_after_match(
                records,
                requirement,
                conclusion,
                base_message,
                matched_index=index,
            )
            if disallowed_message is not None:
                field_mismatch_message = disallowed_message
                continue
            return None

        if field_mismatch_message is not None:
            return field_mismatch_message
        if mismatch_message is not None:
            return mismatch_message
        status_hint = ""
        if status_in:
            status_hint = _(" with status {statuses}").format(statuses=", ".join(sorted(status_in)))
        success_hint = ""
        if expected_success is not None:
            success_hint = _(" and is_success={expected}").format(expected=str(bool(expected_success)).lower())
        action_hint = ""
        if len(actions) == 1:
            action_hint = f" {next(iter(actions))}"
        elif actions:
            action_hint = _(" one of {actions}").format(actions=", ".join(sorted(actions)))
        return _(
            "{message} Call {tool}{action} first and wait for a successful result{status_hint}{success_hint}."
        ).format(
            message=base_message,
            tool=tool_name or _("the required tool"),
            action=action_hint,
            status_hint=status_hint,
            success_hint=success_hint,
        )

    def _latest_satisfied_tool_result_index(
        self,
        records: list[Any],
        requirement: dict[str, Any],
        conclusion: dict[str, Any],
    ) -> int | None:
        for index in range(len(records) - 1, -1, -1):
            record = records[index]
            if isinstance(record, dict) and self._tool_result_record_satisfies(record, requirement, conclusion):
                return index
        return None

    def _tool_result_record_satisfies(
        self,
        record: dict[str, Any],
        requirement: dict[str, Any],
        conclusion: dict[str, Any],
    ) -> bool:
        tool_name = str(requirement.get("tool") or "")
        actions = self._expected_actions(requirement)
        expected_product = requirement.get("product")
        expected_success = requirement.get("is_success")
        status_in = {str(status) for status in requirement.get("status_in") or [] if status is not None}
        result_field_equals = requirement.get("result_field_equals") or {}
        required_result_fields = requirement.get("required_result_fields") or []

        if tool_name and record.get("tool_name") != tool_name:
            return False
        tool_input = self._dict_value(record.get("input"))
        if expected_product and not self._strings_equal_ignore_case(
            self._first_string(tool_input, ("product", "Product")),
            str(expected_product),
        ):
            return False
        if actions and self._first_string(tool_input, ("action", "Action")) not in actions:
            return False
        if record.get("is_error"):
            return False
        result = record.get("result")
        if not isinstance(result, dict):
            return False
        if expected_success is not None and self._bool_from_result(result) is not bool(expected_success):
            return False
        if status_in:
            status = self._status_from_result(result)
            if status not in status_in:
                return False
        if self._first_match_field_mismatch(requirement, conclusion, tool_input, result, tool_name) is not None:
            return False
        if isinstance(result_field_equals, dict) and self._first_failed_result_field(result, result_field_equals):
            return False
        if self._first_missing_result_field(result, required_result_fields) is not None:
            return False
        return True

    def _validate_latest_required_tool_result(
        self,
        records: list[Any],
        requirement: dict[str, Any],
        conclusion: dict[str, Any],
        base_message: str,
        *,
        after_index: int,
    ) -> str | None:
        tool_name = str(requirement.get("tool") or "")
        actions = self._expected_actions(requirement)
        expected_product = requirement.get("product")
        expected_success = requirement.get("is_success")
        status_in = {str(status) for status in requirement.get("status_in") or [] if status is not None}
        result_field_equals = requirement.get("result_field_equals") or {}
        required_result_fields = requirement.get("required_result_fields") or []
        mismatch_message: str | None = None

        for index in range(len(records) - 1, after_index, -1):
            record = records[index]
            if not isinstance(record, dict):
                continue
            if tool_name and record.get("tool_name") != tool_name:
                continue
            tool_input = record.get("input") if isinstance(record.get("input"), dict) else {}
            if expected_product and not self._strings_equal_ignore_case(
                self._first_string(tool_input, ("product", "Product")),
                str(expected_product),
            ):
                continue
            if actions and self._first_string(tool_input, ("action", "Action")) not in actions:
                continue
            raw_result = record.get("result")
            result = raw_result if isinstance(raw_result, dict) else {}
            field_mismatch = self._first_match_field_mismatch(requirement, conclusion, tool_input, result, tool_name)
            if field_mismatch is not None:
                mismatch_message = self._format_match_field_mismatch(base_message, field_mismatch)
                continue
            if record.get("is_error") or not isinstance(raw_result, dict):
                break
            if expected_success is not None and self._bool_from_result(result) is not bool(expected_success):
                break
            if status_in:
                status = self._status_from_result(result)
                if status not in status_in:
                    break
            if isinstance(result_field_equals, dict):
                failed_field = self._first_failed_result_field(result, result_field_equals)
                if failed_field is not None:
                    field, expected, actual = failed_field
                    return _(
                        "{message} The latest matching {tool} result field {field} must equal {expected}; "
                        "actual value was {actual}."
                    ).format(
                        message=base_message,
                        tool=tool_name or _("tool"),
                        field=field,
                        expected=expected,
                        actual=actual,
                    )
            missing_required_field = self._first_missing_result_field(result, required_result_fields)
            if missing_required_field is not None:
                return _("{message} The latest matching {tool} result must include non-empty field {field}.").format(
                    message=base_message,
                    tool=tool_name or _("tool"),
                    field=missing_required_field,
                )
            disallowed_message = self._validate_no_disallowed_tool_results_after_match(
                records,
                requirement,
                conclusion,
                base_message,
                matched_index=index,
            )
            if disallowed_message is not None:
                return disallowed_message
            return None

        if mismatch_message is not None:
            return mismatch_message
        return _("{message} Call {tool} first and wait for a successful result.").format(
            message=base_message,
            tool=tool_name or _("the required tool"),
        )

    def _validate_no_disallowed_tool_results_after_match(
        self,
        records: list[Any],
        requirement: dict[str, Any],
        conclusion: dict[str, Any],
        base_message: str,
        *,
        matched_index: int,
    ) -> str | None:
        raw_rules = requirement.get("disallow_tool_results_after_match") or []
        if isinstance(raw_rules, dict):
            rules = [raw_rules]
        elif isinstance(raw_rules, list):
            rules = [rule for rule in raw_rules if isinstance(rule, dict)]
        else:
            rules = []
        if not rules:
            return None

        for record in records[matched_index + 1 :]:
            if not isinstance(record, dict) or record.get("is_error"):
                continue
            record_tool_name = str(record.get("tool_name") or "")
            tool_input = record.get("input") if isinstance(record.get("input"), dict) else {}
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            for rule in rules:
                tool_names = self._string_set(rule.get("tool")) | self._string_set(rule.get("tools"))
                if tool_names and record_tool_name not in tool_names:
                    continue
                match_conclusion_field = rule.get("match_conclusion_field")
                match_result_field = str(rule.get("match_result_field") or "file_path")
                if isinstance(match_conclusion_field, str) and match_conclusion_field:
                    conclusion_value = self._resolve_dotted(conclusion, match_conclusion_field)
                    result_value = self._resolve_match_result_field(tool_input, result, match_result_field)
                    if not self._field_values_match(
                        conclusion_value,
                        result_value,
                        conclusion_field=match_conclusion_field,
                        result_field=match_result_field,
                    ):
                        continue
                rule_message = self._completion_guard_message(rule, None)
                if rule_message is not None:
                    return rule_message.format(message=base_message, tool=record_tool_name or _("A write tool"))
                return _(
                    "{message} {tool} ran after the required validation result for the same target; "
                    "rerun the required validation before completing the step."
                ).format(message=base_message, tool=record_tool_name or _("A write tool"))
        return None

    @classmethod
    def _resolve_match_result_field(
        cls,
        tool_input: dict[str, Any],
        result: dict[str, Any],
        match_result_field: str,
    ) -> Any:
        if match_result_field == "stack_id":
            return cls._stack_id_from_result(result)
        source = {"input": tool_input, "result": result}
        value = cls._resolve_dotted(source, match_result_field)
        if value is not None:
            return value
        return cls._resolve_dotted(result, match_result_field)

    def _first_match_field_mismatch(
        self,
        requirement: dict[str, Any],
        conclusion: dict[str, Any],
        tool_input: dict[str, Any],
        result: dict[str, Any],
        tool_name: str,
    ) -> dict[str, Any] | None:
        for field_spec in self._match_field_specs(requirement):
            conclusion_field = field_spec["conclusion_field"]
            result_field = field_spec["result_field"]
            conclusion_value = self._resolve_dotted(conclusion, conclusion_field)
            result_value = self._resolve_match_result_field(tool_input, result, result_field)
            if self._field_values_match(
                conclusion_value,
                result_value,
                conclusion_field=conclusion_field,
                result_field=result_field,
            ):
                continue
            return {
                "conclusion_field": conclusion_field,
                "result_field": result_field,
                "result_value": result_value,
                "tool_name": tool_name,
            }
        return None

    @staticmethod
    def _match_field_specs(requirement: dict[str, Any]) -> list[dict[str, str]]:
        specs: list[dict[str, str]] = []
        match_conclusion_field = requirement.get("match_conclusion_field")
        if isinstance(match_conclusion_field, str) and match_conclusion_field:
            specs.append(
                {
                    "conclusion_field": match_conclusion_field,
                    "result_field": str(requirement.get("match_result_field") or "stack_id"),
                }
            )
        raw_specs = requirement.get("match_fields") or []
        if isinstance(raw_specs, dict):
            raw_specs = [raw_specs]
        if isinstance(raw_specs, list):
            for raw_spec in raw_specs:
                if not isinstance(raw_spec, dict):
                    continue
                conclusion_field = raw_spec.get("conclusion_field") or raw_spec.get("match_conclusion_field")
                result_field = raw_spec.get("result_field") or raw_spec.get("match_result_field")
                if isinstance(conclusion_field, str) and conclusion_field:
                    specs.append(
                        {
                            "conclusion_field": conclusion_field,
                            "result_field": str(result_field or "stack_id"),
                        }
                    )
        return specs

    @staticmethod
    def _format_match_field_mismatch(base_message: str, mismatch: dict[str, Any]) -> str:
        return _("{message} complete_step.conclusion.{field} must match the {tool} result value {value}.").format(
            message=base_message,
            field=mismatch["conclusion_field"],
            tool=mismatch["tool_name"] or _("tool"),
            value=mismatch["result_value"] or _("<missing>"),
        )

    def _field_values_match(
        self,
        conclusion_value: Any,
        result_value: Any,
        *,
        conclusion_field: str,
        result_field: str,
    ) -> bool:
        if conclusion_value == result_value:
            return True
        if not isinstance(conclusion_value, str) or not isinstance(result_value, str):
            return False
        if not self._path_field(conclusion_field) and not self._path_field(result_field):
            return False
        cwd = self._completion_guard_state.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return False
        return self._canonical_local_path(conclusion_value, cwd) == self._canonical_local_path(result_value, cwd)

    @staticmethod
    def _path_field(field: str) -> bool:
        normalized = field.lower()
        return "path" in normalized or "templateurl" in normalized

    @staticmethod
    def _canonical_local_path(value: str, cwd: str) -> str:
        if "://" in value:
            return value
        expanded = os.path.expandvars(os.path.expanduser(value))
        if not os.path.isabs(expanded):
            expanded = os.path.join(cwd, expanded)
        return os.path.normcase(os.path.realpath(os.path.abspath(expanded)))

    @classmethod
    def _first_failed_result_field(
        cls,
        result: dict[str, Any],
        result_field_equals: dict[str, Any],
    ) -> tuple[str, Any, Any] | None:
        for field, expected in result_field_equals.items():
            actual = cls._resolve_dotted(result, field)
            if actual != expected:
                return field, expected, actual
        return None

    @classmethod
    def _first_missing_result_field(cls, result: dict[str, Any], required_fields: Any) -> str | None:
        if not isinstance(required_fields, list):
            return None
        for field in required_fields:
            if not isinstance(field, str) or not field:
                continue
            if cls._resolve_dotted(result, field) in (None, "", [], {}):
                return field
        return None

    def validate_completion_input(self, tool_input: dict[str, Any]) -> str | None:
        """Validate a complete_step input without mutating retry counters."""

        self.normalize_input(tool_input)
        rollback_target_error = self._validate_rollback_target_limit()
        if rollback_target_error is not None:
            return rollback_target_error

        conclusion = tool_input["conclusion"]
        rollback = tool_input.get("rollback_request")
        rollback_tuple = (rollback["target_step"], rollback["reason"]) if rollback else None
        if rollback_tuple and self._step_config.rollback_count >= self._step_config.max_rollbacks:
            return self._rollback_budget_exhausted_message(rollback_tuple)

        validation_error = self._validate_conclusion(conclusion)
        if validation_error is None:
            validation_error = self._validate_completion_guards(conclusion)
        if validation_error is None:
            validation_error = self._validate_candidate_limit(conclusion)
        return validation_error

    def _guard_applies(self, guard: dict, conclusion: dict) -> bool:
        unless_patterns = guard.get("unless_user_message_matches_any") or []
        if any(self._matches(pattern, self._user_message) for pattern in unless_patterns):
            return False

        user_patterns = guard.get("when_user_message_matches_any") or []
        conclusion_equals = guard.get("when_conclusion_field_equals") or {}
        applies = guard.get("always") is True
        applies = applies or bool(
            user_patterns and any(self._matches(pattern, self._user_message) for pattern in user_patterns)
        )
        applies = applies or any(
            self._resolve_dotted(conclusion, field) == value for field, value in conclusion_equals.items()
        )
        if not applies:
            return False

        when_tool_result_exists = guard.get("when_tool_result_exists")
        if isinstance(when_tool_result_exists, dict) and not self._matching_tool_result_exists(
            when_tool_result_exists,
            conclusion,
        ):
            return False
        return True

    def _matching_tool_result_exists(self, requirement: dict[str, Any], conclusion: dict[str, Any]) -> bool:
        tool_names = self._string_set(requirement.get("tool")) | self._string_set(requirement.get("tools"))
        match_conclusion_field = requirement.get("match_conclusion_field")
        match_result_field = str(requirement.get("match_result_field") or "file_path")
        records = self._completion_guard_state.get("tool_result_records") or []
        for record in records:
            if not isinstance(record, dict) or record.get("is_error"):
                continue
            record_tool_name = str(record.get("tool_name") or "")
            if tool_names and record_tool_name not in tool_names:
                continue
            if isinstance(match_conclusion_field, str) and match_conclusion_field:
                tool_input = record.get("input") if isinstance(record.get("input"), dict) else {}
                result = record.get("result") if isinstance(record.get("result"), dict) else {}
                conclusion_value = self._resolve_dotted(conclusion, match_conclusion_field)
                result_value = self._resolve_match_result_field(tool_input, result, match_result_field)
                if not self._field_values_match(
                    conclusion_value,
                    result_value,
                    conclusion_field=match_conclusion_field,
                    result_field=match_result_field,
                ):
                    continue
            return True
        return False

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

    def _rollback_budget_exhausted_message(self, rollback_tuple: tuple[str, str]) -> str:
        """Explain a rollback-limit failure with root cause and follow-up options.

        The first sentence keeps the historical "Rollback count cannot exceed
        {max_rollbacks}" wording, then a root-cause diagnosis (the last rollback
        target and reason) and the user-facing follow-up options (retry, change
        spec / return to candidate selection, or ask for human help) are appended
        so the deployment failure is explained instead of only surfacing the
        terse limit error.
        """
        target_step, reason = rollback_tuple
        target_label = display_step_name(target_step)
        clean_reason = sanitize_strict_text(str(reason)).strip() or _("no reason was provided")
        return _(
            "Rollback count cannot exceed {max_rollbacks}. The deployment kept rolling back to {target_step} "
            "because: {reason}. Do not roll back again. Finish this step with status: failed and, in the error "
            "field, explain to the user that the deployment did not complete, give this root-cause diagnosis, and "
            "offer follow-up options: retry the deployment, change the specification (return to candidate "
            "selection), or ask for human help."
        ).format(
            max_rollbacks=self._step_config.max_rollbacks,
            target_step=target_label,
            reason=clean_reason,
        )

    @staticmethod
    def _matches(pattern: str, value: str) -> bool:
        try:
            return re.search(pattern, value, flags=re.IGNORECASE) is not None
        except re.error:
            return pattern in value

    @staticmethod
    def _resolve_dotted(value: Any, path: str) -> Any:
        current: Any = value
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                if index >= len(current):
                    return None
                current = current[index]
            else:
                return None
            if current is None:
                return None
        return current

    @classmethod
    def _status_from_result(cls, result: dict[str, Any]) -> str | None:
        nested = cls._dict_value(result.get("Stack") or result.get("stack"))
        return cls._first_string(
            result,
            ("StackStatus", "stackStatus", "stack_status", "Status", "status"),
        ) or cls._first_string(nested, ("StackStatus", "stackStatus", "stack_status", "Status", "status"))

    @classmethod
    def _stack_id_from_result(cls, result: dict[str, Any]) -> str | None:
        nested = cls._dict_value(result.get("Stack") or result.get("stack"))
        return cls._first_string(result, ("StackId", "stackId", "stack_id")) or cls._first_string(
            nested,
            ("StackId", "stackId", "stack_id"),
        )

    @classmethod
    def _bool_from_result(cls, result: dict[str, Any]) -> bool | None:
        value = result.get("is_success")
        if value is None:
            value = result.get("isSuccess")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lower = value.strip().lower()
            if lower in {"true", "1", "yes"}:
                return True
            if lower in {"false", "0", "no"}:
                return False
        return None

    @staticmethod
    def _dict_value(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _first_string(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _strings_equal_ignore_case(left: str | None, right: str | None) -> bool:
        if left is None or right is None:
            return left == right
        return left.lower() == right.lower()

    @classmethod
    def _expected_actions(cls, requirement: dict[str, Any]) -> set[str]:
        actions: set[str] = set()
        for key in ("action", "action_in", "actions"):
            actions.update(cls._string_set(requirement.get(key)))
        return actions

    @staticmethod
    def _string_set(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value} if value else set()
        if isinstance(value, list | tuple | set):
            return {str(item) for item in value if item not in (None, "")}
        return set()

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        self.normalize_input(tool_input)
        rollback_target_error = self._validate_rollback_target_limit()
        if rollback_target_error is not None:
            return ToolResult(content=rollback_target_error, is_error=True)

        conclusion = tool_input["conclusion"]
        rollback = tool_input.get("rollback_request")
        rollback_tuple = (rollback["target_step"], rollback["reason"]) if rollback else None

        logger.debug(
            "[complete_step] step=%s input=%s",
            self._step_config.step_id,
            sanitize_strict_text(repr(tool_input)),
        )

        if rollback_tuple and self._step_config.rollback_count >= self._step_config.max_rollbacks:
            return ToolResult(
                content=self._rollback_budget_exhausted_message(rollback_tuple),
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

        logger.debug(
            "[complete_step] step=%s validation=OK conclusion=%s",
            self._step_config.step_id,
            sanitize_strict_text(repr(conclusion)),
        )
        return ToolResult(
            content=_("Step {step_id} completed. Conclusion submitted.").format(
                step_id=display_step_name(self._step_config.step_id)
            ),
            metadata={
                "step_result": step_result,
                "complete_step_terminal": self._step_config.complete_step_terminal,
            },
        )
