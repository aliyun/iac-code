from iac_code.pipeline.engine.types import RollbackRule, StepConfig, StepResult, StepStatus


class TestStepStatus:
    def test_enum_values(self):
        assert StepStatus.PENDING == "pending"
        assert StepStatus.RUNNING == "running"
        assert StepStatus.COMPLETED == "completed"
        assert StepStatus.STALE == "stale"
        assert StepStatus.FAILED == "failed"

    def test_is_str_enum(self):
        assert isinstance(StepStatus.PENDING, str)


class TestRollbackRule:
    def test_basic_construction(self):
        rule = RollbackRule(target_step="intent_parsing", condition="user_request")
        assert rule.target_step == "intent_parsing"
        assert rule.condition == "user_request"
        assert rule.invalidates == []

    def test_with_invalidates(self):
        rule = RollbackRule(
            target_step="architecture_planning",
            condition="cost_too_high",
            invalidates=["specs", "template"],
        )
        assert rule.invalidates == ["specs", "template"]


class TestStepConfig:
    def test_defaults(self):
        config = StepConfig(
            step_id="test_step",
            conclusion_field="test_field",
            forward="next_step",
        )
        assert config.auto_advance is True
        assert config.max_agent_turns == 50
        assert config.rollback_rules == []

    def test_custom_values(self):
        rules = [RollbackRule(target_step="prev", condition="wrong")]
        config = StepConfig(
            step_id="my_step",
            conclusion_field="my_field",
            forward=None,
            rollback_rules=rules,
            auto_advance=False,
            max_agent_turns=20,
        )
        assert config.forward is None
        assert config.auto_advance is False
        assert config.max_agent_turns == 20
        assert len(config.rollback_rules) == 1


class TestStepResult:
    def test_completed(self):
        result = StepResult(
            step_id="intent_parsing",
            status=StepStatus.COMPLETED,
            conclusion={"intent": "e-commerce"},
        )
        assert result.rollback_request is None
        assert result.error is None

    def test_with_rollback_request(self):
        result = StepResult(
            step_id="cost_estimating",
            status=StepStatus.COMPLETED,
            conclusion={"cost": 1500},
            rollback_request=("spec_recommending", "cost_too_high"),
        )
        target, reason = result.rollback_request
        assert target == "spec_recommending"
        assert reason == "cost_too_high"

    def test_failed(self):
        result = StepResult(
            step_id="deploying",
            status=StepStatus.FAILED,
            error="deployment timeout",
        )
        assert result.conclusion is None
