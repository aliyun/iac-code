"""Offline serialization contract for the endpoint landing collector."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


def validator():
    path = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_browser_batch.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["items"]["items"]["properties"]["diagnostics"]["additionalProperties"] is False
    return Draft202012Validator(schema["$defs"]["polardbEndpointDiagnostics"])


def diagnostic():
    return {
        "matched": True,
        "parentRouteMatched": True,
        "matchedCards": 1,
        "cards": [
            {
                "visible": True,
                "text": "默认地址（pe-fake） 地址: example.invalid:5432",
                "connectionScoped": True,
                "identityLabel": "visible_default_endpoint",
            }
        ],
        "hoverTargets": [],
        "section": {"count": 1, "scrolled": True},
        "connectionContent": {"ready": False, "waitError": "primary label absent"},
        "labelCandidates": [{"label": "主地址", "count": 0}],
        "readOnly": True,
        "searchFallbackSuppressed": True,
        "listView": {"attempted": True, "switched": True, "readOnly": True, "controls": []},
        "bodyTail": "diagnostic only",
        "graphEvidence": [],
    }


def test_endpoint_diagnostics_serialize_without_dropping_raw_collector_fields():
    validator().validate(diagnostic())
    validator().validate(None)
    negative = diagnostic()
    negative.update(matched=False, matchedCards=0, cards=[], readError="bounded read failed")
    validator().validate(negative)


@pytest.mark.parametrize("mutation", ["writable", "search", "extra", "card", "count", "missing"])
def test_endpoint_diagnostics_fail_closed_on_invalid_shape(mutation):
    value = copy.deepcopy(diagnostic())
    if mutation == "writable":
        value["readOnly"] = False
    elif mutation == "search":
        value["searchFallbackSuppressed"] = False
    elif mutation == "extra":
        value["arbitraryPassOverride"] = True
    elif mutation == "card":
        value["cards"][0]["visible"] = "true"
    elif mutation == "count":
        value["matchedCards"] = -1
    else:
        del value["cards"]
    assert list(validator().iter_errors(value))
