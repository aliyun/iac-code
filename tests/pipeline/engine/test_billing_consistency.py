from iac_code.pipeline.engine.billing_consistency import (
    charge_types_in_parameters,
    normalize_charge_type,
    validate_billing_confirmation,
    validate_billing_consistency,
    validate_priced_currency,
)


def _codes(issues) -> list[str]:
    return [issue.code for issue in issues]


class TestNormalizeChargeType:
    def test_prepaid_spellings_collapse(self):
        for value in ["PrePaid", "Prepaid", "PREPAY", "PRE", "Subscription", "pre-paid", "包年包月"]:
            assert normalize_charge_type(value) == "prepaid", value

    def test_postpaid_spellings_collapse(self):
        for value in ["PostPaid", "Postpaid", "POSTPAY", "POST", "PayAsYouGo", "PayOnDemand", "按量付费", "CDT"]:
            assert normalize_charge_type(value) == "postpaid", value

    def test_unknown_and_empty_values_are_not_classified(self):
        assert normalize_charge_type("") is None
        assert normalize_charge_type(None) is None
        assert normalize_charge_type(True) is None
        assert normalize_charge_type("SomethingElse") is None


class TestChargeTypesInParameters:
    def test_detects_billing_parameters_by_name(self):
        found = charge_types_in_parameters(
            {
                "InstanceChargeType": "PrePaid",
                "PayType": "PostPaid",
                "ZoneId": "cn-hangzhou-k",
            }
        )
        assert found == {"InstanceChargeType": "prepaid", "PayType": "postpaid"}

    def test_ignores_non_billing_and_unclassifiable_values(self):
        assert charge_types_in_parameters({"ZoneId": "cn-hangzhou-k"}) == {}
        assert charge_types_in_parameters({"InstanceChargeType": "Weird"}) == {}
        assert charge_types_in_parameters(None) == {}


class TestValidateBillingConsistency:
    def _consistent_disclosure(self, charge_type: str = "PostPaid") -> dict:
        return {
            "user_intent_charge_type": charge_type,
            "priced_charge_type": charge_type,
            "deployed_charge_type": charge_type,
            "priced_currency": "CNY",
            "consistent": True,
        }

    def test_accepts_a_genuinely_consistent_disclosure(self):
        issues = validate_billing_consistency(
            self._consistent_disclosure(),
            {"InstanceChargeType": "PostPaid"},
        )
        assert issues == []

    def test_accepts_differing_spellings_of_the_same_mode(self):
        disclosure = {
            "user_intent_charge_type": "POSTPAY",
            "priced_charge_type": "PayAsYouGo",
            "deployed_charge_type": "PostPaid",
            "priced_currency": "CNY",
            "consistent": True,
        }
        assert validate_billing_consistency(disclosure, {"InstanceChargeType": "Postpaid"}) == []

    def test_rejects_missing_disclosure(self):
        assert _codes(validate_billing_consistency(None, {})) == ["missing_billing_disclosure"]

    def test_rejects_missing_required_fields(self):
        issues = validate_billing_consistency({"consistent": True}, {})
        assert _codes(issues) == ["missing_billing_field"] * 3

    def test_rejects_claiming_consistency_while_silently_rewriting(self):
        disclosure = {
            "user_intent_charge_type": "POSTPAY",
            "priced_charge_type": "PrePaid",
            "deployed_charge_type": "PrePaid",
            "priced_currency": "CNY",
            "consistent": True,
        }
        issues = validate_billing_consistency(disclosure, {"InstanceChargeType": "PrePaid"})
        assert "billing_inconsistency_undisclosed" in _codes(issues)

    def test_rejects_declared_mode_that_contradicts_deployment_parameters(self):
        disclosure = self._consistent_disclosure("PostPaid")
        issues = validate_billing_consistency(disclosure, {"InstanceChargeType": "PrePaid"})
        assert "deployed_charge_type_mismatch" in _codes(issues)
        assert any("InstanceChargeType" in issue.detail for issue in issues)

    def test_disclosed_inconsistency_must_request_confirmation(self):
        disclosure = {
            "user_intent_charge_type": "POSTPAY",
            "priced_charge_type": "PrePaid",
            "deployed_charge_type": "PrePaid",
            "priced_currency": "CNY",
            "consistent": False,
        }
        issues = validate_billing_consistency(disclosure, {"InstanceChargeType": "PrePaid"})
        assert _codes(issues) == [
            "billing_user_confirmation_not_requested",
            "missing_billing_inconsistencies",
        ]

    def test_accepts_a_fully_escalated_inconsistency(self):
        disclosure = {
            "user_intent_charge_type": "POSTPAY",
            "priced_charge_type": "PrePaid",
            "deployed_charge_type": "PrePaid",
            "priced_currency": "USD",
            "consistent": False,
            "user_confirmation_required": True,
            "inconsistencies": [
                {
                    "party": "pricing",
                    "expected": "POSTPAY",
                    "actual": "PrePaid",
                    "reason": "only PrePaid can be priced in this region",
                }
            ],
        }
        assert validate_billing_consistency(disclosure, {"InstanceChargeType": "PrePaid"}) == []

    def test_rejects_unrecognized_charge_type(self):
        disclosure = self._consistent_disclosure()
        disclosure["priced_charge_type"] = "MysteryMode"
        issues = validate_billing_consistency(disclosure, {})
        assert _codes(issues) == ["unrecognized_charge_type"]


class TestValidatePricedCurrency:
    def test_accepts_matching_currency_ignoring_case(self):
        assert validate_priced_currency("usd", {"priced_currency": "USD"}) == []

    def test_rejects_cny_hardcoded_against_a_usd_quote(self):
        issues = validate_priced_currency("CNY", {"priced_currency": "USD"})
        assert _codes(issues) == ["currency_mismatch"]

    def test_rejects_missing_values(self):
        assert _codes(validate_priced_currency("CNY", {})) == ["missing_billing_field"]
        assert _codes(validate_priced_currency("", {"priced_currency": "USD"})) == ["missing_billing_field"]


class TestValidateBillingConfirmation:
    def _confirmation(self) -> dict:
        return {
            "confirmed": True,
            "acknowledged_charge_type": "PrePaid",
            "user_input": "可以，按预付费部署",
        }

    def test_no_notices_needs_no_confirmation(self):
        assert validate_billing_confirmation([], None) == []
        assert validate_billing_confirmation(None, None) == []

    def test_notices_require_a_confirmation_record(self):
        assert _codes(validate_billing_confirmation([{"detail": "x"}], None)) == ["missing_billing_confirmation"]

    def test_accepts_a_complete_confirmation(self):
        assert validate_billing_confirmation([{"detail": "x"}], self._confirmation()) == []

    def test_accepts_an_explicit_rejection(self):
        confirmation = dict(self._confirmation(), confirmed=False)
        assert validate_billing_confirmation([{"detail": "x"}], confirmation) == []

    def test_rejects_incomplete_confirmation(self):
        confirmation = self._confirmation()
        del confirmation["user_input"]
        assert _codes(validate_billing_confirmation([{"detail": "x"}], confirmation)) == ["missing_billing_field"]
