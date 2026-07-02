from iac_code.a2a.thinking_metadata import A2AThinkingMetadata


def test_thinking_metadata_accepts_integral_float_budget_from_a2a_boundary() -> None:
    policy = A2AThinkingMetadata.request_policy_from_metadata(
        {"iac_code": {"thinking": {"enabled": False, "effort": "low", "budget": 2048.0}}}
    )

    assert policy is not None
    assert policy.thinking_enabled is False
    assert policy.effort == "low"
    assert policy.thinking_budget == 2048


def test_thinking_metadata_accepts_flat_string_enabled_alias() -> None:
    policy = A2AThinkingMetadata.request_policy_from_metadata(
        {"iac_code": {"thinking_enabled": "true", "thinkingBudget": "1024"}}
    )

    assert policy is not None
    assert policy.thinking_enabled is True
    assert policy.thinking_budget == 1024
