import hashlib
from pathlib import Path

import pytest

from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
from iac_code.pipeline.engine.types import StepConfig, StepStatus
from iac_code.tools.base import ToolContext


@pytest.fixture
def step_config():
    return StepConfig(
        step_id="intent_parsing",
        conclusion_field="intent",
        forward="architecture_planning",
    )


@pytest.fixture
def tool(step_config):
    return CompleteStepTool(step_config)


class TestCompleteStepToolMeta:
    def test_name(self, tool):
        assert tool.name == "complete_step"

    def test_has_input_schema(self, tool):
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "conclusion" in schema["properties"]
        assert "conclusion" in schema["required"]

    def test_error_result_renders_compact_summary(self, tool):
        long_error = (
            "Invalid input for tool 'complete_step': 'conclusion' is a required property\n"
            "Current step: intent_parsing\n"
            "conclusion must match this schema summary:\n"
            '{"type": "object", "required": ["is_infra_intent", "confidence"]}'
        )

        compact = tool.render_tool_result_message(long_error, is_error=True)

        assert compact == "complete_step validation failed."
        assert "'conclusion' is a required property" not in compact
        assert "schema summary" not in compact

    @pytest.mark.asyncio
    async def test_completion_guard_message_key_renders_translated_message(self, step_config):
        tool = CompleteStepTool(
            step_config,
            completion_guards=[
                {
                    "require_tool": "ask_user_question",
                    "when_conclusion_field_equals": {"category": "chat"},
                    "message_key": "intent_not_deployment_request",
                }
            ],
            completion_guard_state={"successful_tools": set()},
        )

        result = await tool.execute(tool_input={"conclusion": {"category": "chat"}}, context=ToolContext())

        assert result.is_error
        assert "deployment or cloud resource request" in result.content
        assert "intent_not_deployment_request" not in result.content


class TestDynamicInputSchema:
    def test_schema_with_conclusion_schema(self):
        config = StepConfig(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward="arch",
            conclusion_schema={
                "type": "object",
                "required": ["is_infra"],
                "properties": {"is_infra": {"type": "boolean"}},
            },
        )
        tool = CompleteStepTool(config)
        schema = tool.input_schema
        assert schema["properties"]["conclusion"] == {
            "type": "object",
            "required": ["is_infra"],
            "properties": {"is_infra": {"type": "boolean"}},
        }

    def test_schema_without_conclusion_schema(self):
        config = StepConfig(step_id="x", conclusion_field="x", forward=None)
        tool = CompleteStepTool(config)
        schema = tool.input_schema
        assert schema["properties"]["conclusion"]["type"] == "object"
        assert "properties" not in schema["properties"]["conclusion"]

    def test_rollback_targets_in_schema(self):
        config = StepConfig(
            step_id="arch",
            conclusion_field="architecture",
            forward=None,
            rollback_targets=["intent_parsing", "requirements"],
        )
        tool = CompleteStepTool(config)
        schema = tool.input_schema
        rb = schema["properties"]["rollback_request"]
        assert rb["properties"]["target_step"]["enum"] == ["intent_parsing", "requirements"]

    def test_rollback_request_hidden_when_too_many_targets(self):
        config = StepConfig(
            step_id="arch",
            conclusion_field="architecture",
            forward=None,
            rollback_targets=[f"step_{index}" for index in range(6)],
        )
        tool = CompleteStepTool(config)

        schema = tool.input_schema

        assert "rollback_request" not in schema["properties"]

    def test_no_rollback_in_schema_when_no_targets(self):
        config = StepConfig(step_id="x", conclusion_field="x", forward=None, rollback_targets=[])
        tool = CompleteStepTool(config)
        schema = tool.input_schema
        assert "rollback_request" not in schema["properties"]

    def test_extra_rollback_request_is_rejected_when_no_targets(self):
        config = StepConfig(step_id="x", conclusion_field="x", forward=None, rollback_targets=[])
        tool = CompleteStepTool(config)

        is_valid, error = tool.validate_input(
            {
                "conclusion": {"ok": True},
                "rollback_request": {"target_step": "future_step", "reason": "try to skip ahead"},
            }
        )

        assert is_valid is False
        assert "rollback_request" in error


class TestCompleteStepToolExecute:
    @pytest.mark.asyncio
    async def test_returns_step_result_in_metadata(self, tool):
        context = ToolContext()
        result = await tool.execute(
            tool_input={"conclusion": {"intent_type": "e-commerce", "requirements": ["ECS", "RDS"]}},
            context=context,
        )
        assert not result.is_error
        assert "step_result" in result.metadata
        step_result = result.metadata["step_result"]
        assert step_result.step_id == "intent_parsing"
        assert step_result.status == StepStatus.COMPLETED
        assert step_result.conclusion == {"intent_type": "e-commerce", "requirements": ["ECS", "RDS"]}
        assert step_result.rollback_request is None
        assert result.metadata["complete_step_terminal"] is True

    @pytest.mark.asyncio
    async def test_marks_success_as_non_terminal_when_configured(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            complete_step_terminal=False,
        )

        result = await CompleteStepTool(config).execute(
            tool_input={"conclusion": {"status": "success", "outputs": {"url": "https://example.com"}}},
            context=ToolContext(),
        )

        assert not result.is_error
        assert result.metadata["complete_step_terminal"] is False

    @pytest.mark.asyncio
    async def test_with_rollback_request(self, tool):
        context = ToolContext()
        result = await tool.execute(
            tool_input={
                "conclusion": {"cost": 5000},
                "rollback_request": {
                    "target_step": "spec_recommending",
                    "reason": "cost_too_high",
                },
            },
            context=context,
        )
        step_result = result.metadata["step_result"]
        assert step_result.rollback_request == ("spec_recommending", "cost_too_high")

    @pytest.mark.asyncio
    async def test_content_mentions_localized_step_display_name(self, tool):
        context = ToolContext()
        result = await tool.execute(
            tool_input={"conclusion": {"done": True}},
            context=context,
        )
        assert "Intent parsing" in result.content
        assert "intent_parsing" not in result.content

    @pytest.mark.asyncio
    async def test_allows_five_candidates(self):
        config = StepConfig(step_id="architecture_planning", conclusion_field="architecture", forward=None)
        tool = CompleteStepTool(config)

        result = await tool.execute(
            tool_input={"conclusion": {"candidates": [{"name": str(i)} for i in range(5)]}},
            context=ToolContext(),
        )

        assert not result.is_error
        assert result.metadata["step_result"].status == StepStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_rejects_more_than_five_candidates_before_parallel_execution(self):
        config = StepConfig(step_id="architecture_planning", conclusion_field="architecture", forward=None)
        tool = CompleteStepTool(config)

        result = await tool.execute(
            tool_input={"conclusion": {"candidates": [{"name": str(i)} for i in range(6)]}},
            context=ToolContext(),
        )

        assert result.is_error
        assert "Candidate count cannot exceed 5" in result.content
        assert result.metadata is None

    @pytest.mark.asyncio
    async def test_rejects_rollback_when_budget_is_exhausted_before_step_result(self):
        config = StepConfig(
            step_id="cost_estimating",
            conclusion_field="cost",
            forward=None,
            rollback_targets=["template_generating"],
        )
        config.rollback_count = 5
        config.max_rollbacks = 5
        tool = CompleteStepTool(config)

        result = await tool.execute(
            tool_input={
                "conclusion": {"total": 200},
                "rollback_request": {"target_step": "template_generating", "reason": "redo"},
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert result.metadata is None
        assert "5" in result.content

    @pytest.mark.asyncio
    async def test_exhausted_rollback_falls_back_to_configured_target(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            rollback_targets=["confirm_and_select"],
            rollback_count=3,
            max_rollbacks=3,
            rollback_exhausted_target="confirm_and_select",
        )
        tool = CompleteStepTool(config)

        result = await tool.execute(
            tool_input={
                "conclusion": {"status": "failed"},
                "rollback_request": {"target_step": "confirm_and_select", "reason": "rollback limit reached"},
            },
            context=ToolContext(),
        )

        assert not result.is_error
        step_result = result.metadata["step_result"]
        assert step_result.status == StepStatus.COMPLETED
        assert step_result.rollback_request == ("confirm_and_select", "rollback limit reached")
        assert step_result.rollback_exhausted is True

    @pytest.mark.asyncio
    async def test_exhausted_rollback_fallback_redirects_other_targets(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            rollback_targets=["confirm_and_select", "architecture_planning"],
            rollback_count=3,
            max_rollbacks=3,
            rollback_exhausted_target="confirm_and_select",
        )
        tool = CompleteStepTool(config)

        result = await tool.execute(
            tool_input={
                "conclusion": {"status": "failed"},
                "rollback_request": {"target_step": "architecture_planning", "reason": "template broken"},
            },
            context=ToolContext(),
        )

        assert not result.is_error
        step_result = result.metadata["step_result"]
        assert step_result.rollback_request == ("confirm_and_select", "template broken")
        assert step_result.rollback_exhausted is True

    @pytest.mark.asyncio
    async def test_exhausted_rollback_without_fallback_target_keeps_error(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            rollback_targets=["confirm_and_select"],
            rollback_count=3,
            max_rollbacks=3,
        )
        tool = CompleteStepTool(config)

        result = await tool.execute(
            tool_input={
                "conclusion": {"status": "failed"},
                "rollback_request": {"target_step": "confirm_and_select", "reason": "rollback limit reached"},
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert result.metadata is None
        assert "Rollback count cannot exceed 3" in result.content

    @pytest.mark.asyncio
    async def test_self_referential_fallback_target_keeps_error(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            rollback_targets=["confirm_and_select"],
            rollback_count=3,
            max_rollbacks=3,
            rollback_exhausted_target="deploying",
        )
        tool = CompleteStepTool(config)

        result = await tool.execute(
            tool_input={
                "conclusion": {"status": "failed"},
                "rollback_request": {"target_step": "confirm_and_select", "reason": "rollback limit reached"},
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "Rollback count cannot exceed 3" in result.content

    def test_validate_completion_input_allows_exhausted_rollback_with_fallback(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            rollback_targets=["confirm_and_select"],
            rollback_count=3,
            max_rollbacks=3,
            rollback_exhausted_target="confirm_and_select",
        )
        tool = CompleteStepTool(config)

        error = tool.validate_completion_input(
            {
                "conclusion": {"status": "failed"},
                "rollback_request": {"target_step": "confirm_and_select", "reason": "rollback limit reached"},
            }
        )

        assert error is None

    def test_validate_completion_input_rejects_exhausted_rollback_without_fallback(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            rollback_targets=["confirm_and_select"],
            rollback_count=3,
            max_rollbacks=3,
        )
        tool = CompleteStepTool(config)

        error = tool.validate_completion_input(
            {
                "conclusion": {"status": "failed"},
                "rollback_request": {"target_step": "confirm_and_select", "reason": "rollback limit reached"},
            }
        )

        assert error is not None
        assert "Rollback count cannot exceed 3" in error

    @pytest.mark.asyncio
    async def test_within_budget_rollback_is_not_marked_exhausted(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            rollback_targets=["confirm_and_select"],
            rollback_count=1,
            max_rollbacks=3,
            rollback_exhausted_target="confirm_and_select",
        )
        tool = CompleteStepTool(config)

        result = await tool.execute(
            tool_input={
                "conclusion": {"status": "failed"},
                "rollback_request": {"target_step": "confirm_and_select", "reason": "retry"},
            },
            context=ToolContext(),
        )

        step_result = result.metadata["step_result"]
        assert step_result.rollback_request == ("confirm_and_select", "retry")
        assert step_result.rollback_exhausted is False

    @pytest.mark.asyncio
    async def test_rejects_when_rollback_target_count_exceeds_limit(self):
        config = StepConfig(
            step_id="reviewing",
            conclusion_field="review",
            forward=None,
            rollback_targets=[f"step_{index}" for index in range(6)],
        )
        tool = CompleteStepTool(config)

        is_valid, error = tool.validate_input({"conclusion": {"ok": True}})
        result = await tool.execute(tool_input={"conclusion": {"ok": True}}, context=ToolContext())

        assert is_valid is False
        assert "Rollback target count cannot exceed 5" in error
        assert result.is_error
        assert result.metadata is None
        assert "Rollback target count cannot exceed 5" in result.content


class TestCompletionGuards:
    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def test_requires_generic_context_constraint_coverage_and_tool_evidence(self):
        constraint = {
            "id": "db-storage-min",
            "target": "RDS",
            "property": "storage",
            "operator": "gte",
            "value": 100,
            "unit": "GiB",
            "verification_mode": "tool",
            "source": "user",
            "source_text": "存储至少 100 GiB",
        }
        guard = {
            "always": True,
            "require_context_constraint_coverage": {
                "source_fields": ["candidate.hard_constraints"],
                "checks_field": "hard_constraint_checks",
                "deployment_parameters_field": "deployment_parameters",
            },
        }
        conclusion = {
            "deployment_parameters": {"DBInstanceStorage": 120},
            "hard_constraint_checks": [
                {
                    "constraint": constraint,
                    "status": "satisfied",
                    "actual_value": 120,
                    "actual_unit": "GiB",
                    "parameter_values": {"DBInstanceStorage": 120},
                    "evidence": [
                        {
                            "type": "tool",
                            "summary": "RDS storage",
                            "tool_name": "aliyun_api",
                            "product": "rds",
                            "action": "DescribeDBInstanceAttribute",
                            "result_path": "Items.0.Storage",
                            "actual_value": 120,
                        }
                    ],
                }
            ],
        }
        tool = CompleteStepTool(
            StepConfig(step_id="cost_estimating", conclusion_field="cost", forward=None),
            completion_guards=[guard],
            completion_guard_state={
                "context_snapshot": {
                    "candidate": {"hard_constraints": [constraint]},
                },
                "tool_result_records": [
                    {
                        "tool_name": "aliyun_api",
                        "input": {"product": "rds", "action": "DescribeDBInstanceAttribute"},
                        "result": {"Items": [{"Storage": 120}]},
                        "is_error": False,
                    }
                ],
            },
        )

        assert tool.validate_completion_input({"conclusion": conclusion}) is None

        without_tool_evidence = dict(conclusion)
        direct_check = dict(conclusion["hard_constraint_checks"][0])
        direct_check["status"] = "unresolved"
        direct_check["evidence"] = [{"type": "context", "summary": "copied requirement", "actual_value": 120}]
        without_tool_evidence["hard_constraint_checks"] = [direct_check]
        error = tool.validate_completion_input({"conclusion": without_tool_evidence})
        assert error is not None
        assert "missing_tool_evidence" in error

        mismatched = dict(conclusion)
        mismatched["deployment_parameters"] = {"DBInstanceStorage": 80}
        mismatched_check = dict(conclusion["hard_constraint_checks"][0])
        mismatched_check["status"] = "unresolved"
        mismatched["hard_constraint_checks"] = [mismatched_check]
        error = tool.validate_completion_input({"conclusion": mismatched})
        assert error is not None
        assert "constraint_parameter_mismatch" in error
        assert "DBInstanceStorage" in error

        second_constraint = {
            **constraint,
            "id": "db-engine-version",
            "property": "version",
            "operator": "eq",
            "value": "8.0",
            "unit": None,
            "source_text": "MySQL 8.0",
        }
        multi_issue_tool = CompleteStepTool(
            StepConfig(step_id="cost_estimating", conclusion_field="cost", forward=None),
            completion_guards=[guard],
            completion_guard_state={
                "context_snapshot": {"candidate": {"hard_constraints": [constraint, second_constraint]}},
                "tool_result_records": tool._completion_guard_state["tool_result_records"],
            },
        )
        error = multi_issue_tool.validate_completion_input({"conclusion": mismatched})
        assert error is not None
        assert "multiple_constraint_issues" in error
        assert "constraint_parameter_mismatch[db-storage-min, DBInstanceStorage]" in error
        assert "missing_constraint_check[db-engine-version]" in error

    @pytest.mark.asyncio
    async def test_constraint_guard_returns_repairable_error_before_failing_step(self):
        constraint = {
            "id": "node-count",
            "target": "Service",
            "property": "count",
            "operator": "eq",
            "value": 2,
            "unit": "count",
            "verification_mode": "direct",
            "source": "user",
            "source_text": "部署两个节点",
        }
        tool = CompleteStepTool(
            StepConfig(
                step_id="cost_estimating",
                conclusion_field="cost",
                forward=None,
                max_conclusion_retries=2,
            ),
            completion_guards=[
                {
                    "always": True,
                    "require_context_constraint_coverage": {
                        "source_fields": ["candidate.hard_constraints"],
                        "checks_field": "hard_constraint_checks",
                        "deployment_parameters_field": "deployment_parameters",
                    },
                }
            ],
            completion_guard_state={"context_snapshot": {"candidate": {"hard_constraints": [constraint]}}},
        )
        invalid_conclusion = {"deployment_parameters": {"NodeCount": 1}, "hard_constraint_checks": []}

        first = await tool.execute(tool_input={"conclusion": invalid_conclusion}, context=ToolContext())
        second = await tool.execute(tool_input={"conclusion": invalid_conclusion}, context=ToolContext())

        assert first.is_error is True
        assert first.metadata is None
        assert "fix it and call complete_step again" in first.content
        assert "missing_constraint_check" in first.content
        assert second.is_error is True
        assert second.metadata is None

        valid_conclusion = {
            "deployment_parameters": {"NodeCount": 2},
            "hard_constraint_checks": [
                {
                    "constraint": constraint,
                    "status": "satisfied",
                    "actual_value": 2,
                    "parameter_values": {"NodeCount": 2},
                    "evidence": [
                        {
                            "type": "template",
                            "summary": "NodeCount parameter",
                            "actual_value": 2,
                        }
                    ],
                }
            ],
        }
        repaired = await tool.execute(tool_input={"conclusion": valid_conclusion}, context=ToolContext())

        assert repaired.is_error is False
        assert repaired.metadata["step_result"].conclusion == valid_conclusion

        terminal_tool = CompleteStepTool(
            StepConfig(
                step_id="cost_estimating",
                conclusion_field="cost",
                forward=None,
                max_conclusion_retries=1,
            ),
            completion_guards=tool._completion_guards,
            completion_guard_state={"context_snapshot": {"candidate": {"hard_constraints": [constraint]}}},
        )
        await terminal_tool.execute(tool_input={"conclusion": invalid_conclusion}, context=ToolContext())
        terminal = await terminal_tool.execute(tool_input={"conclusion": invalid_conclusion}, context=ToolContext())

        assert terminal.is_error is True
        assert terminal.metadata["step_result"].status is StepStatus.FAILED
        assert "missing_constraint_check" in terminal.metadata["step_result"].error

    @staticmethod
    def _deploying_success_guard() -> dict:
        return {
            "when_conclusion_field_equals": {"status": "success"},
            "required_conclusion_field": "stack_id",
            "require_tool_result": {
                "tool": "ros_stack",
                "action_in": ["CreateStack", "ContinueCreateStack"],
                "is_success": True,
                "status_in": ["CREATE_COMPLETE"],
                "match_conclusion_field": "stack_id",
            },
            "message": "部署成功必须等待 ros_stack CreateStack 返回 CREATE_COMPLETE。",
        }

    @staticmethod
    def _deploying_tool(result_records: list[dict] | None = None) -> CompleteStepTool:
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["status"],
                "additionalProperties": False,
                "properties": {
                    "stack_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["success", "failed", "cancelled"]},
                    "error": {"type": "string"},
                },
            },
        )
        return CompleteStepTool(
            config,
            completion_guards=[TestCompletionGuards._deploying_success_guard()],
            completion_guard_state={
                "successful_tools": set(),
                "tool_results": {},
                "tool_result_records": list(result_records or []),
            },
        )

    @staticmethod
    def _review_guards() -> list[dict]:
        return [
            {
                "when_conclusion_field_equals": {"validated": True},
                "require_tool_result": {
                    "tool": "ros_validate_template",
                    "match_conclusion_field": "file_path",
                    "match_result_field": "input.template_url",
                },
            },
            {
                "when_conclusion_field_equals": {"review_passed": True},
                "require_tool_result": {
                    "tool": "infraguard_scan",
                    "latest_match": True,
                    "after_tool_result": {
                        "tool": "ros_validate_template",
                        "match_conclusion_field": "file_path",
                        "match_result_field": "input.template_url",
                    },
                    "match_conclusion_field": "file_path",
                    "match_result_field": "file_path",
                    "result_field_equals": {"passed": True, "blocking_findings": 0},
                },
            },
        ]

    @staticmethod
    def _review_tool(result_records: list[dict] | None = None) -> CompleteStepTool:
        config = StepConfig(
            step_id="reviewing",
            conclusion_field="review",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["file_path", "validated", "review_passed"],
                "additionalProperties": False,
                "properties": {
                    "file_path": {"type": "string"},
                    "validated": {"type": "boolean"},
                    "review_passed": {"type": "boolean"},
                },
            },
        )
        return CompleteStepTool(
            config,
            completion_guards=TestCompletionGuards._review_guards(),
            completion_guard_state={
                "successful_tools": set(),
                "tool_results": {},
                "tool_result_records": list(result_records or []),
            },
        )

    @staticmethod
    def _selling_review_tool(result_records: list[dict] | None = None) -> CompleteStepTool:
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        selling_dir = Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"
        loaded = load_pipeline_dir(selling_dir, feature_flag_overrides={"enable_reviewing": True})
        review_step = next(
            step for step in loaded.sub_pipelines["evaluate_candidate"].steps if step.step_id == "reviewing"
        )
        config = StepConfig(
            step_id=review_step.step_id,
            conclusion_field=review_step.conclusion_field,
            forward=review_step.forward,
            conclusion_schema=review_step.conclusion_schema,
        )
        return CompleteStepTool(
            config,
            completion_guards=review_step.completion_guards,
            completion_guard_state={
                "successful_tools": set(),
                "tool_results": {},
                "tool_result_records": list(result_records or []),
            },
        )

    @staticmethod
    def _validate_template_record(
        file_path: str = "oss://bucket/template.yaml",
        *,
        result: dict | None = None,
    ) -> dict:
        return {
            "tool_name": "ros_validate_template",
            "input": {"template_url": file_path},
            "result": {"Description": "Valid"} if result is None else result,
            "is_error": False,
        }

    @staticmethod
    def _infraguard_scan_record(
        file_path: str = "oss://bucket/template.yaml",
        *,
        passed: bool = True,
        blocking_findings: int = 0,
        file_sha256: str | None = None,
        file_content: str | None = None,
    ) -> dict:
        result = {
            "file_path": file_path,
            "passed": passed,
            "blocking_findings": blocking_findings,
            "findings": [],
            "summary": {},
            "selected_aspects": ["security"],
            "expanded_policies": ["pack:aliyun:security"],
        }
        if file_sha256 is not None:
            result["file_sha256"] = file_sha256
        if file_content is not None:
            result["file_content"] = file_content
        return {
            "tool_name": "infraguard_scan",
            "input": {"file_path": file_path},
            "result": result,
            "is_error": False,
        }

    @pytest.mark.asyncio
    async def test_required_tool_result_rejects_deploying_success_without_create_stack_result(self):
        tool = self._deploying_tool()

        result = await tool.execute(
            tool_input={"conclusion": {"status": "success", "stack_id": "stack-123"}},
            context=ToolContext(),
        )

        assert result.is_error
        assert "CreateStack" in result.content
        assert "CREATE_COMPLETE" in result.content

    @pytest.mark.asyncio
    async def test_required_tool_result_rejects_deploying_success_when_stack_creation_failed(self):
        tool = self._deploying_tool(
            [
                {
                    "tool_name": "ros_stack",
                    "input": {"action": "CreateStack", "params": {"StackName": "demo"}},
                    "result": {"stack_id": "stack-123", "status": "CREATE_FAILED", "is_success": False},
                    "is_error": True,
                }
            ]
        )

        result = await tool.execute(
            tool_input={"conclusion": {"status": "success", "stack_id": "stack-123"}},
            context=ToolContext(),
        )

        assert result.is_error
        assert "CREATE_COMPLETE" in result.content

    @pytest.mark.asyncio
    async def test_required_tool_result_rejects_deploying_success_when_stack_id_mismatches(self):
        tool = self._deploying_tool(
            [
                {
                    "tool_name": "ros_stack",
                    "input": {"action": "CreateStack", "params": {"StackName": "demo"}},
                    "result": {"stack_id": "stack-123", "status": "CREATE_COMPLETE", "is_success": True},
                    "is_error": False,
                }
            ]
        )

        result = await tool.execute(
            tool_input={"conclusion": {"status": "success", "stack_id": "stack-other"}},
            context=ToolContext(),
        )

        assert result.is_error
        assert "stack_id" in result.content

    @pytest.mark.asyncio
    async def test_required_tool_result_accepts_matching_deploying_success(self):
        tool = self._deploying_tool(
            [
                {
                    "tool_name": "ros_stack",
                    "input": {"action": "CreateStack", "params": {"StackName": "demo"}},
                    "result": {"stack_id": "stack-123", "status": "CREATE_COMPLETE", "is_success": True},
                    "is_error": False,
                }
            ]
        )

        result = await tool.execute(
            tool_input={"conclusion": {"status": "success", "stack_id": "stack-123"}},
            context=ToolContext(),
        )

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_required_tool_result_accepts_matching_continue_create_stack_success(self):
        tool = self._deploying_tool(
            [
                {
                    "tool_name": "ros_stack",
                    "input": {"action": "ContinueCreateStack", "params": {"StackName": "demo"}},
                    "result": {"stack_id": "stack-123", "status": "CREATE_COMPLETE", "is_success": True},
                    "is_error": False,
                }
            ]
        )

        result = await tool.execute(
            tool_input={"conclusion": {"status": "success", "stack_id": "stack-123"}},
            context=ToolContext(),
        )

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_required_tool_result_accepts_matching_ros_deploy_wait_success(self):
        config = StepConfig(
            step_id="deploying",
            conclusion_field="deployment",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["status", "stack_id"],
                "properties": {
                    "stack_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["success", "failed", "cancelled"]},
                },
            },
        )
        tool = CompleteStepTool(
            config,
            completion_guards=[
                {
                    "when_conclusion_field_equals": {"status": "success"},
                    "required_conclusion_field": "stack_id",
                    "require_tool_result": {
                        "tool": "ros_deploy",
                        "action_in": ["create", "continue_create", "delete_and_create", "wait"],
                        "is_success": True,
                        "status_in": ["CREATE_COMPLETE"],
                        "match_conclusion_field": "stack_id",
                    },
                }
            ],
            completion_guard_state={
                "successful_tools": {"ros_deploy"},
                "tool_results": {},
                "tool_result_records": [
                    {
                        "tool_name": "ros_deploy",
                        "input": {"action": "wait", "stack_id": "stack-123"},
                        "result": {"stack_id": "stack-123", "status": "CREATE_COMPLETE", "is_success": True},
                        "is_error": False,
                    }
                ],
            },
        )

        result = await tool.execute(
            tool_input={"conclusion": {"status": "success", "stack_id": "stack-123"}},
            context=ToolContext(),
        )

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_required_tool_result_rejects_non_matching_stack_action(self):
        tool = self._deploying_tool(
            [
                {
                    "tool_name": "ros_stack",
                    "input": {"action": "UpdateStack", "params": {"StackName": "demo"}},
                    "result": {"stack_id": "stack-123", "status": "CREATE_COMPLETE", "is_success": True},
                    "is_error": False,
                }
            ]
        )

        result = await tool.execute(
            tool_input={"conclusion": {"status": "success", "stack_id": "stack-123"}},
            context=ToolContext(),
        )

        assert result.is_error
        assert "CreateStack" in result.content
        assert "ContinueCreateStack" in result.content

    @pytest.mark.asyncio
    async def test_required_tool_result_does_not_block_failed_deploying_conclusion(self):
        tool = self._deploying_tool()

        result = await tool.execute(
            tool_input={"conclusion": {"status": "failed", "error": "CREATE_FAILED"}},
            context=ToolContext(),
        )

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_required_conclusion_any_of_accepts_clarification_text(self):
        config = StepConfig(step_id="intent_parsing", conclusion_field="intent", forward=None)
        tool = CompleteStepTool(
            config,
            completion_guards=[
                {
                    "require_tool": "ask_user_question",
                    "when_user_message_matches_any": ["项目.*上线"],
                    "required_conclusion_any_of": ["clarification_choice", "clarification_text"],
                    "copy_tool_result_to_conclusion": {
                        "selected_id": "clarification_choice",
                        "free_text": "clarification_text",
                    },
                }
            ],
            completion_guard_state={
                "successful_tools": {"ask_user_question"},
                "tool_results": {
                    "ask_user_question": {
                        "selected_id": "",
                        "selected_label": "",
                        "free_text": "nginx 网站，日访问 1 万",
                    }
                },
            },
            user_message="我有个项目想上线",
        )
        tool_input = {"conclusion": {"is_infra_intent": True, "confidence": "medium"}}

        result = await tool.execute(tool_input=tool_input, context=ToolContext())

        assert not result.is_error
        conclusion = result.metadata["step_result"].conclusion
        assert conclusion["clarification_text"] == "nginx 网站，日访问 1 万"
        assert "clarification_choice" not in conclusion

    @pytest.mark.asyncio
    async def test_required_conclusion_any_of_rejects_missing_clarification_result(self):
        config = StepConfig(step_id="intent_parsing", conclusion_field="intent", forward=None)
        tool = CompleteStepTool(
            config,
            completion_guards=[
                {
                    "require_tool": "ask_user_question",
                    "when_user_message_matches_any": ["项目.*上线"],
                    "required_conclusion_any_of": ["clarification_choice", "clarification_text"],
                }
            ],
            completion_guard_state={"successful_tools": {"ask_user_question"}, "tool_results": {}},
            user_message="我有个项目想上线",
        )

        result = await tool.execute(
            tool_input={"conclusion": {"is_infra_intent": True, "confidence": "medium"}},
            context=ToolContext(),
        )

        assert result.is_error
        assert "clarification_choice" in result.content
        assert "clarification_text" in result.content

    @pytest.mark.asyncio
    async def test_review_guard_rejects_validated_conclusion_without_validate_template(self):
        tool = self._review_tool([self._infraguard_scan_record()])

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "oss://bucket/template.yaml",
                    "validated": True,
                    "review_passed": False,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "ros_validate_template" in result.content

    @pytest.mark.asyncio
    async def test_review_guard_rejects_review_passed_without_final_scan(self):
        tool = self._review_tool([self._validate_template_record()])

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "oss://bucket/template.yaml",
                    "validated": True,
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "infraguard_scan" in result.content

    @pytest.mark.asyncio
    async def test_review_guard_rejects_failed_scan_with_field_mismatch_message(self):
        tool = self._review_tool(
            [
                self._validate_template_record(),
                self._infraguard_scan_record(passed=False, blocking_findings=1),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "oss://bucket/template.yaml",
                    "validated": True,
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "passed" in result.content
        assert "True" in result.content
        assert "False" in result.content
        assert "Call infraguard_scan first" not in result.content

    @pytest.mark.asyncio
    async def test_review_guard_rejects_when_latest_scan_for_same_file_fails(self):
        tool = self._review_tool(
            [
                self._validate_template_record(),
                self._infraguard_scan_record(passed=True, blocking_findings=0),
                self._infraguard_scan_record(passed=False, blocking_findings=1),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "oss://bucket/template.yaml",
                    "validated": True,
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "passed" in result.content
        assert "False" in result.content

    @pytest.mark.asyncio
    async def test_review_guard_rejects_scan_that_precedes_validate_template(self):
        tool = self._review_tool(
            [
                self._infraguard_scan_record(passed=True, blocking_findings=0),
                self._validate_template_record(),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "oss://bucket/template.yaml",
                    "validated": True,
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "infraguard_scan" in result.content

    @pytest.mark.asyncio
    async def test_review_guard_rejects_wrong_scan_file_path(self):
        tool = self._review_tool(
            [
                self._validate_template_record(),
                self._infraguard_scan_record("oss://bucket/other.yaml"),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "oss://bucket/template.yaml",
                    "validated": True,
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "file_path" in result.content

    @pytest.mark.asyncio
    async def test_review_guard_accepts_ros_validate_template_for_same_file(self):
        tool = self._review_tool(
            [
                self._validate_template_record(),
                self._infraguard_scan_record(),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "oss://bucket/template.yaml",
                    "validated": True,
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_review_guard_accepts_validate_template_and_final_scan(self):
        tool = self._review_tool(
            [
                self._validate_template_record(),
                self._infraguard_scan_record(),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "oss://bucket/template.yaml",
                    "validated": True,
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_selling_review_guard_accepts_validate_template_response_without_is_success(self):
        template = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"
        template_sha256 = self._sha256(template)
        tool = self._selling_review_tool(
            [
                self._validate_template_record(result={"Description": "Valid"}),
                self._infraguard_scan_record(file_sha256=template_sha256, file_content=template),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "template": template,
                    "template_sha256": template_sha256,
                    "file_path": "oss://bucket/template.yaml",
                    "region": "cn-hangzhou",
                    "description": "valid template",
                    "validated": True,
                    "review_passed": True,
                    "review_issues": [],
                    "selected_review_aspects": [{"key": "security", "reason": "baseline"}],
                    "skipped_review_aspects": [],
                    "resolved_infraguard_policies": ["pack:aliyun:security"],
                    "infraguard_summary": {},
                    "fix_summary": "validated",
                }
            },
            context=ToolContext(),
        )

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_selling_review_guard_accepts_clean_initial_scan_without_validate_or_rescan(self):
        template = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"
        template_sha256 = self._sha256(template)
        tool = self._selling_review_tool(
            [
                self._infraguard_scan_record(
                    "/tmp/template.yaml",
                    passed=True,
                    blocking_findings=0,
                    file_sha256=template_sha256,
                    file_content=template,
                ),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "template": template,
                    "template_sha256": template_sha256,
                    "file_path": "/tmp/template.yaml",
                    "region": "cn-hangzhou",
                    "description": "clean template",
                    "validated": True,
                    "review_passed": True,
                    "review_issues": [],
                    "selected_review_aspects": [{"key": "security", "reason": "baseline"}],
                    "skipped_review_aspects": [],
                    "resolved_infraguard_policies": ["pack:aliyun:security"],
                    "infraguard_summary": {"passed": True, "blocking_findings": 0},
                    "fix_summary": "initial InfraGuard scan passed; no template edits were required",
                }
            },
            context=ToolContext(),
        )

        assert not result.is_error

    @pytest.mark.asyncio
    async def test_selling_review_guard_still_requires_validate_after_same_file_edit(self):
        template = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"
        template_sha256 = self._sha256(template)
        tool = self._selling_review_tool(
            [
                {
                    "tool_name": "edit_file",
                    "input": {"path": "/tmp/template.yaml"},
                    "result": {"file_path": "/tmp/template.yaml"},
                    "is_error": False,
                },
                self._infraguard_scan_record(
                    "/tmp/template.yaml",
                    passed=True,
                    blocking_findings=0,
                    file_sha256=template_sha256,
                    file_content=template,
                ),
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "template": template,
                    "template_sha256": template_sha256,
                    "file_path": "/tmp/template.yaml",
                    "region": "cn-hangzhou",
                    "description": "reviewed",
                    "validated": True,
                    "review_passed": True,
                    "review_issues": [],
                    "selected_review_aspects": [],
                    "skipped_review_aspects": [],
                    "resolved_infraguard_policies": [],
                    "infraguard_summary": {},
                    "fix_summary": "done",
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "ros_validate_template" in result.content

    @pytest.mark.asyncio
    async def test_selling_review_guard_rejects_same_file_edit_after_final_scan(self):
        template = "ROSTemplateFormatVersion: '2015-09-01'\n"
        template_sha256 = self._sha256(template)
        tool = self._selling_review_tool(
            [
                self._validate_template_record("/tmp/template.yaml"),
                self._infraguard_scan_record(
                    "/tmp/template.yaml",
                    passed=True,
                    blocking_findings=0,
                    file_sha256=template_sha256,
                    file_content=template,
                ),
                {
                    "tool_name": "edit_file",
                    "input": {"path": "/tmp/template.yaml"},
                    "result": {"file_path": "/tmp/template.yaml"},
                    "is_error": False,
                },
            ]
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "template": template,
                    "template_sha256": template_sha256,
                    "file_path": "/tmp/template.yaml",
                    "region": "cn-hangzhou",
                    "description": "reviewed",
                    "validated": True,
                    "review_passed": True,
                    "review_issues": [],
                    "selected_review_aspects": [],
                    "skipped_review_aspects": [],
                    "resolved_infraguard_policies": [],
                    "infraguard_summary": {},
                    "fix_summary": "done",
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "edit_file" in result.content
        assert "infraguard_scan" in result.content

    @pytest.mark.asyncio
    async def test_conclusion_sha256_guard_rejects_template_hash_mismatch(self):
        config = StepConfig(
            step_id="reviewing",
            conclusion_field="template",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["template", "template_sha256", "review_passed"],
                "additionalProperties": False,
                "properties": {
                    "template": {"type": "string"},
                    "template_sha256": {"type": "string"},
                    "review_passed": {"type": "boolean"},
                },
            },
        )
        tool = CompleteStepTool(
            config,
            completion_guards=[
                {
                    "when_conclusion_field_equals": {"review_passed": True},
                    "require_conclusion_sha256": {
                        "content_field": "template",
                        "sha256_field": "template_sha256",
                    },
                }
            ],
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "template": "actual template\n",
                    "template_sha256": self._sha256("stale template\n"),
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "template_sha256" in result.content

    @pytest.mark.asyncio
    async def test_required_tool_result_rejects_template_hash_mismatch_with_scan(self):
        template = "ROSTemplateFormatVersion: '2015-09-01'\n"
        config = StepConfig(
            step_id="reviewing",
            conclusion_field="template",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["file_path", "template_sha256", "review_passed"],
                "additionalProperties": False,
                "properties": {
                    "file_path": {"type": "string"},
                    "template_sha256": {"type": "string"},
                    "review_passed": {"type": "boolean"},
                },
            },
        )
        tool = CompleteStepTool(
            config,
            completion_guards=[
                {
                    "when_conclusion_field_equals": {"review_passed": True},
                    "require_tool_result": {
                        "tool": "infraguard_scan",
                        "match_fields": [
                            {"conclusion_field": "file_path", "result_field": "file_path"},
                            {"conclusion_field": "template_sha256", "result_field": "file_sha256"},
                        ],
                        "result_field_equals": {"passed": True, "blocking_findings": 0},
                    },
                }
            ],
            completion_guard_state={
                "successful_tools": set(),
                "tool_results": {},
                "tool_result_records": [
                    {
                        "tool_name": "infraguard_scan",
                        "input": {"file_path": "template.yaml"},
                        "result": {
                            "file_path": "template.yaml",
                            "file_sha256": self._sha256(template),
                            "passed": True,
                            "blocking_findings": 0,
                        },
                        "is_error": False,
                    }
                ],
            },
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "template.yaml",
                    "template_sha256": self._sha256("different template\n"),
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "template_sha256" in result.content

    @pytest.mark.asyncio
    async def test_required_tool_result_rejects_missing_required_result_fields(self):
        template = "ROSTemplateFormatVersion: '2015-09-01'\n"
        template_sha256 = self._sha256(template)
        config = StepConfig(
            step_id="reviewing",
            conclusion_field="template",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["file_path", "template_sha256", "review_passed"],
                "additionalProperties": False,
                "properties": {
                    "file_path": {"type": "string"},
                    "template_sha256": {"type": "string"},
                    "review_passed": {"type": "boolean"},
                },
            },
        )
        tool = CompleteStepTool(
            config,
            completion_guards=[
                {
                    "when_conclusion_field_equals": {"review_passed": True},
                    "require_tool_result": {
                        "tool": "infraguard_scan",
                        "match_fields": [
                            {"conclusion_field": "file_path", "result_field": "file_path"},
                            {"conclusion_field": "template_sha256", "result_field": "file_sha256"},
                        ],
                        "result_field_equals": {"passed": True, "blocking_findings": 0},
                        "required_result_fields": ["selected_aspects", "expanded_policies"],
                    },
                }
            ],
            completion_guard_state={
                "successful_tools": set(),
                "tool_results": {},
                "tool_result_records": [
                    {
                        "tool_name": "infraguard_scan",
                        "input": {"file_path": "template.yaml"},
                        "result": {
                            "file_path": "template.yaml",
                            "file_sha256": template_sha256,
                            "passed": True,
                            "blocking_findings": 0,
                        },
                        "is_error": False,
                    }
                ],
            },
        )

        result = await tool.execute(
            tool_input={
                "conclusion": {
                    "file_path": "template.yaml",
                    "template_sha256": template_sha256,
                    "review_passed": True,
                }
            },
            context=ToolContext(),
        )

        assert result.is_error
        assert "selected_aspects" in result.content

    @pytest.mark.asyncio
    async def test_disallowed_file_mutation_matches_relative_and_absolute_paths(self, tmp_path):
        config = StepConfig(
            step_id="reviewing",
            conclusion_field="template",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["file_path", "review_passed"],
                "additionalProperties": False,
                "properties": {
                    "file_path": {"type": "string"},
                    "review_passed": {"type": "boolean"},
                },
            },
        )
        absolute_template = tmp_path / "template.yaml"
        tool = CompleteStepTool(
            config,
            completion_guards=[
                {
                    "when_conclusion_field_equals": {"review_passed": True},
                    "require_tool_result": {
                        "tool": "infraguard_scan",
                        "latest_match": True,
                        "match_conclusion_field": "file_path",
                        "match_result_field": "file_path",
                        "result_field_equals": {"passed": True, "blocking_findings": 0},
                        "disallow_tool_results_after_match": [
                            {
                                "tools": ["write_file"],
                                "match_conclusion_field": "file_path",
                                "match_result_field": "result.file_path",
                                "message": "rerun validation",
                            }
                        ],
                    },
                }
            ],
            completion_guard_state={
                "cwd": str(tmp_path),
                "successful_tools": set(),
                "tool_results": {},
                "tool_result_records": [
                    {
                        "tool_name": "infraguard_scan",
                        "input": {"file_path": "template.yaml"},
                        "result": {"file_path": "template.yaml", "passed": True, "blocking_findings": 0},
                        "is_error": False,
                    },
                    {
                        "tool_name": "write_file",
                        "input": {"path": str(absolute_template)},
                        "result": {"file_path": str(absolute_template)},
                        "is_error": False,
                    },
                ],
            },
        )

        result = await tool.execute(
            tool_input={"conclusion": {"file_path": "template.yaml", "review_passed": True}},
            context=ToolContext(),
        )

        assert result.is_error
        assert "rerun validation" in result.content


class TestSchemaValidation:
    def test_missing_conclusion_validation_error_includes_current_step_schema(self):
        config = StepConfig(
            step_id="architecture_planning",
            conclusion_field="architecture",
            forward="evaluate_candidates",
            conclusion_schema={
                "type": "object",
                "required": ["candidates"],
                "properties": {
                    "candidates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "topology"],
                            "properties": {
                                "name": {"type": "string"},
                                "topology": {"type": "string"},
                            },
                        },
                    }
                },
            },
        )
        tool = CompleteStepTool(config)

        valid, error = tool.validate_input({})

        assert not valid
        assert "Architecture planning" in error
        assert "candidates" in error
        assert '{"conclusion"' in error
        assert "is_infra_intent" not in error

    @pytest.mark.asyncio
    async def test_valid_conclusion_passes(self):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        )
        tool = CompleteStepTool(config)
        result = await tool.execute(
            tool_input={"conclusion": {"name": "hello"}},
            context=ToolContext(),
        )
        assert not result.is_error
        assert "step_result" in result.metadata

    @pytest.mark.asyncio
    async def test_invalid_conclusion_returns_error(self):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        )
        tool = CompleteStepTool(config)
        result = await tool.execute(
            tool_input={"conclusion": {"wrong_field": 123}},
            context=ToolContext(),
        )
        assert result.is_error
        assert "name" in result.content
        assert result.metadata is None

    def test_invalid_tool_input_error_preserves_previous_invalid_input_for_llm_retry(self):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        )
        tool = CompleteStepTool(config)
        valid, error = tool.validate_input(
            {
                "conclusion": {"wrong_field": 123},
                "admin_token": "tok-completestepsecret123",
                "config_path": r"C:\Users\Alice Smith\.iac-code\settings.yml",
            }
        )

        assert not valid
        assert "tok-completestepsecret123" in error
        assert "config_path" in error
        assert "Alice Smith" in error
        assert "settings.yml" in error
        assert "[REDACTED]" not in error
        assert "[PATH]" not in error

    def test_invalid_conclusion_schema_error_preserves_value_for_llm_retry(self):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["admin_token"],
                "properties": {"admin_token": {"type": "integer"}},
            },
        )
        tool = CompleteStepTool(config)

        valid, error = tool.validate_input({"conclusion": {"admin_token": "tok-completestepsecret123"}})

        assert not valid
        assert "tok-completestepsecret123" in error
        assert "[REDACTED]" not in error

    @pytest.mark.asyncio
    async def test_execute_schema_error_preserves_functional_value_but_strictly_sanitizes_log(
        self, caplog: pytest.LogCaptureFixture
    ):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["admin_token"],
                "properties": {"admin_token": {"type": "integer"}},
            },
            max_conclusion_retries=1,
        )
        tool = CompleteStepTool(config)

        r1 = await tool.execute(
            tool_input={"conclusion": {"admin_token": "tok-plainsecret123"}},
            context=ToolContext(),
        )
        r2 = await tool.execute(
            tool_input={"conclusion": {"admin_token": "tok-plainsecret123"}},
            context=ToolContext(),
        )

        assert r1.is_error
        assert r2.is_error
        assert "tok-plainsecret123" in r1.content
        assert "tok-plainsecret123" in r2.content
        assert "tok-plainsecret123" in r2.metadata["step_result"].error
        assert "tok-plainsecret123" not in caplog.text
        assert "validator=type" in caplog.text

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_fix(self):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["count"],
                "properties": {"count": {"type": "integer"}},
            },
            max_conclusion_retries=2,
        )
        tool = CompleteStepTool(config)
        # First call: invalid
        r1 = await tool.execute(tool_input={"conclusion": {"count": "not_int"}}, context=ToolContext())
        assert r1.is_error
        # Second call: valid
        r2 = await tool.execute(tool_input={"conclusion": {"count": 42}}, context=ToolContext())
        assert not r2.is_error
        assert r2.metadata["step_result"].conclusion == {"count": 42}

    @pytest.mark.asyncio
    async def test_exceeds_max_retries_marks_failed(self):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["x"],
                "properties": {"x": {"type": "boolean"}},
            },
            max_conclusion_retries=1,
        )
        tool = CompleteStepTool(config)
        # First call: invalid (attempt 1)
        r1 = await tool.execute(tool_input={"conclusion": {}}, context=ToolContext())
        assert r1.is_error
        assert r1.metadata is None
        # Second call: still invalid (attempt 2 = max_retries exceeded)
        r2 = await tool.execute(tool_input={"conclusion": {}}, context=ToolContext())
        assert r2.is_error
        assert r2.metadata is not None
        assert r2.metadata["step_result"].status == StepStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_validation_when_no_schema(self):
        config = StepConfig(step_id="test", conclusion_field="out", forward=None)
        tool = CompleteStepTool(config)
        result = await tool.execute(
            tool_input={"conclusion": {"anything": "goes"}},
            context=ToolContext(),
        )
        assert not result.is_error


class TestNullNormalization:
    """LLMs pass null for optional fields — normalization strips them before validation."""

    def test_null_optional_fields_stripped_before_validation(self):
        config = StepConfig(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward="arch",
            conclusion_schema={
                "type": "object",
                "required": ["is_infra_intent", "confidence"],
                "additionalProperties": False,
                "properties": {
                    "is_infra_intent": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {"type": "string"},
                    "budget_constraint": {"type": "string"},
                },
            },
        )
        tool = CompleteStepTool(config)
        tool_input = {
            "conclusion": {
                "is_infra_intent": True,
                "confidence": "high",
                "category": None,
                "budget_constraint": None,
            }
        }
        valid, error = tool.validate_input(tool_input)
        assert valid, f"Expected valid but got: {error}"
        assert "category" not in tool_input["conclusion"]
        assert "budget_constraint" not in tool_input["conclusion"]

    def test_non_null_values_preserved(self):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        )
        tool = CompleteStepTool(config)
        tool_input = {"conclusion": {"name": "hello", "note": "world"}}
        valid, _ = tool.validate_input(tool_input)
        assert valid
        assert tool_input["conclusion"]["note"] == "world"

    def test_null_required_field_still_fails(self):
        config = StepConfig(
            step_id="test",
            conclusion_field="out",
            forward=None,
            conclusion_schema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        )
        tool = CompleteStepTool(config)
        tool_input = {"conclusion": {"name": None}}
        valid, error = tool.validate_input(tool_input)
        assert not valid
        assert "name" in error
