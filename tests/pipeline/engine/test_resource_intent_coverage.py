from iac_code.pipeline.engine.resource_intent_coverage import (
    collect_resource_intents,
    normalize_product,
    validate_resource_intent_coverage,
)


def _intents():
    return [
        {"product": "ECS", "action": "create"},
        {"product": "NATGateway", "action": "create"},
        {"product": "EIP", "action": "create"},
    ]


def _codes(issues):
    return [issue.code for issue in issues]


def test_collect_resource_intents_merges_by_normalized_product():
    intents, issues = collect_resource_intents(
        {
            "intent": {"resource_intents": [{"product": "NAT Gateway", "action": "create"}]},
            "candidate": {"resource_intents": [{"product": "natgateway", "action": "use_existing"}]},
        },
        ["intent.resource_intents", "candidate.resource_intents"],
    )
    assert intents == [{"product": "natgateway", "action": "use_existing"}]
    assert issues == []


def test_collect_resource_intents_reports_invalid_sources():
    intents, issues = collect_resource_intents(
        {"intent": {"resource_intents": "ECS"}, "candidate": {"resource_intents": ["ECS", {"action": "create"}]}},
        ["intent.resource_intents", "candidate.resource_intents", "missing.field"],
    )
    assert intents == []
    assert _codes(issues) == [
        "invalid_resource_intent_source",
        "invalid_resource_intent",
        "missing_resource_intent_product",
    ]


def test_full_coverage_passes_with_alias_product_names():
    items = [{"covered_products": ["ecs", "nat-gateway", "Elastic IP"]}]
    issues = validate_resource_intent_coverage(
        [{"product": "ECS", "action": "create"}, {"product": "NATGateway", "action": "create"}],
        items,
        covered_products_fields=["covered_products"],
        gaps_field="resource_intent_gaps",
    )
    assert issues == []


def test_missing_intent_resource_is_reported_per_item():
    items = [{"covered_products": ["VPC", "VSwitch"]}]
    issues = validate_resource_intent_coverage(
        _intents(),
        items,
        covered_products_fields=["covered_products"],
        gaps_field="resource_intent_gaps",
    )
    assert _codes(issues) == ["uncovered_resource_intent"] * 3
    assert [issue.product for issue in issues] == ["ECS", "NATGateway", "EIP"]


def test_declared_gap_with_reason_is_accepted():
    items = [
        {
            "covered_products": ["ECS", "NATGateway"],
            "resource_intent_gaps": [{"product": "EIP", "reason": "第二期交付"}],
        }
    ]
    assert (
        validate_resource_intent_coverage(
            _intents(),
            items,
            covered_products_fields=["covered_products"],
            gaps_field="resource_intent_gaps",
        )
        == []
    )


def test_gap_without_reason_is_rejected():
    items = [{"covered_products": ["ECS", "NATGateway"], "resource_intent_gaps": [{"product": "EIP"}]}]
    issues = validate_resource_intent_coverage(
        _intents(),
        items,
        covered_products_fields=["covered_products"],
        gaps_field="resource_intent_gaps",
    )
    assert _codes(issues) == ["invalid_resource_intent_gap", "uncovered_resource_intent"]


def test_gap_for_undeclared_product_is_reported():
    items = [
        {
            "covered_products": ["ECS", "NATGateway", "EIP"],
            "resource_intent_gaps": [{"product": "RDS", "reason": "不在范围内"}],
        }
    ]
    issues = validate_resource_intent_coverage(
        _intents(),
        items,
        covered_products_fields=["covered_products"],
        gaps_field="resource_intent_gaps",
    )
    assert _codes(issues) == ["unexpected_resource_intent_gap"]


def test_forbidden_product_must_not_be_covered():
    issues = validate_resource_intent_coverage(
        [{"product": "ECS", "action": "create"}, {"product": "VSwitch", "action": "forbid"}],
        [{"covered_products": ["ECS", "VSwitch"]}],
        covered_products_fields=["covered_products"],
        gaps_field="resource_intent_gaps",
    )
    assert _codes(issues) == ["forbidden_resource_intent_covered"]


def test_resource_intent_objects_are_accepted_as_coverage_source():
    issues = validate_resource_intent_coverage(
        [{"product": "VPC", "action": "use_existing"}, {"product": "SecurityGroup", "action": "create"}],
        [
            {
                "resource_intents": [
                    {"product": "VPC", "action": "use_existing"},
                    {"product": "SecurityGroup", "action": "create"},
                ]
            }
        ],
        covered_products_fields=["resource_intents", "products"],
        gaps_field="resource_intent_gaps",
    )
    assert issues == []


def test_forbid_marked_coverage_entry_does_not_count_as_covered():
    issues = validate_resource_intent_coverage(
        [{"product": "ECS", "action": "create"}],
        [{"resource_intents": [{"product": "ECS", "action": "forbid"}]}],
        covered_products_fields=["resource_intents"],
        gaps_field="resource_intent_gaps",
    )
    assert _codes(issues) == ["uncovered_resource_intent"]


def test_empty_or_invalid_items_are_reported():
    for items in ([], "options", None):
        issues = validate_resource_intent_coverage(
            _intents(),
            items,
            covered_products_fields=["covered_products"],
            gaps_field="resource_intent_gaps",
        )
        assert _codes(issues) == ["invalid_coverage_items"]


def test_no_declared_intents_skips_validation():
    assert (
        validate_resource_intent_coverage(
            [],
            [],
            covered_products_fields=["covered_products"],
            gaps_field="resource_intent_gaps",
        )
        == []
    )


def test_normalize_product_folds_case_and_separators():
    assert normalize_product("NAT Gateway") == normalize_product("nat-gateway") == "natgateway"
