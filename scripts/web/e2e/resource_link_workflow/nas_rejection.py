"""Reconcile an explicit rejected CPFS order, never an ambiguous cloud write.

The original wire and its consumed allowance stay unchanged. Independent,
same-account inventory of the task's own isolated VPC proves no CPFS remains;
this only permits diagnosis, not another order or a navigation verdict.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow import execution_gate as gate

from .files import WorkflowError, contained, digest, read_json
from .ownership import owner_key

KIND = "ops_independent_rejected_native_nas_create_absence"
LOADED_SHA = digest(Path(__file__))


def document(root: Path, ref: dict[str, Any]) -> dict[str, Any]:
    path = contained(root / "inputs", ref.get("path", "").removeprefix("inputs/"))
    if not path.is_file() or digest(path) != ref.get("sha256"):
        raise WorkflowError("NAS rejection original evidence changed")
    return read_json(path)


def validate(root: Path, state: sqlite3.Connection, task_id: str, audit: dict[str, Any]) -> set[str]:
    job_id = audit.get("original_job", "")
    job = state.execute("SELECT task_id,role,state,turn_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    task = state.execute("SELECT spec FROM tasks WHERE id=?", (task_id,)).fetchone()
    if (audit.get("kind") != KIND or audit.get("task_id") != task_id
            or audit.get("schema_version") != 1 or audit.get("approved") is not True
            or audit.get("confirmed_no_residual") is not True or audit.get("no_journal_mutation") is not True
            or audit.get("new_creation_authorized") is not False or audit.get("pass_added") is not False
            or not job or tuple(job[:3]) != (task_id, "glm", "complete") or not job[3]
            or not task or json.loads(task[0]).get("key") != "ALIYUN::NAS::DataFlow"):
        raise WorkflowError("NAS rejection needs its exact terminal native-query task")
    originals = gate.creations(root / "execution-gate.sqlite3", [job_id])
    records = [r for r in originals if r["attempt_id"] == audit.get("attempt_id")]
    if len(records) != 1 or records[0] != document(root, audit.get("wire_source", {})):
        raise WorkflowError("NAS rejection must preserve the full original creation wire")
    record = records[0]
    response = record.get("response") or {}
    call, body = response.get("call", {}), response.get("body", {})
    params = call.get("params", {})
    request_id = body.get("RequestId")
    account, region = record["resource"]["account"], record["resource"]["region"]
    if (record["dimension"] != "QUERY" or record["state"] != "unknown" or record["resource"]["id"] is not None
            or (call.get("product"), call.get("action"), call.get("version"), call.get("region_id"))
            != ("nas", "CreateFileSystem", "2017-06-26", "cn-hangzhou")
            or region != "cn-hangzhou" or not account or audit.get("actual_account") != account
            or response.get("status") != 400 or body.get("statusCode") != 400
            or body.get("Code") != "COMMODITY.INVALID_COMPONENT"
            or body.get("HostId") != "nas.cn-hangzhou.aliyuncs.com"
            or not request_id or response.get("headers", {}).get("x-acs-request-id") != request_id
            or body.get("FileSystemId") or params.get("DryRun") is not False
            or params.get("FileSystemType") != "cpfs" or params.get("ProtocolType") != "cpfs"
            or params.get("StorageType") != "advance_200" or params.get("Capacity") != 3600
            or params.get("Bandwidth") != 720 or params.get("ChargeType") != "PayAsYouGo"):
        raise WorkflowError("only the explicit original CPFS commodity HTTP400 rejection is reconciliable")
    try:
        message = json.loads(body["Message"])
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowError("NAS rejection needs the unaltered commodity error") from error
    if (message.get("code") != body["Code"] or message.get("requestId") != request_id
            or message.get("success") is not False or not message.get("message")):
        raise WorkflowError("NAS commodity rejection request binding differs")
    with closing(sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True)) as wire:
        wire.row_factory = sqlite3.Row
        scope = job_id + ":QUERY"
        if not gate.nas_native_scope(wire, scope):
            raise WorkflowError("NAS rejection is not the original finite native graph")
        row = wire.execute("SELECT * FROM attempts WHERE id=? AND scope=?", (record["attempt_id"], scope)).fetchone()
        identity = wire.execute("SELECT a.* FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                                "WHERE a.scope=? AND g.kind='identity'", (scope,)).fetchall()
        grant = wire.execute("SELECT * FROM grants WHERE scope=? AND id='create-filesystem'", (scope,)).fetchone()
        if not row or len(identity) != 1 or not grant:
            raise WorkflowError("NAS rejection lacks original signed identity and exact create grant")
        signed = identity[0]
        limit = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                             (grant["authority"], grant["id"])).fetchone()
        if (row["grant_id"] != grant["id"] or row["state"] != "unknown" or signed["state"] != "succeeded"
                or any(signed[k] != row[k] for k in ("scope", "session", "credential", "reference_scope"))
                or row["session"] != record["session_id"] or row["tool_use_id"] != record["tool_use_id"]
                or not row["tool_use_id"] or not row["reference_scope"]
                or json.loads(signed["response"]).get("body", {}).get("AccountId") != account
                or signed["created"] >= row["created"] or grant["quota"] != 1 or grant["used"] != 1
                or not limit or limit[0] != 1
                or wire.execute("SELECT count(*) FROM attempts WHERE scope=? AND grant_id=?",
                                (scope, grant["id"])).fetchone()[0] != 1
                or not wire.execute("SELECT 1 FROM identities WHERE scope=? AND session=? "
                                    "AND credential=? AND account=?",
                                    (scope, row["session"], row["credential"], account)).fetchone()):
            raise WorkflowError("NAS original identity, signer, session or consumed allowance differs")
        try:
            active = {"scope": scope, "session": row["session"], "account": account}
            expected = gate.nas_native_resolve(wire, active, grant, signed)
        except gate.GateDeniedError as error:
            raise WorkflowError("NAS original response-bound prerequisites differ") from error
        if call != expected:
            raise WorkflowError("NAS original call differs from the exact resolved grant")
        created_at = row["created"]
    # Both parent IDs must originate in this very GLM's successful create chain,
    # and their own generation must already have verified cleanup committed.
    for action, key in (("CreateVpc", "VpcId"), ("CreateVSwitch", "VSwitchId")):
        parents = [r for r in originals if r["resource"].get("action") == action]
        if (len(parents) != 1 or parents[0]["state"] != "succeeded"
                or parents[0]["resource"]["id"] != params.get(key)
                or parents[0]["session_id"] != record["session_id"]
                or parents[0]["resource"]["account"] != account):
            raise WorkflowError("NAS rejection inventory must bind its own isolated response-created network")
        owner_id = owner_key(state, root, task_id, "QUERY", parents[0]["resource"], job_id)
        owner = state.execute("SELECT resource,evidence FROM owned WHERE id=? AND task_id=? AND state='deleted'",
                              (owner_id, task_id)).fetchone()
        if not owner or json.loads(owner[0]) != parents[0]["resource"]:
            raise WorkflowError("NAS rejected order needs verified original network cleanup")
        cleanup = document(root, json.loads(owner[1]))
        source = contained(root, cleanup.get("source_evidence", ""))
        if (cleanup.get("resource") != parents[0]["resource"] or cleanup.get("absent") is not True
                or cleanup.get("failed") is not False or not source.is_file()
                or digest(source) != cleanup.get("source_sha256")):
            raise WorkflowError("NAS rejected order original network cleanup changed")
    report = document(root, audit.get("independent_absence", {}))
    if (report.get("kind") != "ops_rejected_native_filesystem_absence_result"
            or report.get("task_id") != task_id or report.get("original_job_id") != job_id
            or report.get("account") != account or report.get("region") != region
            or report.get("vpc_id") != params["VpcId"] or report.get("cloud_writes") != 0
            or report.get("formal_test") is not False or report.get("pass_added") is not False
            or report.get("failure_type") is not None or len(report.get("events", [])) != 2):
        raise WorkflowError("NAS rejection needs bounded independent same-account absence")
    bodies = []
    expected_calls = [
        {"product": "sts", "action": "GetCallerIdentity", "version": "2015-04-01", "params": {}},
        {"product": "nas", "action": "DescribeFileSystems", "version": "2017-06-26",
         "params": {"VpcId": params["VpcId"], "FileSystemType": "cpfs", "PageNumber": 1, "PageSize": 100}},
    ]
    for event, expected in zip(report["events"], expected_calls):
        raw = document(root, event)
        body = raw.get("body", {})
        if (raw.get("call") != expected or event.get("exit_code") != 0 or raw.get("exit_code") != 0
                or not body.get("RequestId") or body["RequestId"] != event.get("request_id")
                or type(raw.get("started_at")) not in (int, float)
                or type(raw.get("completed_at")) not in (int, float)
                or raw["started_at"] <= created_at or raw["completed_at"] < raw["started_at"]
                or any(k in body for k in ("Code", "ErrorCode"))):
            raise WorkflowError("NAS independent absence raw call or chronology differs")
        created_at = raw["completed_at"]
        bodies.append(body)
    inventory = bodies[1]
    if (bodies[0].get("AccountId") != account or inventory.get("FileSystems") != {"FileSystem": []}
            or type(inventory.get("TotalCount")) is not int or inventory["TotalCount"] != 0
            or inventory.get("PageNumber") != 1 or inventory.get("PageSize") != 100
            or inventory.get("NextToken") or inventory.get("IsTruncated") not in (None, False)):
        raise WorkflowError("NAS independent account or complete zero CPFS inventory differs")
    return {record["attempt_id"]}
