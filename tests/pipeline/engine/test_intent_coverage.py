from __future__ import annotations

from iac_code.pipeline.engine.intent_coverage import (
    IntentCoverageIssue,
    collect_resource_intents,
    validate_intent_coverage,
)


def _intent(product, action="create", **overrides):
    intent = {"product": product, "action": action, "source": "user"}
    intent.update(overrides)
    return intent


def _candidate(name="ecs-single", products=None, resource_intents=None, excluded=None):
    candidate = {
        "name": name,
        "output_path": "templates/1-ecs-single.yml",
        "products": products if products is not None else ["ECS", "VPC"],
        "hard_constraints": [],
        "topology": "single ECS behind EIP",
        "monthly_estimate": "100-200 CNY",
        "pros": ["simple"],
        "cons": ["single point"],
    }
    if resource_intents is not None:
        candidate["resource_intents"] = resource_intents
    if excluded is not None:
        candidate["excluded_resource_intents"] = excluded
    return candidate


def test_collect_resource_intents_merges_sources_and_reports_invalid_entries():
    intents, issues = collect_resource_intents(
        {"intent": {"resource_intents": [_intent("OSS"), _intent("CDN")]}},
        ["intent.resource_intents"],
    )
    assert [item["product"] for item in intents] == ["OSS", "CDN"]
    assert issues == []

    intents, issues = collect_resource_intents(
        {
            "intent": {"resource_intents": [_intent("OSS", action="create")]},
            "candidate": {"resource_intents": [_intent("OSS", action="use_existing")]},
        },
        ["intent.resource_intents", "candidate.resource_intents"],
    )
    assert intents == [_intent("OSS", action="use_existing")]

    intents, issues = collect_resource_intents(
        {"intent": {"resource_intents": "OSS"}},
        ["intent.resource_intents"],
    )
    assert intents == []
    assert issues == [IntentCoverageIssue("invalid_intent_source", detail="intent.resource_intents")]

    intents, issues = collect_resource_intents(
        {"intent": {"resource_intents": ["OSS", {"action": "create"}]}},
        ["intent.resource_intents"],
    )
    assert intents == []
    assert [issue.code for issue in issues] == ["invalid_resource_intent", "missing_intent_product"]


def test_empty_intents_allow_any_candidate_set():
    assert validate_intent_coverage([], [_candidate()]) == []
    assert validate_intent_coverage([], []) == []


def test_reports_intent_resource_dropped_from_candidates():
    intents = [_intent("OSS"), _intent("CDN")]
    candidates = [_candidate(products=["ECS", "VPC"]), _candidate(name="sae", products=["SAE"])]

    issues = validate_intent_coverage(intents, candidates)

    assert [(issue.code, issue.product, issue.detail) for issue in issues] == [
        ("intent_resource_not_covered", "OSS", "candidates.0"),
        ("intent_resource_not_covered", "CDN", "candidates.0"),
        ("intent_resource_not_covered", "OSS", "candidates.1"),
        ("intent_resource_not_covered", "CDN", "candidates.1"),
    ]


def test_candidate_covers_intent_resource_via_products_or_resource_intents():
    intents = [_intent("OSS"), _intent("CDN")]

    via_products = _candidate(products=["OSS", "CDN"])
    assert validate_intent_coverage(intents, [via_products]) == []

    via_resource_intents = _candidate(
        products=[],
        resource_intents=[_intent("OSS"), _intent("CDN", action="reference")],
    )
    assert validate_intent_coverage(intents, [via_resource_intents]) == []


def test_product_name_matching_ignores_case_and_separators():
    intents = [_intent("Object-Storage")]
    candidate = _candidate(products=["object storage"])

    assert validate_intent_coverage(intents, [candidate]) == []


def test_explicit_exclusion_requires_non_empty_reason():
    intents = [_intent("OSS")]

    with_reason = _candidate(
        products=["ECS"],
        excluded=[{"product": "OSS", "reason": "本方案由 ECS 承载动态渲染，静态资源不单独使用 OSS"}],
    )
    assert validate_intent_coverage(intents, [with_reason]) == []

    without_reason = _candidate(products=["ECS"], excluded=[{"product": "OSS", "reason": "  "}])
    issues = validate_intent_coverage(intents, [without_reason])
    assert [(issue.code, issue.product) for issue in issues] == [
        ("intent_resource_exclusion_reason_missing", "OSS")
    ]


def test_exclusion_list_cannot_introduce_products_outside_the_intent():
    intents = [_intent("OSS")]
    candidate = _candidate(
        products=["OSS"],
        excluded=[{"product": "PolarDB", "reason": "成本过高"}],
    )

    issues = validate_intent_coverage(intents, [candidate])

    assert [(issue.code, issue.product) for issue in issues] == [("unexpected_intent_exclusion", "PolarDB")]


def test_forbidden_intent_resource_must_not_be_created():
    intents = [_intent("VSwitch", action="forbid"), _intent("SecurityGroup")]

    violating = _candidate(products=["SecurityGroup", "VSwitch"])
    issues = validate_intent_coverage(intents, [violating])
    assert [(issue.code, issue.product) for issue in issues] == [
        ("forbidden_intent_resource_present", "VSwitch")
    ]

    compliant = _candidate(
        products=[],
        resource_intents=[_intent("SecurityGroup"), _intent("VSwitch", action="forbid")],
    )
    assert validate_intent_coverage(intents, [compliant]) == []


def test_forbidden_resource_referenced_as_existing_is_not_a_creation():
    intents = [_intent("VPC", action="forbid"), _intent("SecurityGroup")]
    candidate = _candidate(
        products=[],
        resource_intents=[_intent("SecurityGroup"), _intent("VPC", action="use_existing")],
    )

    assert validate_intent_coverage(intents, [candidate]) == []


def test_malformed_candidate_payloads_are_reported():
    intents = [_intent("OSS")]

    assert validate_intent_coverage(intents, []) == [IntentCoverageIssue("invalid_candidates")]
    assert validate_intent_coverage(intents, "candidates") == [IntentCoverageIssue("invalid_candidates")]
    assert validate_intent_coverage(intents, ["oops"]) == [
        IntentCoverageIssue("invalid_candidate", detail="candidates.0")
    ]
