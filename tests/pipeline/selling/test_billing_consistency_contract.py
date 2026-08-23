"""Acceptance coverage for the reported billing-consistency defect.

Session d10910d359354a6891f4d3efad68e3e2: the user explicitly asked for
pay-as-you-go (CDT) instances, only PrePaid could be priced, and cost_estimating
silently rewrote InstanceChargeType to PrePaid while also reporting CNY for a
USD quote. These tests pin the contract that makes that impossible.
"""

from pathlib import Path

import pytest
import yaml

from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.types import StepConfig

SELLING_DIR = Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"

USER_CHOICE = "需要后付费CDT的实例，加速区域带宽200M，ipv4"


def _cost_guards() -> list[dict]:
    raw = yaml.safe_load((SELLING_DIR / "pipeline.yaml").read_text(encoding="utf-8"))
    steps = raw["sub_pipelines"]["evaluate_candidate"]["steps"]
    return next(step for step in steps if step["id"] == "cost_estimating")["completion_guards"]


def _confirm_guards() -> list[dict]:
    raw = yaml.safe_load((SELLING_DIR / "pipeline.yaml").read_text(encoding="utf-8"))
    return next(step for step in raw["steps"] if step["id"] == "confirm_and_select")["completion_guards"]


def _cost_tool(guards: list[dict] | None = None) -> CompleteStepTool:
    return CompleteStepTool(
        StepConfig(step_id="cost_estimating", conclusion_field="cost", forward=None),
        completion_guards=guards if guards is not None else _cost_guards(),
        completion_guard_state={"context_snapshot": {}},
        user_message=USER_CHOICE,
    )


def _confirm_tool() -> CompleteStepTool:
    return CompleteStepTool(
        StepConfig(step_id="confirm_and_select", conclusion_field="selected_plan", forward="deploying"),
        completion_guards=_confirm_guards(),
        completion_guard_state={},
        user_message="选择方案0",
    )


def _cost_conclusion(**overrides) -> dict:
    conclusion = {
        "monthly_estimate": "¥96.80/月",
        "currency": "CNY",
        "resources": [{"type": "ALIYUN::ECS::InstanceGroup", "cost": "¥96.80/月"}],
        "template_fixed": False,
        "deployment_parameters": {"InstanceChargeType": "PrePaid", "ZoneId": "cn-hangzhou-k"},
        "hard_constraint_checks": [],
        "preview_validation": {"succeeded": False, "error": "missing VpcId"},
    }
    conclusion.update(overrides)
    return conclusion


class TestCostEstimatingRejectsSilentRewrite:
    def test_silent_rewrite_reported_as_consistent_is_rejected(self):
        """The exact regression: POSTPAY intent, PrePaid quote, claimed consistent."""
        conclusion = _cost_conclusion(
            billing_consistency={
                "user_intent_charge_type": "POSTPAY",
                "priced_charge_type": "PrePaid",
                "deployed_charge_type": "PrePaid",
                "priced_currency": "CNY",
                "consistent": True,
            }
        )

        error = _cost_tool().validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "billing_inconsistency_undisclosed" in error

    def test_rewrite_hidden_in_fix_summary_is_still_rejected(self):
        """fix_summary prose must not substitute for a structured disclosure."""
        conclusion = _cost_conclusion(
            template_fixed=True,
            fix_summary="将 InstanceChargeType 由 POSTPAY 调整为 PREPAY 以便询价",
            billing_consistency={
                "user_intent_charge_type": "POSTPAY",
                "priced_charge_type": "PrePaid",
                "deployed_charge_type": "PrePaid",
                "priced_currency": "CNY",
                "consistent": True,
            },
        )

        error = _cost_tool().validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "billing_inconsistency_undisclosed" in error

    def test_disclosure_must_match_deployment_parameters(self):
        """Claiming POSTPAY while the parameters say PrePaid is caught by code."""
        conclusion = _cost_conclusion(
            billing_consistency={
                "user_intent_charge_type": "POSTPAY",
                "priced_charge_type": "POSTPAY",
                "deployed_charge_type": "POSTPAY",
                "priced_currency": "CNY",
                "consistent": True,
            }
        )

        error = _cost_tool().validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "deployed_charge_type_mismatch" in error
        assert "InstanceChargeType" in error

    def test_inconsistency_must_request_user_confirmation(self):
        conclusion = _cost_conclusion(
            billing_consistency={
                "user_intent_charge_type": "POSTPAY",
                "priced_charge_type": "PrePaid",
                "deployed_charge_type": "PrePaid",
                "priced_currency": "CNY",
                "consistent": False,
            }
        )

        error = _cost_tool().validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "billing_user_confirmation_not_requested" in error

    def test_escalated_inconsistency_is_accepted(self):
        conclusion = _cost_conclusion(
            billing_consistency={
                "user_intent_charge_type": "POSTPAY",
                "priced_charge_type": "PrePaid",
                "deployed_charge_type": "PrePaid",
                "priced_currency": "CNY",
                "consistent": False,
                "user_confirmation_required": True,
                "inconsistencies": [
                    {
                        "party": "pricing",
                        "expected": "POSTPAY",
                        "actual": "PrePaid",
                        "reason": "该地域当前仅预付费可询价",
                    }
                ],
            }
        )

        assert _cost_tool().validate_completion_input({"conclusion": conclusion}) is None

    def test_cny_reported_for_a_usd_quote_is_rejected(self):
        """The international-site currency conflict from the same session."""
        conclusion = _cost_conclusion(
            currency="CNY",
            deployment_parameters={"InstanceChargeType": "PostPaid"},
            billing_consistency={
                "user_intent_charge_type": "POSTPAY",
                "priced_charge_type": "PostPaid",
                "deployed_charge_type": "PostPaid",
                "priced_currency": "USD",
                "consistent": True,
            },
        )

        error = _cost_tool().validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "currency_mismatch" in error

    def test_usd_quote_reported_as_usd_is_accepted(self):
        conclusion = _cost_conclusion(
            currency="USD",
            deployment_parameters={"InstanceChargeType": "PostPaid"},
            billing_consistency={
                "user_intent_charge_type": "POSTPAY",
                "priced_charge_type": "PostPaid",
                "deployed_charge_type": "PostPaid",
                "priced_currency": "USD",
                "consistent": True,
            },
        )

        assert _cost_tool().validate_completion_input({"conclusion": conclusion}) is None


class TestConfirmAndSelectRequiresSecondConfirmation:
    def _selection(self, **overrides) -> dict:
        conclusion = {
            "user_prompt": "该方案计费模式与您的选择不一致，请确认是否接受：",
            "options": [{"name": "方案A", "summary": "ECS + EIP", "candidate_index": 0}],
            "billing_notices": [
                {
                    "candidate_index": 0,
                    "user_intent_charge_type": "POSTPAY",
                    "priced_charge_type": "PrePaid",
                    "deployed_charge_type": "PrePaid",
                    "detail": "您选择后付费CDT，但该地域仅预付费可询价，将产生预付费账单",
                }
            ],
            "selected_candidate_index": 0,
            "selected_evaluated_candidate_index": 0,
            "user_input": "选择方案0",
        }
        conclusion.update(overrides)
        return conclusion

    def test_selection_without_confirmation_is_rejected(self):
        error = _confirm_tool().validate_completion_input({"conclusion": self._selection()})

        assert error is not None
        assert "missing_billing_confirmation" in error

    def test_selection_with_confirmation_is_accepted(self):
        conclusion = self._selection(
            billing_confirmation={
                "confirmed": True,
                "acknowledged_charge_type": "PrePaid",
                "user_input": "可以，接受预付费",
            }
        )

        assert _confirm_tool().validate_completion_input({"conclusion": conclusion}) is None

    def test_explicit_refusal_is_also_a_valid_decision(self):
        conclusion = self._selection(
            billing_confirmation={
                "confirmed": False,
                "acknowledged_charge_type": "POSTPAY",
                "user_input": "不接受，我要后付费",
            }
        )

        assert _confirm_tool().validate_completion_input({"conclusion": conclusion}) is None

    def test_plan_without_billing_notices_needs_no_confirmation(self):
        conclusion = self._selection(billing_notices=[])

        assert _confirm_tool().validate_completion_input({"conclusion": conclusion}) is None


class TestPipelineContractWiring:
    def test_selling_pipeline_wires_both_billing_guards(self):
        loaded = load_pipeline_dir(SELLING_DIR)

        cost_step = next(
            step for step in loaded.sub_pipelines["evaluate_candidate"].steps if step.step_id == "cost_estimating"
        )
        assert any("require_billing_consistency_disclosure" in guard for guard in cost_step.completion_guards)

        confirm_step = next(step for step in loaded.steps if step.step_id == "confirm_and_select")
        assert any("require_billing_confirmation" in guard for guard in confirm_step.completion_guards)

    def test_billing_fields_exist_on_every_confirm_surface(self):
        loaded = load_pipeline_dir(SELLING_DIR)
        confirm_step = next(step for step in loaded.steps if step.step_id == "confirm_and_select")

        assert confirm_step.conclusion_schema is not None
        schemas = [confirm_step.conclusion_schema]
        rich_override = confirm_step.surface_overrides["a2a_rich"].conclusion_schema
        assert rich_override is not None
        schemas.append(rich_override)
        for schema in schemas:
            assert "billing_notices" in schema["properties"]
            assert "billing_confirmation" in schema["properties"]

    def test_cost_schema_no_longer_hardcodes_cny(self):
        loaded = load_pipeline_dir(SELLING_DIR)
        cost_step = next(
            step for step in loaded.sub_pipelines["evaluate_candidate"].steps if step.step_id == "cost_estimating"
        )

        assert cost_step.conclusion_schema is not None
        currency = cost_step.conclusion_schema["properties"]["currency"]
        assert "enum" not in currency
        assert "billing_consistency" in cost_step.conclusion_schema["required"]


@pytest.mark.parametrize(
    "guard_key",
    ["require_billing_consistency_disclosure", "require_billing_confirmation"],
)
def test_new_guard_keys_are_accepted_by_the_loader(guard_key):
    """A guard key the loader does not know about raises at load time."""
    from iac_code.pipeline.engine.loader import _SUPPORTED_COMPLETION_GUARD_KEYS

    assert guard_key in _SUPPORTED_COMPLETION_GUARD_KEYS
