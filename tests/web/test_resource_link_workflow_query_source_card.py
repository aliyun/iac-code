"""Offline original QUERY card ownership, never model-authored URLs or cloud calls."""

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import query_source_card_binding


def reference(identifier="fixture-user", resource_type="ALIYUN::RAM::User", **source):
    return {
        "identifier": identifier,
        "resourceType": resource_type,
        "sources": [
            {
                "toolUseId": "call_own",
                "turnId": "own-turn",
                "evidence": "api_response",
                "sourceKind": "tool_result",
                **source,
            }
        ],
    }


def test_own_query_card_deduplicates_same_original_owner_only():
    own = reference()
    assert query_source_card_binding(
        [own, own, reference(identifier="other-user")], "fixture-user", "ALIYUN::RAM::User"
    ) == {
        "source_card_tool_use_id": "call_own",
        "source_card_turn_id": "own-turn",
    }


@pytest.mark.parametrize(
    "refs",
    [
        [],
        [{"identifier": "fixture-user", "resourceType": "ALIYUN::RAM::User"}],
        [reference(identifier="other-user")],
        [reference(resource_type="alicloud_ram_user")],
        [reference(evidence="model_output")],
        [reference(sourceKind="assistant")],
        [reference(), reference(toolUseId="call_other")],
        [reference(), reference(turnId="other-turn")],
    ],
)
def test_missing_or_ambiguous_owner_does_not_guess(refs):
    assert query_source_card_binding(refs, "fixture-user", "ALIYUN::RAM::User") is None


@pytest.mark.parametrize(
    "source",
    [
        {"toolUseId": None},
        {"toolUseId": 'call_"wrong'},
        {"toolUseId": ""},
        {"turnId": None},
        {"turnId": ""},
        {"turnId": "turn_unsafe"},
    ],
)
def test_unique_malformed_owner_is_rejected(source):
    with pytest.raises(WorkflowError, match="exact QUERY source card"):
        query_source_card_binding([reference(**source)], "fixture-user", "ALIYUN::RAM::User")
