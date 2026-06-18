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

    def test_invalid_tool_input_error_redacts_previous_invalid_input(self):
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
        assert "tok-completestepsecret123" not in error
        assert r"C:\Users" not in error
        assert r"Alice Smith\.iac-code" not in error
        assert "[REDACTED]" in error
        assert "[PATH]" in error

    def test_invalid_conclusion_schema_error_redacts_secret_value(self):
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
        assert "tok-completestepsecret123" not in error
        assert "[REDACTED]" in error

    @pytest.mark.asyncio
    async def test_execute_schema_error_redacts_sensitive_field_value(self):
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
        assert "tok-plainsecret123" not in r1.content
        assert "tok-plainsecret123" not in r2.content
        assert "[REDACTED]" in r1.content
        assert "[REDACTED]" in r2.metadata["step_result"].error

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
