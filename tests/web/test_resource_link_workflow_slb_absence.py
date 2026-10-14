"""Official SLB negative-read classification, not automatic cleanup or PASS."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.phases import known_absence_read


def negative(envelope, identifier):
    body = {
        "Code": "AclNotExist",
        "HostId": "slb.aliyuncs.com",
        "Message": "Acl does not exist.",
        "RequestId": "offline-request",
    }
    response = (
        {"status": 400, "body": body, "headers": {"x-acs-request-id": body["RequestId"]}}
        if envelope
        else {**body, "statusCode": 400}
    )
    return {
        "kind": "read",
        "state": "unknown",
        "call": json.dumps(
            {
                "product": "slb",
                "version": "2014-05-15",
                "action": "DescribeAccessControlListAttribute",
                "region_id": "cn-hangzhou",
                "params": {
                    "RegionId": "cn-hangzhou",
                    "AclId": identifier,
                    "Page": 1,
                    "PageSize": 10,
                },
            }
        ),
        "response": json.dumps(response),
    }


@pytest.mark.parametrize("envelope", [True, False])
@pytest.mark.parametrize("identifier", ["acl-offline123", "$created_id"])
def test_typed_original_slb_read_preserves_unknown_and_all_original_fields(envelope, identifier):
    row = negative(envelope, identifier)
    before = copy.deepcopy(row)
    assert known_absence_read(row)
    assert row == before


@pytest.mark.parametrize("envelope", [True, False])
@pytest.mark.parametrize(
    "damage",
    [
        "kind",
        "state",
        "product",
        "action",
        "version",
        "region",
        "param_region",
        "extra_param",
        "page",
        "page_size",
        "id_missing",
        "wildcard",
        "host",
        "code",
        "message",
        "request",
        "status",
        "missing_status",
        "float_status",
        "null",
        "request_whitespace",
    ],
)
def test_slb_error_does_not_generalize_to_unknown_write_or_unbound_read(envelope, damage):
    row = negative(envelope, "$created_id")
    call, response = json.loads(row["call"]), json.loads(row["response"])
    body = response["body"] if envelope else response
    if damage in {"kind", "state"}:
        row[damage] = "delete" if damage == "kind" else "succeeded"
    elif damage in {"product", "action", "version"}:
        call[damage] = "other"
    elif damage == "region":
        call["region_id"] = "cn-other"
    elif damage == "param_region":
        call["params"]["RegionId"] = "cn-other"
    elif damage == "extra_param":
        call["params"]["Unreviewed"] = True
    elif damage == "page":
        call["params"]["Page"] = 2
    elif damage == "page_size":
        call["params"]["PageSize"] = 100
    elif damage == "id_missing":
        del call["params"]["AclId"]
    elif damage == "wildcard":
        call["params"]["AclId"] = "*"
    elif damage in {"host", "code", "message", "request", "request_whitespace"}:
        key = {
            "host": "HostId",
            "code": "Code",
            "message": "Message",
            "request": "RequestId",
            "request_whitespace": "RequestId",
        }[damage]
        body[key] = " whitespace " if damage == "request_whitespace" else ""
    elif damage in {"status", "missing_status", "float_status"}:
        key = "status" if envelope else "statusCode"
        if damage == "missing_status":
            del response[key]
        else:
            response[key] = 403 if damage == "status" else 400.0
    else:
        response = None
    row.update(call=json.dumps(call), response=json.dumps(response))
    before = copy.deepcopy(row)
    assert not known_absence_read(row)
    assert row == before


@pytest.mark.parametrize("damage", ["header", "body_status", "body_null", "headers_null", "extra_wrapper"])
def test_slb_actual_status_and_request_header_must_agree(damage):
    row = negative(True, "acl-offline123")
    response = json.loads(row["response"])
    if damage == "header":
        response["headers"]["x-acs-request-id"] = "wrong"
    elif damage == "body_status":
        response["body"]["statusCode"] = 500
    elif damage == "body_null":
        response["body"] = None
    elif damage == "headers_null":
        response["headers"] = None
    else:
        response["extra"] = True
    row["response"] = json.dumps(response)
    assert not known_absence_read(row)
