"""Offline managed TF member card provenance; no cloud or browser execution."""

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import managed_source_card_binding


def member(**source):
    return {
        "identifier": "fixture:table",
        "resourceType": "alicloud_ots_table",
        "stackId": "fixture-stack",
        "logicalResourceId": "table",
        "sources": [
            {
                "product": "ros",
                "action": "ListStackResources",
                "stackId": "fixture-stack",
                "logicalResourceId": "table",
                "toolUseId": "call_member",
                "turnId": "own-turn",
                "evidence": "api_response",
                "sourceKind": "tool_result",
                **source,
            }
        ],
    }


def binding(refs):
    return managed_source_card_binding(refs, "fixture:table", "alicloud_ots_table")


@pytest.mark.parametrize("action", ["ListStackResources", "GetStackResource"])
def test_managed_member_card_ignores_query_summary_and_cleanup_owners(action):
    assert binding(
        [
            member(action=action),
            member(product="ots", action="ListTable", toolUseId="call_query"),
            member(action="GetStack", toolUseId="call_stack"),
            member(action="DeleteStack", toolUseId="call_delete"),
        ]
    ) == {
        "source_card_tool_use_id": "call_member",
        "source_card_turn_id": "own-turn",
    }


@pytest.mark.parametrize(
    "source",
    [
        {"stackId": "other-stack"},
        {"logicalResourceId": "other-member"},
        {"product": "ots"},
        {"action": "GetStack"},
        {"action": "DeleteStack"},
        {"sourceKind": "assistant"},
        {"evidence": "model_output"},
    ],
)
def test_wrong_member_or_source_is_not_revealed(source):
    assert binding([member(**source)]) is None


def test_ambiguous_card_owner_is_not_guessed():
    assert binding([member(), member(toolUseId="call_other")]) is None
    assert binding([member(), member(turnId="other-turn")]) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("identifier", "other-table"),
        ("resourceType", "ALIYUN::OTS::Table"),
        ("stackId", None),
        ("logicalResourceId", None),
    ],
)
def test_reference_identity_must_match(field, value):
    assert binding([{**member(), field: value}]) is None


def test_unsafe_selector_owner_is_rejected():
    with pytest.raises(WorkflowError, match="source card identity"):
        binding([member(toolUseId='call_"unsafe')])
