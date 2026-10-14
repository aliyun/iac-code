"""Separate product execution from worker navigation and explicit cleanup."""

import json
import re
import sqlite3
import time
from pathlib import Path

from scripts.web.e2e.resource_link_workflow.execution_gate import (
    GateDeniedError,
    mse_managed_members,
    mse_managed_params,
    mse_managed_parent,
    mse_managed_scope,
    mse_rotated_cleanup_identity,
    nas_managed_delete,
    nas_managed_params,
    nas_managed_parent,
    nas_managed_scope,
    nas_native_attempt,
    nas_native_binding,
    nas_native_ready,
    nas_native_resolve,
    nas_native_signer,
    phase_allowed,
    read_policy,
    resource_group_scope,
    response_identifier,
    successful_mse_read_body,
    successful_resource_group_body,
)

from .files import WorkflowError, digest

LOADED_SHA = digest(Path(__file__))


def known_absence_read(row) -> bool:
    """Recognize documented exact negative reads without rewriting the journal.

    NAS returns HTTP404 for a filtered nonexistent access group. This is not
    an unknown cloud write, nor a successful response. Keep the original error
    and quota consumption; independent cleanup acceptance still needs its source.
    """
    try:
        call = json.loads(row["call"])
        response = json.loads(row["response"])
        params = call.get("params", {})
        if (
            row["kind"] == "read" and row["state"] == "unknown"
            and call.get("product") == "slb" and call.get("version") == "2014-05-15"
            and call.get("action") == "DescribeAccessControlListAttribute"
            and isinstance(params, dict) and isinstance(response, dict)
        ):
            identifier, region = params.get("AclId"), call.get("region_id")
            if (not isinstance(region, str) or re.fullmatch(r"cn-[a-z0-9-]+", region) is None
                    or params.get("RegionId") != region
                    or not {"RegionId", "AclId"}.issubset(params)
                    or not set(params).issubset({"RegionId", "AclId", "Page", "PageSize"})
                    or not isinstance(identifier, str)
                    or (identifier != "$created_id" and re.fullmatch(r"acl-[A-Za-z0-9]+", identifier) is None)
                    or any(type(params[key]) is not int or params[key] != 1 for key in ("Page",) if key in params)
                    or ("PageSize" in params and (type(params["PageSize"]) is not int
                                                 or not 1 <= params["PageSize"] <= 50))):
                return False
            if set(response) == {"body", "status", "headers"}:
                body, status, headers = response["body"], response["status"], response["headers"]
                if not isinstance(body, dict) or not isinstance(headers, dict):
                    return False
                if "statusCode" in body and body["statusCode"] != status:
                    return False
                if any(not isinstance(key, str) or (
                    key.casefold() == "x-acs-request-id" and value != body.get("RequestId")
                ) for key, value in headers.items()):
                    return False
            else:
                body, status = response, response.get("statusCode")
            request_id = body.get("RequestId")
            # Official SLB global error table: 400/AclNotExist. This only
            # classifies a negative read; the caller still verifies original
            # signed scope, response-bound AclId and post-delete ordering.
            return (
                type(status) is int and status == 400 and body.get("Code") == "AclNotExist"
                and body.get("HostId") == "slb.aliyuncs.com" and body.get("Message") == "Acl does not exist."
                and isinstance(request_id, str) and bool(request_id) and request_id.strip() == request_id
            )
        if (
            row["kind"] == "read" and row["state"] == "unknown"
            and call.get("product") == "oss" and call.get("action") == "GetBucketInfo"
            and call.get("version") == "2019-05-17"
            and isinstance(params, dict) and set(params) == {"bucket"}
            and isinstance(response, dict) and set(response) == {"status", "body", "headers"}
        ):
            from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse
            from iac_code.tools.cloud.aliyun.oss_v4_adapter import oss_bucket_not_found

            return oss_bucket_not_found(
                NormalizedApiResponse(
                    body=response["body"], status=response["status"], headers=response["headers"],
                    content_type=None, content_encoding=None, size=0,
                ), params["bucket"],
            )
        if (
            row["kind"] == "read" and row["state"] == "unknown"
            and call.get("product") == "ram" and call.get("version") == "2015-05-01"
            and call.get("action") in {"GetRole", "GetPolicy"}
            and isinstance(params, dict) and isinstance(response, dict)
        ):
            role = call["action"] == "GetRole"
            name_key = "RoleName" if role else "PolicyName"
            name = params.get(name_key)
            expected_keys = {name_key} if role else {name_key, "PolicyType"}
            pattern = r"[A-Za-z0-9.-]{1,64}" if role else r"[A-Za-z0-9][A-Za-z0-9-]{0,127}"
            if (set(params) != expected_keys or not isinstance(name, str)
                    or re.fullmatch(pattern, name) is None
                    or (not role and params["PolicyType"] != "Custom")):
                return False
            # New gate envelopes retain actual transport status separately from
            # the unchanged body. Legacy SDK originals may already carry it.
            if set(response) == {"body", "status", "headers"}:
                body, status, headers = response["body"], response["status"], response["headers"]
                if not isinstance(body, dict) or not isinstance(headers, dict):
                    return False
                if "statusCode" in body and body["statusCode"] != status:
                    return False
                if any(
                    not isinstance(key, str) or (
                        key.casefold() == "x-acs-request-id" and value != body.get("RequestId")
                    ) for key, value in headers.items()
                ):
                    return False
            else:
                body, status = response, response.get("statusCode")
            request_id = body.get("RequestId")
            return (
                type(status) is int and status == 404
                and body.get("Code") == ("EntityNotExist.Role" if role else "EntityNotExist.Policy")
                and body.get("HostId") == "ram.aliyuncs.com"
                and body.get("Message") == ("The role not exists:" if role else "The policy does not exist: ") + name
                and isinstance(request_id, str) and bool(request_id) and request_id.strip() == request_id
            )
        return (
            row["kind"] == "read"
            and row["state"] == "unknown"
            and call.get("product", "").lower() == "nas"
            and call.get("action") == "DescribeAccessGroups"
            and isinstance(params.get("AccessGroupName"), str)
            and bool(params["AccessGroupName"])
            and params.get("FileSystemType") in {"standard", "extreme"}
            and response.get("Code") == "InvalidAccessGroup.NotFound"
            and response.get("statusCode") == 404
            and isinstance(response.get("RequestId"), str)
            and bool(response["RequestId"])
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _known_original_oss_absence_read(connection, row) -> bool:
    """Project only the original PutBucket Location, including reviewed STS rotation.

    Read-only classification is not a cleanup verdict. Preserve the original
    unknown response and allowance; post-delete completeness is checked separately.
    """
    from scripts.web.e2e.resource_link_workflow.execution_gate import cleanup_identity_extensions

    try:
        call = json.loads(row["call"])
        if (row["kind"] != "read" or row["state"] != "unknown"
                or call.get("product") != "oss" or call.get("version") != "2019-05-17"
                or call.get("action") != "GetBucketInfo" or call.get("params") != {"bucket": "$created_id"}):
            return False
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        original = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param,g.target_encoding,"
            "g.authority,g.quota,g.used FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.id=?",
            (row["scope"], row["id"]),
        ).fetchone()
        if not original or any(original[k] != row[k] for k in ("kind", "state", "call", "response")):
            return False
        parents = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.authority,g.quota,g.used FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.grant_id=?",
            (original["scope"], original["parent"]),
        ).fetchall()
        if len(parents) != 1:
            return False
        parent = parents[0]
        parent_call = json.loads(parent["call"])
        region = call.get("region_id")
        if (parent["kind"] != "create" or parent["state"] != "succeeded"
                or parent["quota"] != 1 or parent["used"] != 1
                or not original["authority"] or parent["authority"] != original["authority"]
                or not original["session"] or original["session"] != parent["session"]
                or not parent["credential"] or not original["credential"]
                or original["created"] <= parent["created"]
                or original["target_param"] != "bucket" or original["target_encoding"] is not None
                or json.loads(original["response_path"]) != ["headers", "location"]
                or parent_call.get("product") != "oss" or parent_call.get("version") != "2019-05-17"
                or parent_call.get("action") != "PutBucket" or parent_call.get("region_id") != region
                or not isinstance(region, str) or re.fullmatch(r"cn-[a-z0-9-]+", region) is None):
            return False
        identifier = response_identifier(parent_call, json.loads(parent["response"]), ["headers", "location"])
        if not identifier:
            return False
        accounts = []
        for attempt in (parent, original):
            limit = cursor.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?",
                (attempt["authority"], attempt["grant_id"]),
            ).fetchone()
            count = cursor.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?",
                                   (attempt["scope"], attempt["grant_id"])).fetchone()[0]
            if not limit or not 1 <= count == attempt["used"] <= limit[0] <= attempt["quota"]:
                return False
            identity = cursor.execute(
                "SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
                (attempt["scope"], attempt["session"], attempt["credential"]),
            ).fetchone()
            if not identity or not identity[0]:
                return False
            accounts.append(identity[0])
            signed = cursor.execute(
                "SELECT a.response,g.call,g.id,g.authority FROM attempts a JOIN grants g "
                "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.session=? "
                "AND a.credential=? AND a.state='succeeded' AND g.kind='identity' AND a.created<?",
                (attempt["scope"], attempt["session"], attempt["credential"], attempt["created"]),
            ).fetchall()
            valid = [r for r in signed if json.loads(r["call"]).get("product") == "sts"
                     and json.loads(r["call"]).get("action") == "GetCallerIdentity"
                     and str(json.loads(r["response"]).get("AccountId", "")) == identity[0]
                     and bool(json.loads(r["response"]).get("RequestId"))]
            if not valid:
                return False
            if attempt["credential"] != parent["credential"]:
                # Only the immutable OPS one-shot supplement authorizes a new
                # signer; a matching account string or arbitrary STS is not enough.
                factory = connection.row_factory
                try:
                    connection.row_factory = sqlite3.Row
                    extensions = cleanup_identity_extensions(connection, original["scope"])
                finally:
                    connection.row_factory = factory
                approved = cursor.execute(
                    "SELECT grant_id,authority FROM cleanup_identity_reviews WHERE scope=? AND session=? AND account=?",
                    (original["scope"], original["session"], identity[0]),
                ).fetchone()
                if (not approved or approved[1] != original["authority"]
                        or not any(e["id"] == approved[0] for e in extensions)
                        or not any(r["id"] == approved[0] and r["authority"] == approved[1] for r in valid)):
                    return False
            elif not any(r["authority"] == parent["authority"] for r in valid):
                return False
        response = json.loads(original["response"])
        if accounts[0] != accounts[1] or response.get("body", {}).get("HostId") != (
            identifier + ".oss-" + region + ".aliyuncs.com"
        ):
            return False
        call["params"]["bucket"] = identifier
        return known_absence_read({**dict(original), "call": json.dumps(call)})
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OSError, sqlite3.Error):
        return False


def _known_original_nas_child_absence_read(connection, row) -> bool:
    """Classify only an own post-delete typed NAS read, without changing its unknown journal state."""
    try:
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        original = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param,g.read_policy,g.authority,g.quota,g.used "
            "FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=? AND a.scope=?",
            (row["id"], row["scope"]),
        ).fetchone()
        if not original or any(original[key] != row[key] for key in ("kind", "state", "call", "response")):
            return False
        binding = json.loads(original["response_path"] or "null")
        policy = read_policy(original)
        if (
            original["kind"] != "read" or original["state"] != "unknown"
            or not isinstance(binding, dict) or binding.get("kind") != "nas_managed_child"
            or policy.get("phase") != "cleanup" or policy.get("required_cleanup") is not True
            or policy.get("after_delete") != original["parent"]
            or original["quota"] != 1 or original["used"] != 1 or not original["authority"]
        ):
            return False
        identity = cursor.execute(
            "SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
            (original["scope"], original["session"], original["credential"]),
        ).fetchone()
        limit = cursor.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                               (original["authority"], original["grant_id"])).fetchone()
        count = cursor.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?",
                               (original["scope"], original["grant_id"])).fetchone()[0]
        if not identity or not limit or limit[0] != 1 or count != 1:
            return False
        active = {"scope": original["scope"], "session": original["session"], "account": identity[0]}
        # Nested joins need named columns even when an OPS caller uses the
        # default tuple connection. Keep the caller's row factory untouched.
        parent = nas_managed_parent(cursor, active, original["parent"])
        if not parent or parent["credential"] != original["credential"]:
            return False
        params = nas_managed_params(cursor, active, parent, original)
        deletion = nas_managed_delete(cursor, parent)
        if not params or not deletion or original["created"] <= deletion["created"]:
            return False
        expected = json.loads(original["call"])
        expected["params"].update(params)
        response = json.loads(original["response"])
        if (not isinstance(response, dict) or set(response) != {"body", "status", "headers", "call"}
                or response["call"] != expected or type(response["status"]) is not int or response["status"] != 404):
            return False
        body, headers = response["body"], response["headers"]
        allowed = {"InvalidFileSystem.NotFound"}
        if binding["child"] == "mount_target":
            allowed.add("InvalidMountTarget.NotFound")
        if not isinstance(body, dict) or not isinstance(headers, dict):
            return False
        request_id = body.get("RequestId")
        return bool(
            body.get("Code") in allowed and not body.get("ErrorCode")
            and isinstance(request_id, str) and request_id and request_id.strip() == request_id
            and body.get("statusCode", 404) == 404
            and body.get("HostId", "nas.aliyuncs.com") in {"nas.aliyuncs.com", "nas.cn-hangzhou.aliyuncs.com"}
            and all(isinstance(key, str) and (
                key.casefold() != "x-acs-request-id" or value == request_id
            ) for key, value in headers.items())
        )
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, sqlite3.Error, GateDeniedError):
        return False


def _known_original_nas_stack_absence_read(connection, row) -> bool:
    """Own hosted stack's documented GetStack404 after its exact deletion only."""
    try:
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        original = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param,g.read_policy,g.authority,g.quota,g.used "
            "FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=? AND a.scope=?",
            (row["id"], row["scope"]),
        ).fetchone()
        if not original or any(original[key] != row[key] for key in ("kind", "state", "call", "response")):
            return False
        policy = read_policy(original)
        call = json.loads(original["call"])
        if (original["kind"] != "read" or original["state"] != "unknown"
                or not (nas_managed_scope(connection, original["scope"])
                        or mse_managed_scope(connection, original["scope"]))
                or policy.get("phase") != "cleanup" or policy.get("required_cleanup") is not True
                or policy.get("after_delete") != original["parent"]
                or json.loads(original["response_path"] or "null") != ["StackId"]
                or original["target_param"] != "StackId" or call.get("product") != "ros"
                or call.get("version") != "2019-09-10" or call.get("action") != "GetStack"):
            return False
        identity = cursor.execute("SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
                                  (original["scope"], original["session"], original["credential"])).fetchone()
        if not identity:
            return False
        active = {"scope": original["scope"], "session": original["session"], "account": identity[0]}
        mse = mse_managed_scope(connection, original["scope"])
        parent_reader, params_reader = ((mse_managed_parent, mse_managed_params) if mse
                                        else (nas_managed_parent, nas_managed_params))
        parent = parent_reader(cursor, active, original["parent"])
        if not parent or (parent["credential"] != original["credential"] and not (
            mse and mse_rotated_cleanup_identity(cursor, active, original, parent, original["credential"],
                                                original["reference_scope"], before=original["created"])
        )):
            return False
        child = next((grant for grant in cursor.execute("SELECT * FROM grants WHERE scope=? AND parent=?",
                                                       (original["scope"], original["parent"]))
                      if isinstance(binding := json.loads(grant["response_path"] or "null"), dict)
                      and binding.get("kind") == ("mse_managed_child" if mse else "nas_managed_child")
                      and read_policy(grant).get("phase") == "cleanup"), None)
        if child is None or not params_reader(cursor, active, parent, child):
            return False
        deletion = nas_managed_delete(cursor, parent)
        stack_id = json.loads(parent["response"])["StackId"]
        expected = {"product": "ros", "action": "GetStack", "version": "2019-09-10",
                    "region_id": "cn-hangzhou",
                    "params": {"RegionId": "cn-hangzhou", "StackId": stack_id, "OutputOption": "Enabled"}}
        call["params"]["StackId"] = stack_id
        response = json.loads(original["response"])
        limit = cursor.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                               (original["authority"], original["grant_id"])).fetchone()
        count = cursor.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?",
                               (original["scope"], original["grant_id"])).fetchone()[0]
        if (not deletion or original["created"] <= deletion["created"] or call != expected
                or not limit or not 0 < count == original["used"] <= limit[0] <= original["quota"]
                or not isinstance(response, dict) or set(response) != {"body", "status", "headers", "call"}
                or response["call"] != expected or type(response["status"]) is not int or response["status"] != 404):
            return False
        body, headers = response["body"], response["headers"]
        if not isinstance(body, dict) or not isinstance(headers, dict):
            return False
        request_id = body.get("RequestId")
        return bool(
            body.get("Code") == "StackNotFound" and not body.get("ErrorCode")
            and body.get("Message") == "The Stack (" + stack_id + ") could not be found."
            and isinstance(request_id, str) and request_id and request_id.strip() == request_id
            and body.get("statusCode", 404) == 404
            and body.get("HostId", "ros.aliyuncs.com") in {"ros.aliyuncs.com", "ros.cn-hangzhou.aliyuncs.com"}
            and all(isinstance(key, str) and (key.casefold() != "x-acs-request-id" or value == request_id)
                    for key, value in headers.items())
        )
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, sqlite3.Error, GateDeniedError):
        return False


def _known_original_mse_child_absence_read(connection, row) -> bool:
    """Documented exact negative reads of original hosted MSE/network members.

    This classifies the unchanged failed read, never marks ownership deleted or
    navigation passed. Same-signer pre-delete membership and a successful own
    deletion are mandatory; permission/server errors are not absence.
    """
    try:
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        original = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param,g.read_policy,g.authority,g.quota,g.used "
            "FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=? AND a.scope=?",
            (row["id"], row["scope"]),
        ).fetchone()
        if not original or any(original[key] != row[key] for key in ("kind", "state", "call", "response")):
            return False
        binding = json.loads(original["response_path"] or "null")
        policy = read_policy(original)
        if (original["kind"] != "read" or original["state"] != "unknown"
                or not isinstance(binding, dict) or binding.get("kind") != "mse_managed_child"
                or policy.get("phase") != "cleanup" or policy.get("required_cleanup") is not True
                or policy.get("after_delete") != original["parent"]
                or not original["authority"]):
            return False
        identity = cursor.execute("SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
                                  (original["scope"], original["session"], original["credential"])).fetchone()
        limit = cursor.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                               (original["authority"], original["grant_id"])).fetchone()
        count = cursor.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?",
                               (original["scope"], original["grant_id"])).fetchone()[0]
        if not identity or not limit or not 0 < count == original["used"] <= limit[0] <= original["quota"]:
            return False
        active = {"scope": original["scope"], "session": original["session"], "account": identity[0]}
        parent = mse_managed_parent(cursor, active, original["parent"])
        same_signer = bool(parent and parent["credential"] == original["credential"]
                           and parent["reference_scope"] == original["reference_scope"])
        if (not original["tool_use_id"] or (not same_signer and not mse_rotated_cleanup_identity(
                cursor, active, original, parent, original["credential"], original["reference_scope"],
                before=original["created"],
        ))):
            return False
        params = mse_managed_params(cursor, active, parent, original)
        deletion = nas_managed_delete(cursor, parent)
        if not params or not deletion or original["created"] <= deletion["created"]:
            return False
        expected = json.loads(original["call"])
        expected["params"].update(params)
        response = json.loads(original["response"])
        if (not isinstance(response, dict) or set(response) != {"body", "status", "headers", "call"}
                or response["call"] != expected or type(response["status"]) is not int):
            return False
        body, headers, status = response["body"], response["headers"], response["status"]
        if not isinstance(body, dict) or not isinstance(headers, dict):
            return False
        contracts = {
            "cluster": ("mse", "QueryClusterInfo", "2019-05-31",
                        {(404, "NotFound")}, None),
            "vpc": ("vpc", "DescribeVpcAttribute", "2016-04-28",
                    {(400, "Forbidden.VpcNotFound"), (404, "InvalidVpcId.NotFound")},
                    "Specified VPC does not exist."),
            "vswitch": ("vpc", "DescribeVSwitchAttributes", "2016-04-28",
                        {(400, "InvalidVSwitchId.NotFound")}, "Specified VSwitch can not found."),
        }
        product, action, version, errors, message = contracts[binding["child"]]
        request_id = body.get("RequestId")
        actual_message = body.get("Message")
        message_valid = (actual_message == message if message is not None else
                         isinstance(actual_message, str) and actual_message.startswith("Not found:")
                         and bool(actual_message.removeprefix("Not found:")))
        return bool(
            (expected["product"], expected["action"], expected["version"]) == (product, action, version)
            and expected["region_id"] == expected["params"].get("RegionId") == "cn-hangzhou"
            and (status, body.get("Code")) in errors and not body.get("ErrorCode") and message_valid
            and isinstance(request_id, str) and request_id and request_id.strip() == request_id
            and body.get("statusCode", status) == status
            and body.get("HostId", product + ".aliyuncs.com") in {
                product + ".aliyuncs.com", product + ".cn-hangzhou.aliyuncs.com"}
            and all(isinstance(key, str) and (key.casefold() != "x-acs-request-id" or value == request_id)
                    for key, value in headers.items())
        )
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, sqlite3.Error, GateDeniedError):
        return False


def known_original_mse_success_read(connection, row) -> bool:
    """Read-only interpretation of an old misclassified successful own-cluster read.

    This is not absence, a write reconciliation, cleanup proof or navigation PASS.
    Preserve the original unknown state, response and consumed authority.
    """
    try:
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        original = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param,g.read_policy,"
            "g.target_encoding,g.authority,g.quota,g.used FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=? AND a.scope=?",
            (row["id"], row["scope"]),
        ).fetchone()
        if not original or any(original[k] != row[k] for k in ("kind", "state", "call", "response")):
            return False
        binding = json.loads(original["response_path"] or "null")
        call = json.loads(original["call"])
        if (original["kind"] != "read" or original["state"] != "unknown"
                or not isinstance(binding, dict) or binding.get("kind") != "mse_managed_child"
                or binding.get("child") != "cluster" or read_policy(original) != {"phase": "execute"}
                or original["target_param"] != "InstanceId" or original["target_encoding"] is not None
                or not original["authority"] or not original["tool_use_id"]
                or call != {"product": "mse", "action": "QueryClusterInfo", "version": "2019-05-31",
                            "region_id": "cn-hangzhou",
                            "params": {"RegionId": "cn-hangzhou", "InstanceId": "$created_id"}}):
            return False
        identity = cursor.execute("SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
                                  (original["scope"], original["session"], original["credential"])).fetchone()
        limit = cursor.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                               (original["authority"], original["grant_id"])).fetchone()
        count = cursor.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?",
                               (original["scope"], original["grant_id"])).fetchone()[0]
        parent_grant = cursor.execute("SELECT authority,quota,used FROM grants WHERE scope=? AND id=?",
                                      (original["scope"], original["parent"])).fetchone()
        parent_limit = cursor.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                      (original["authority"], original["parent"])).fetchone()
        if (not identity or not limit or not parent_grant or parent_grant[0] != original["authority"]
                or not parent_limit or parent_limit[0] != parent_grant[1] or parent_grant[1] != 1
                or parent_grant[2] != 1
                or not 0 < count == original["used"] <= limit[0] <= original["quota"]):
            return False
        active = {"scope": original["scope"], "session": original["session"], "account": identity[0]}
        parent = mse_managed_parent(cursor, active, original["parent"])
        if (not parent or parent["credential"] != original["credential"]
                or parent["reference_scope"] != original["reference_scope"]
                or original["created"] <= parent["created"]):
            return False
        proof = mse_managed_members(cursor, active, parent, binding)
        deletion = nas_managed_delete(cursor, parent)
        if (not proof or original["created"] <= max(proof[k]["created"] for k in (
            "member_attempt", "status_attempt")) or deletion and original["created"] >= deletion["created"]):
            return False
        identifier = proof["members"]["cluster"]["PhysicalResourceId"]
        call["params"]["InstanceId"] = identifier
        # Virtual success only for transport validation; never update the saved row.
        body = successful_mse_read_body({**dict(original), "state": "succeeded"}, call)
        data = body.get("Data") if body else None
        return bool(body and body.get("ErrorCode") == "Success" and isinstance(data, dict)
                    and data.get("InstanceId") == identifier and data.get("RegionId") == call["region_id"]
                    and data.get("AccountId", identity[0]) == identity[0]
                    and all(data.get(key, value) == value for key, value in (
                        ("VpcId", proof["members"]["vpc"]["PhysicalResourceId"]),
                        ("VSwitchId", proof["members"]["vswitch"]["PhysicalResourceId"]),
                    )))
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, sqlite3.Error, GateDeniedError):
        return False


def known_original_resolved_read(connection, row) -> bool:
    """Original proven negative or narrowly misclassified successful read, never a write."""
    return (known_original_native_nas_absence_read(connection, row)
            or known_original_absence_read(connection, row) or known_original_mse_success_read(connection, row))


def known_original_native_nas_absence_read(connection, row) -> bool:
    """Classify exact signed native negative reads without rewriting state/counters."""
    if (row["kind"] != "read" or row["state"] != "unknown"
            or row["grant_id"] not in {"precheck-bucket-absent", "delete-bucket-absence", "delete-export-absence",
                                       "delete-filesystem-absence"}):
        return False
    try:
        active = connection.execute(
            "SELECT * FROM active WHERE scope=? AND session=?", (row["scope"], row["session"]),
        ).fetchone()
        if not active:
            return False
        signer = nas_native_signer(connection, active)
        original, call, response = nas_native_attempt(
            connection, active, row["grant_id"], signer, allow_error=True,
        )
        return original["id"] == row["id"] and nas_native_ready(row["grant_id"], call, response)
    except (GateDeniedError, KeyError, TypeError, ValueError, AttributeError):
        return False


def known_original_absence_read(connection, row) -> bool:
    """Resolve a RAM name from its original signed creation, never error text.

    Grants deliberately retain $created_id. Classification uses a temporary
    projection after checking the original attempt, parent, identity and consumed
    authority in the same read-only snapshot. No journal/allowance is changed;
    deletion ordering and independent cleanup acceptance remain separate checks.
    """
    if known_absence_read(row):
        return True
    if _known_original_oss_absence_read(connection, row):
        return True
    if _known_original_nas_child_absence_read(connection, row):
        return True
    if _known_original_nas_stack_absence_read(connection, row):
        return True
    if _known_original_mse_child_absence_read(connection, row):
        return True
    try:
        call = json.loads(row["call"])
        role = call.get("action") == "GetRole"
        key = "RoleName" if role else "PolicyName"
        if (
            row["kind"] != "read" or row["state"] != "unknown"
            or call.get("product") != "ram" or call.get("version") != "2015-05-01"
            or call.get("action") not in {"GetRole", "GetPolicy"}
            or call.get("params", {}).get(key) != "$created_id"
        ):
            return False
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        original = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param,g.target_encoding,"
            "g.authority,g.quota,g.used FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.id=?",
            (row["scope"], row["id"]),
        ).fetchone()
        if not original or any(original[k] != row[k] for k in ("kind", "state", "call", "response")):
            return False
        parents = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.authority,g.quota,g.used FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.grant_id=?",
            (original["scope"], original["parent"]),
        ).fetchall()
        if len(parents) != 1:
            return False
        parent = parents[0]
        parent_call, body = json.loads(parent["call"]), json.loads(parent["response"])
        path = ["Role", "RoleName"] if role else ["Policy", "PolicyName"]
        if (
            parent["kind"] != "create" or parent["state"] != "succeeded"
            or parent["quota"] != 1 or parent["used"] != 1
            or not original["authority"] or original["authority"] != parent["authority"]
            or not original["session"] or not original["credential"]
            or (original["session"], original["credential"]) != (parent["session"], parent["credential"])
            or original["created"] <= parent["created"]
            or original["target_param"] != key or original["target_encoding"] is not None
            or json.loads(original["response_path"]) != path
            or parent_call.get("product") != "ram" or parent_call.get("version") != "2015-05-01"
            or parent_call.get("action") != ("CreateRole" if role else "CreatePolicy")
            or parent_call.get("region_id") != call.get("region_id")
            or not isinstance(call.get("region_id"), str)
            or re.fullmatch(r"cn-[a-z0-9-]+", call["region_id"]) is None
            or not isinstance(body, dict) or not isinstance(body.get("RequestId"), str) or not body["RequestId"]
            or (not role and body.get("Policy", {}).get("PolicyType") != "Custom")
        ):
            return False
        identifier = response_identifier(parent_call, body, path)
        if not identifier or identifier != parent_call.get("params", {}).get(key):
            return False
        for attempt in (original, parent):
            limit = cursor.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?",
                (attempt["authority"], attempt["grant_id"]),
            ).fetchone()
            count = cursor.execute(
                "SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?",
                (attempt["scope"], attempt["grant_id"]),
            ).fetchone()[0]
            if not limit or not 1 <= count == attempt["used"] <= limit[0] <= attempt["quota"]:
                return False
        identity = cursor.execute(
            "SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
            (parent["scope"], parent["session"], parent["credential"]),
        ).fetchone()
        if not identity or not identity[0]:
            return False
        signed = cursor.execute(
            "SELECT a.response,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
            "WHERE a.scope=? AND a.session=? AND a.credential=? AND a.state='succeeded' "
            "AND g.kind='identity' AND g.authority=? AND a.created<?",
            (parent["scope"], parent["session"], parent["credential"], parent["authority"], parent["created"]),
        ).fetchall()
        if not any(
            json.loads(r["call"]).get("product") == "sts"
            and json.loads(r["call"]).get("action") == "GetCallerIdentity"
            and str(json.loads(r["response"]).get("AccountId", "")) == identity[0]
            and bool(json.loads(r["response"]).get("RequestId")) for r in signed
        ):
            return False
        call["params"][key] = identifier
        return known_absence_read({**dict(original), "call": json.dumps(call)})
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, sqlite3.Error):
        return False


def known_original_rejected_oss_detail_read(connection, row) -> bool:
    """A definite execute-stage signature rejection is not an unknown write.

    This narrowly permits investigation/cleanup of the original creation. It
    never proves absence, satisfies a cleanup read, enables source continuation,
    rewrites the error, or replenishes an allowance. The original signed
    identity and every consumed read attempt remain required.
    """
    try:
        call, response = json.loads(row["call"]), json.loads(row["response"])
        params, region = call.get("params"), call.get("region_id")
        if (row["kind"] != "read" or row["state"] != "unknown"
                or call.get("product") != "oss" or call.get("action") != "GetBucketInfo"
                or call.get("version") != "2019-05-17"
                or not isinstance(params, dict) or set(params) != {"bucket"}
                or not isinstance(params["bucket"], str)
                or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", params["bucket"]) is None
                or not isinstance(region, str) or re.fullmatch(r"cn-[a-z0-9-]+", region) is None
                or not isinstance(response, dict) or set(response) != {"status", "body", "headers"}
                or type(response["status"]) is not int or response["status"] != 403):
            return False
        body, headers = response["body"], response["headers"]
        if not isinstance(body, dict) or not isinstance(headers, dict):
            return False
        request_id = body.get("RequestId")
        if (body.get("Code") != "SignatureDoesNotMatch" or body.get("EC") != "0002-00000201"
                or body.get("HostId") != params["bucket"] + ".oss-" + region + ".aliyuncs.com"
                or not isinstance(request_id, str) or not request_id or request_id.strip() != request_id
                or any(not isinstance(key, str) for key in headers)
                or [value for key, value in headers.items() if key.casefold() == "x-oss-request-id"] != [request_id]
                or ("statusCode" in body and body["statusCode"] != response["status"])):
            return False
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        original = cursor.execute(
            "SELECT a.*,g.kind,g.call,g.authority,g.quota,g.used,g.read_policy FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.id=?",
            (row["scope"], row["id"]),
        ).fetchone()
        if (not original or any(original[key] != row[key] for key in ("kind", "state", "call", "response"))
                or not original["session"] or not original["credential"] or not original["authority"]):
            return False
        policy = read_policy(original)
        if policy.get("phase") != "execute" or policy.get("required_cleanup"):
            return False
        identity = cursor.execute(
            "SELECT 1 FROM identities i JOIN active a ON a.scope=i.scope AND a.session=i.session "
            "AND a.account=i.account WHERE i.scope=? AND i.session=? AND i.credential=?",
            (original["scope"], original["session"], original["credential"]),
        ).fetchone()
        limit = cursor.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                               (original["authority"], original["grant_id"])).fetchone()
        count = cursor.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?",
                               (original["scope"], original["grant_id"])).fetchone()[0]
        return bool(identity and limit and 0 < count == original["used"] <= limit[0] <= original["quota"])
    except (KeyError, TypeError, ValueError, AttributeError, sqlite3.Error):
        return False


def execution_request(approved_text: str) -> str:
    """Keep the reviewed request intact, but make its current phase unambiguous.

    Older packets describe the whole lifecycle in one request. The product
    cannot observe the worker's later browser clicks, so it must stop after
    execution; the existing wire gate independently rejects premature deletes.
    """
    phase = (
        "当前是执行及引用生成阶段，不是清理阶段。仅执行下面已审批的创建或只读计划，"
        "取得真实状态和结构化资源引用后立即结束本次回答，保留资源供外部执行者导航截图。"
        "你无法观察外部执行者的点击；不得自行认定采样完成，不得在本次回答里删除或清理。"
        "旧计划中涉及采样后清理的文字仅描述后续流程，不是当前删除指令。"
        "只有收到单独的清理请求且程序切换清理阶段后，才执行已审批的响应绑定删除。"
    )
    return phase + "\n\n已审批请求：\n" + approved_text + "\n\n" + phase


def assert_navigation_before_cleanup(output: Path, dimension: str) -> None:
    """Do not click a now-deleted sample and misclassify it as a link defect.

    This never prevents cleanup: failure screenshots and the cleanup operation
    remain available, and no model deadline or cloud allowance is changed.
    """
    if (output / f"{dimension}-cleanup-intent.json").exists():
        raise WorkflowError(
            "navigation must precede explicit cleanup; preserve prior evidence and submit the execution gap, "
            "do not click a deleted sample or recreate it"
        )


def original_stack_read_body(connection, row, create, *, require_active_identity=True):
    """Project an original, response-bound ResourceGroup or managed NAS stack read.

    Historical raw bodies retain their existing semantics. New envelopes need
    original grant, parent, full session and actual signer/account provenance;
    this does not change the journal or classify an error as successful.
    """
    try:
        response = json.loads(row["response"])
        if not isinstance(response, dict):
            return None
        protected = (resource_group_scope(connection, create["scope"])
                     or nas_managed_scope(connection, create["scope"])
                     or mse_managed_scope(connection, create["scope"]))
        if not protected and not any(key in response for key in ("body", "status", "headers", "call")):
            return response
        call = json.loads(row["call"])
        parent_call = json.loads(create["call"])
        identifier = json.loads(create["response"]).get("StackId")
        if (
            not protected
            or call.get("product") != "ros" or call.get("version") != "2019-09-10"
            or call.get("action") != "GetStack" or call.get("region_id") != parent_call.get("region_id")
            or row["parent"] != create["grant_id"] or row["target_param"] != "StackId"
            or json.loads(row["response_path"]) != ["StackId"]
            or call["params"].get("StackId") != "$created_id"
            or not isinstance(identifier, str) or not identifier
            or not create["session"] or not create["credential"]
            or (row["scope"], row["session"]) != (create["scope"], create["session"])
        ):
            return None
        identity = connection.execute(
            "SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
            (create["scope"], create["session"], create["credential"]),
        ).fetchone()
        if not identity:
            return None
        if row["credential"] != create["credential"]:
            # A post-delete signer rotation was already independently reviewed
            # for original MSE cleanup reads. Recheck the same immutable bridge
            # here; readiness must not silently fall back to an older pending
            # status merely because the valid terminal read uses that signer.
            if not mse_managed_scope(connection, create["scope"]):
                return None
            cursor = connection.cursor()
            cursor.row_factory = sqlite3.Row
            original = cursor.execute(
                "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param,g.read_policy,g.authority "
                "FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE a.id=? AND a.scope=?", (row["id"], row["scope"]),
            ).fetchone()
            if not original or any(original[key] != row[key] for key in (
                "scope", "session", "credential", "state", "response", "call", "created",
                "kind", "parent", "response_path", "target_param",
            )):
                return None
            active = {"scope": create["scope"], "session": create["session"], "account": identity[0]}
            parent = mse_managed_parent(cursor, active, original["parent"])
            if (not parent or parent["id"] != create["id"] or not mse_rotated_cleanup_identity(
                cursor, active, original, parent, original["credential"], original["reference_scope"],
                before=original["created"],
            )):
                return None
        if require_active_identity:
            if not connection.execute(
                "SELECT 1 FROM active WHERE scope=? AND session=? AND account=?",
                (create["scope"], create["session"], identity[0]),
            ).fetchone():
                return None
        else:
            # Earlier dimensions no longer own the single active slot. Verify
            # their immutable pre-create signed identity instead, not the next
            # dimension's credentials or an account string supplied by a model.
            originals = connection.execute(
                "SELECT a.response,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE a.scope=? AND a.session=? AND a.credential=? AND a.state='succeeded' "
                "AND g.kind='identity' AND a.created<?",
                (create["scope"], create["session"], create["credential"], create["created"]),
            ).fetchall()
            if not any(
                json.loads(item["call"]).get("product") == "sts"
                and json.loads(item["call"]).get("action") == "GetCallerIdentity"
                and json.loads(item["response"]).get("AccountId") == identity[0]
                and json.loads(item["response"]).get("RequestId")
                for item in originals
            ):
                return None
        call["params"]["StackId"] = identifier
        body = successful_resource_group_body(row, call)
        if body and mse_managed_scope(connection, create["scope"]) and isinstance(body.get("Stack"), dict):
            nested = body["Stack"]
            if any(key in body and key in nested and body[key] != nested[key]
                   for key in ("StackId", "Status", "StackType", "RegionId", "AccountId")):
                return None
            body = nested
        return body if body and body.get("StackId") == identifier else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def assert_stack_source_ready(path: Path, scope: str, *, cleanup: bool) -> None:
    """An idle answer is not the completion of its asynchronous cloud stack.

    Do not freeze an incomplete source as the sole immutable evidence. Failed
    terminal cloud responses remain capturable; this guard only defers a live
    asynchronous operation, without changing any deadline or API allowance.
    """
    if not path.is_file():
        return
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? ORDER BY a.created",
            (scope,),
        ).fetchall()
        if any(row["state"] != "succeeded" and not known_original_resolved_read(connection, row) for row in rows):
            return  # Preserve real failed/unknown responses; never hide them as pending.
        creates = [
            row for row in rows if row["kind"] == "create" and row["state"] == "succeeded"
            and json.loads(row["call"]).get("action") == "CreateStack"
        ]
        protected = (resource_group_scope(connection, scope) or nas_managed_scope(connection, scope)
                     or mse_managed_scope(connection, scope))
        for create in creates:
            identifier = json.loads(create["response"]).get("StackId")
            deletes = [
                row for row in rows if row["kind"] == "delete" and row["state"] == "succeeded"
                and (not protected or row["parent"] == create["grant_id"])
            ]
            after = (
                max([row["created"] for row in deletes], default=create["created"]) if cleanup else create["created"]
            )
            statuses = []
            for row in rows:
                if (cleanup and row["created"] > after and _known_original_nas_stack_absence_read(connection, row)):
                    statuses.append({"original_exact_stack_not_found": True})
                    continue
                if (row["state"] != "succeeded" or row["kind"] != "read"
                        or json.loads(row["call"]).get("action") != "GetStack" or row["created"] <= after):
                    continue
                body = original_stack_read_body(connection, row, create)
                if body and body.get("StackId") == identifier:
                    statuses.append(body)
                elif protected:
                    # A newer malformed proof cannot be replaced by an older
                    # successful status. Preserve all originals, defer capture.
                    statuses.append({})
            if statuses and statuses[-1].get("original_exact_stack_not_found"):
                continue  # Readiness only; original404/unknown and independent absence decision stay unchanged.
            latest = statuses[-1].get("Status", "") if statuses else ""
            if not latest or latest.endswith("_IN_PROGRESS") or (cleanup and latest == "CREATE_COMPLETE"):
                raise WorkflowError(
                    "cloud stack is still pending; use continue-read with the same dimension and stage "
                    "to finish original response-bound reads before saving source; never recreate or repeat deletion"
                )


def assert_cleanup_reads_complete(path: Path, scope: str) -> None:
    """Require each reviewed post-delete check, not a model's cleanup summary.

    This proves execution/completeness only. Independent cloud and visual
    verification still decides absence and acceptance. Legacy grants are kept
    intact; no missing policy is invented for a historical scope.
    """
    if not path.is_file():
        return
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        grants = connection.execute("SELECT * FROM grants WHERE scope=?", (scope,)).fetchall()
        rows = connection.execute(
            "SELECT a.*,g.kind,g.call,g.parent FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? ORDER BY a.created", (scope,),
        ).fetchall()
        for grant in grants:
            policy = read_policy(grant)
            if not policy.get("required_cleanup"):
                continue
            parent = policy["after_delete"]
            if not any(row["grant_id"] == parent and row["state"] == "succeeded" for row in rows):
                continue  # A rejected/unexecuted creation is not an owned object.
            deletes = [
                row["created"] for row in rows
                if row["kind"] == "delete" and row["parent"] == parent and row["state"] == "succeeded"
            ]
            if not deletes or not any(
                row["grant_id"] == grant["id"] and row["created"] > max(deletes)
                and (row["state"] == "succeeded" or known_original_resolved_read(connection, row))
                for row in rows
            ):
                raise WorkflowError(
                    "required post-delete read is missing or unresolved: " + grant["id"]
                    + "; preserve original evidence, use remaining cleanup continue-read; never repeat a write"
                )


def missing_cleanup_read_delivery(connection, grants, results) -> list[dict]:
    """Project unfulfilled mandatory reads from one unchanged journal snapshot.

    Deliver missing checks before repeating already-observed asynchronous state.
    This is execution guidance, not proof of absence, a quota refill or a new
    capability. The transport gate still verifies each actual signed call.
    """
    pending = []
    for grant in grants:
        policy = read_policy(grant)
        if grant["kind"] != "read" or not policy.get("required_cleanup"):
            continue
        parent = policy["after_delete"]
        creates = [row for row in results if row["grant_id"] == parent and row["state"] == "succeeded"]
        if not creates:
            continue
        deletes = [row["created"] for row in results
                   if row["kind"] == "delete" and row["parent"] == parent and row["state"] == "succeeded"]
        if not deletes:
            raise WorkflowError("required cleanup delivery needs the successful original deletion")
        if any(row["grant_id"] == grant["id"] and row["created"] > max(deletes)
               and (row["state"] == "succeeded" or known_original_resolved_read(connection, row))
               for row in results):
            continue
        limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?", (grant["authority"], grant["id"])
        ).fetchone()
        if (not phase_allowed(grant, "cleanup") or not limit
                or not 0 <= grant["used"] <= limit[0] < grant["quota"]):
            raise WorkflowError("missing required cleanup read has no original allowance: " + grant["id"])
        call = json.loads(grant["call"])
        if nas_native_binding(grant):
            active = connection.execute("SELECT * FROM active WHERE scope=?", (grant["scope"],)).fetchone()
            if not active:
                raise WorkflowError("native cleanup read needs the original active signed session")
            try:
                call = nas_native_resolve(connection, active, grant, nas_native_signer(connection, active))
            except GateDeniedError as error:
                raise WorkflowError("native cleanup read needs its original response-bound dependencies") from error
            pending.append({"id": grant["id"], "call": call,
                            "remaining": grant["quota"] - limit[0], "read_policy": policy})
            continue
        if grant["parent"]:
            parents = [row for row in results
                       if row["grant_id"] == grant["parent"] and row["state"] == "succeeded"]
            if len(parents) != 1:
                raise WorkflowError("mandatory cleanup read needs one original creation response")
            binding = json.loads(grant["response_path"])
            if isinstance(binding, dict) and binding.get("kind") in {"nas_managed_child", "mse_managed_child"}:
                parent = parents[0]
                identity = connection.execute(
                    "SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
                    (parent["scope"], parent["session"], parent["credential"]),
                ).fetchone()
                active = {"scope": parent["scope"], "session": parent["session"],
                          "account": identity[0] if identity else None}
                parent_reader, params_reader = ((mse_managed_parent, mse_managed_params)
                                                if binding["kind"] == "mse_managed_child"
                                                else (nas_managed_parent, nas_managed_params))
                proven_parent = parent_reader(connection, active, grant["parent"])
                params = params_reader(connection, active, proven_parent, grant) if proven_parent else None
                if not params:
                    raise WorkflowError("mandatory managed cleanup read needs original signed pre-delete child proof")
                call["params"].update(params)
                pending.append({"id": grant["id"], "call": call,
                                "remaining": grant["quota"] - limit[0], "read_policy": policy})
                continue
            identifier = response_identifier(
                json.loads(parents[0]["call"]), json.loads(parents[0]["response"]),
                json.loads(grant["response_path"]),
            )
            target = grant["target_param"]
            if not identifier or call.get("params", {}).get(target) != "$created_id":
                raise WorkflowError("mandatory cleanup read original response binding changed")
            call["params"][target] = identifier
        pending.append({"id": grant["id"], "call": call,
                        "remaining": grant["quota"] - limit[0], "read_policy": policy})
    return pending


def managed_source_delivery(connection, active, grants) -> str:
    """Tell the Web executor the next dependency group, not just the external GLM.

    Only names of already-approved reads are delivered. Actual parameters and
    remaining authority continue to be enforced by the original machine gate.
    """
    reads = [g for g in grants if g["kind"] == "read" and phase_allowed(g, "execute")
             and json.loads(g["call"])["action"] != "GetStack"]
    groups = [
        [g for g in reads if json.loads(g["call"])["action"] == "ListStackResources"],
        [g for g in reads if json.loads(g["call"])["action"] == "GetStackResource"],
        [g for g in reads if json.loads(g["call"])["product"] != "ros"],
    ]
    completed = {r[0] for r in connection.execute(
        "SELECT DISTINCT grant_id FROM attempts WHERE scope=? AND state='succeeded'", (active["scope"],),
    )}
    pending = next(([g for g in group if g["id"] not in completed and g["used"] < g["quota"]] for group in groups
                    if any(g["id"] not in completed and g["used"] < g["quota"] for g in group)), [])
    if not pending:
        raise WorkflowError("all original MSE source groups executed; capture source, do not repeat reads")
    names = [json.loads(g["call"])["product"] + " " + json.loads(g["call"])["action"] for g in pending]
    logical = [json.loads(g["response_path"])["logical_ids"][json.loads(g["response_path"])["child"]]
               for g in pending if json.loads(g["call"])["action"] == "GetStackResource"]
    return (
        "本次为OPS已审原代次的只读顺序恢复，替代旧消息中泛泛的继续查询，不是新建或清理。"
        "之前实例详情被拒是因为跳过编排成员，本次必须先补依赖，不能重复该被拒步骤。"
        "原身份与创建已成功，禁止再次STS、CreateStack、DeleteStack，不重读已成功终态GetStack。"
        "本次仅执行以下当前机器契约内尚未调用的读取，各一次，取得真实响应后立即结束回答："
        + "；".join(names) + ("。各自逻辑资源名依次为" + "、".join(logical) if logical else "")
        + "。StackId及子资源参数严格由原成功创建和原成员响应绑定，不凭历史文字猜测。"
        "此组以外的查询（包括实例详情在成员组完成之前）全部禁止；后续组会收到独立下一条请求。"
        "保留原真实失败，不创建、不删除、不修改资源，不判断外部点击或通过。"
    )


def assert_managed_source_reads_complete(path: Path, scope: str) -> None:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if not mse_managed_scope(db, scope):
            raise WorkflowError("exact original managed MSE scope required")
        for grant in db.execute("SELECT * FROM grants WHERE scope=?", (scope,)):
            if (grant["kind"] != "read" or not phase_allowed(grant, "execute")
                    or json.loads(grant["call"])["action"] == "GetStack"):
                continue
            if not db.execute("SELECT 1 FROM attempts WHERE scope=? AND grant_id=? AND state='succeeded'",
                              (scope, grant["id"])).fetchone():
                raise WorkflowError("MSE member/detail reads incomplete; continue-read TF source next group first")


def readonly_continuation(path: Path, slot: str, scope: str, session: str, stage: str) -> str:
    """Continue remaining reads without installing a new scope or write allowance."""
    if stage not in {"source", "cleanup"}:
        raise WorkflowError("continue-read stage must be source or cleanup")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        active = connection.execute("SELECT * FROM active WHERE slot=?", (slot,)).fetchone()
        expected_phase = "execute" if stage == "source" else "cleanup"
        if not active or (active["scope"], active["session"], active["phase"]) != (scope, session, expected_phase):
            raise WorkflowError("continue-read requires the original active scope, full session and phase")
        if active["expires"] is not None and active["expires"] <= time.time():
            raise WorkflowError("original capability expired; no automatic renewal")
        original_results = connection.execute(
            "SELECT a.*,g.kind,g.call,g.parent FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=?",
            (scope,),
        ).fetchall()
        if any(row["state"] != "succeeded" and not known_original_resolved_read(connection, row)
               and not (stage == "cleanup" and known_original_rejected_oss_detail_read(connection, row))
               for row in original_results):
            raise WorkflowError("unresolved original API result; no continuation or write retry")
        grants = connection.execute("SELECT * FROM grants WHERE scope=?", (scope,)).fetchall()
        writes = [
            grant for grant in grants if grant["kind"] == "create" or (stage == "cleanup" and grant["kind"] == "delete")
        ]
        if not writes or not any(grant["kind"] == "create" for grant in writes):
            raise WorkflowError("continue-read requires the successful original creation")
        for grant in writes:
            used = connection.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?", (grant["authority"], grant["id"])
            ).fetchone()
            succeeded = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=? AND state='succeeded'", (scope, grant["id"])
            ).fetchone()[0]
            if not used or used[0] != grant["quota"] or grant["used"] != grant["quota"] or succeeded != grant["quota"]:
                raise WorkflowError("all original phase writes must already be consumed and successful")
        remaining = any(
            grant["kind"] == "read"
            and phase_allowed(grant, expected_phase)
            and grant["used"] < grant["quota"]
            and (
                limit := connection.execute(
                    "SELECT used FROM authority_limits WHERE authority=? AND id=?", (grant["authority"], grant["id"])
                ).fetchone()
            )
            and limit[0] < grant["quota"]
            for grant in grants
        )
        if not remaining:
            raise WorkflowError("original read allowance exhausted; no refill or new creation")
        if stage == "source" and mse_managed_scope(connection, scope):
            return managed_source_delivery(connection, active, grants)
        missing = missing_cleanup_read_delivery(connection, grants, original_results) if stage == "cleanup" else []
    delivery = (
        "\n程序按本次原始 journal 快照列出尚未实际完成的必查读取。先逐项执行下面精确调用各一次，"
        "不要继续重复已经观察过的状态接口而遗漏这些项。取得真实响应后停止并保存原证；"
        "异步状态仍未终态时再由下一次 continue-read 核对原剩余额度，不自动循环或重复写操作。"
        "这些项只证明必查调用的执行完整性，不证明消失或通过；PendingDelete 仍保留实际状态。\n"
        + json.dumps({"kind": "original_missing_cleanup_reads", "calls": missing,
                      "absence_verified": False, "allowance_changed": False}, ensure_ascii=False, separators=(",", ":"))
        if missing else ""
    )
    return (
        "继续刚才同一维度、同一完整会话的原始响应绑定只读核查，不是新的创建或删除请求。"
        "原阶段全部写操作已成功执行且额度已耗尽，禁止再次创建、删除、改名或修改资源。"
        "只使用当前机器契约列出的原剩余只读调用；身份绑定仍由机器核验，不绕过或重置身份额度。"
        "编排尚在进行时按原合法读取间隔留给云完成，不连续耗尽次数；取得真实终态后读取原栈子资源"
        "及本阶段必要精确详情或消失证据，不凭模板推断标识。明确失败、未知结果或额度不足如实停止。"
        "保存真实响应与引用后停止，不执行导航或推测外部截图进度。"
        + delivery
    )
