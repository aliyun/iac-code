"""Offline fixtures for precise RAM negative reads, never real credentials/cloud."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.phases import known_absence_read


def negative(action, envelope):
    role = action == "GetRole"
    name = "offline-role" if role else "offline-policy"
    params = {"RoleName": name} if role else {"PolicyName": name, "PolicyType": "Custom"}
    body = {
        "RequestId": "offline-request",
        "HostId": "ram.aliyuncs.com",
        "Code": "EntityNotExist.Role" if role else "EntityNotExist.Policy",
        "Message": ("The role not exists:" if role else "The policy does not exist: ") + name,
    }
    response = (
        {"status": 404, "headers": {"x-acs-request-id": body["RequestId"]}, "body": body}
        if envelope
        else {**body, "statusCode": 404}
    )
    return {
        "kind": "read",
        "state": "unknown",
        "call": json.dumps(
            {"product": "ram", "version": "2015-05-01", "action": action, "region_id": "cn-hangzhou", "params": params}
        ),
        "response": json.dumps(response),
    }


@pytest.mark.parametrize("action", ["GetRole", "GetPolicy"])
@pytest.mark.parametrize("envelope", [True, False])
def test_exact_ram_negative_keeps_original_unknown_and_every_field(action, envelope):
    row = negative(action, envelope)
    before = copy.deepcopy(row)
    assert known_absence_read(row)
    assert row == before and row["state"] == "unknown"


@pytest.mark.parametrize("action", ["GetRole", "GetPolicy"])
@pytest.mark.parametrize("envelope", [True, False])
@pytest.mark.parametrize(
    "damage",
    [
        "kind",
        "state",
        "product",
        "action",
        "version",
        "extra_param",
        "name",
        "invalid_name",
        "extra_response",
        "host",
        "code",
        "message",
        "request",
        "missing_request",
        "status",
        "missing_status",
        "float_status",
        "null",
    ],
)
def test_unknown_errors_or_writes_cannot_be_treated_as_ram_absence(action, envelope, damage):
    row = negative(action, envelope)
    call, response = json.loads(row["call"]), json.loads(row["response"])
    body = response["body"] if envelope else response
    if damage in {"kind", "state"}:
        row[damage] = "delete" if damage == "kind" else "succeeded"
    elif damage in {"product", "action", "version"}:
        call[damage] = "other"
    elif damage == "extra_param":
        call["params"]["unreviewed"] = "other"
    elif damage in {"name", "invalid_name"}:
        call["params"]["RoleName" if action == "GetRole" else "PolicyName"] = "other-name" if damage == "name" else "*"
    elif damage == "extra_response":
        response["unexpected_envelope_key"] = True
        if not envelope:
            body.pop("statusCode")  # Unproved wrapper cannot supply transport status.
    elif damage in {"host", "code", "message", "request"}:
        body[{"host": "HostId", "code": "Code", "message": "Message", "request": "RequestId"}[damage]] = ""
    elif damage == "missing_request":
        body.pop("RequestId")
    elif damage in {"status", "missing_status", "float_status"}:
        key = "status" if envelope else "statusCode"
        if damage == "missing_status":
            response.pop(key)
        else:
            response[key] = 403 if damage == "status" else 404.0
    else:
        response = None
    row.update(call=json.dumps(call), response=json.dumps(response))
    before = copy.deepcopy(row)
    assert not known_absence_read(row)
    assert row == before


@pytest.mark.parametrize("damage", ["system", "header", "body_status", "body_null", "headers_null", "name_missing"])
def test_ram_policy_exact_type_transport_status_and_header_provenance(damage):
    row = negative("GetPolicy", True)
    call, response = json.loads(row["call"]), json.loads(row["response"])
    if damage == "system":
        call["params"]["PolicyType"] = "System"
    elif damage == "header":
        response["headers"]["x-acs-request-id"] = "other-request"
    elif damage == "body_status":
        response["body"]["statusCode"] = 500
    elif damage == "body_null":
        response["body"] = None
    elif damage == "headers_null":
        response["headers"] = None
    else:
        call["params"].pop("PolicyName")
    row.update(call=json.dumps(call), response=json.dumps(response))
    assert not known_absence_read(row)
