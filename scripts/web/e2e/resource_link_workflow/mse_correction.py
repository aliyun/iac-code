"""One reviewed dev1 -> pro3 hosted MSE correction, with actual OPS cleanup.

This reads the immutable predecessor journal. It never credits the OPS delete
as a model call, changes an old allowance, or treats a proposal as a test PASS.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

from .files import WorkflowError, contained, digest, read_json

KIND = "ops_mse_tf_professional_correction"
CLEANUP_KIND = "ops_external_failed_mse_stack_cleanup_provenance"
RECONCILIATION_KIND = "ops_reconciled_failed_managed_mse_member"
LOADED_SHA = digest(Path(__file__))


def _document(root: Path, ref: dict[str, Any]) -> dict[str, Any]:
    path = contained(root, ref.get("path", "missing"))
    if digest(path) != ref.get("sha256"):
        raise WorkflowError("MSE correction bound original evidence changed")
    return read_json(path)


def corrected_grants(original: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    """Exactly two literal changes; the original admission closes every other field."""
    result = json.loads(json.dumps(original))
    creates = [entry for entry in result if entry.get("kind") == "create"]
    if len(creates) != 1:
        raise WorkflowError("MSE correction needs one original hosted create")
    call = creates[0].get("call", {})
    before = call.get("params", {}).get("TemplateBody", "")
    template = json.loads(before)
    hcl = template.get("Workspace", {}).get("main.tf", "")
    if (
        call.get("product") != "ros"
        or call.get("action") != "CreateStack"
        or template.get("Transform") != "Aliyun::Terraform-v1.5"
        or set(template.get("Workspace", {})) != {"main.tf"}
        or 'version = "= 1.260.0"' not in hcl
        or re.findall(r'\bresource\s+"([^"]+)"', hcl) != ["alicloud_vpc", "alicloud_vswitch", "alicloud_mse_cluster"]
        or hcl.count('mse_version = "mse_dev"') != 1
        or hcl.count("instance_count = 1\n") != 1
    ):
        raise WorkflowError("only the original pinned private dev1 MSE template can be corrected")
    # Replace in the parsed HCL, then retain the original JSON serialization.
    after = before.replace('mse_version = \\"mse_dev\\"', 'mse_version = \\"mse_pro\\"')
    after = after.replace("instance_count = 1\\n", "instance_count = 3\\n")
    if json.loads(after)["Workspace"]["main.tf"] != hcl.replace(
        'mse_version = "mse_dev"',
        'mse_version = "mse_pro"',
    ).replace("instance_count = 1\n", "instance_count = 3\n"):
        raise WorkflowError("MSE correction must preserve the original serialized template")
    creates[0]["call"]["params"]["TemplateBody"] = after
    return result, before, after


def reviewed_mse_correction(
    root: Path,
    old: dict[str, Any],
    packet: dict[str, Any],
    request: dict[str, Any],
    proof: dict[str, Any],
) -> bool:
    from .hourly_budget import operation_review
    from .temporary_samples import reviewed_scope

    reviews = proof.get("temporary_template_corrections", {})
    review = reviews.get(request.get("request_id"), {})
    if (
        set(reviews) != {request.get("request_id")}
        or review.get("kind") != KIND
        or review.get("approved") is not True
        or review.get("task_id") != packet["task_id"]
        or request.get("dimension") != "TF"
        or packet.get("additional_source_requests") != [request]
        or set(packet.get("execution_grants", {})) != {"TF"}
        or set(packet.get("scenes", {})) != {packet["task_id"].rsplit(":", 1)[-1] + "-TF-1-iframe"}
        or not review.get("human_scope_interpretation")
        or request.get("external_cleanup") != review.get("external_cleanup")
    ):
        raise WorkflowError("one independently reviewed remaining MSE hosted TF iframe correction required")
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as state:
        state.row_factory = sqlite3.Row
        task = state.execute("SELECT * FROM tasks WHERE id=?", (packet["task_id"],)).fetchone()
        row = state.execute(
            "SELECT value FROM meta WHERE key=?", ("temporary_samples:" + packet["task_id"],)
        ).fetchone()
        if not task or not row:
            raise WorkflowError("MSE correction needs the immutable original temporary admission")
        saved = json.loads(row[0])
        run = state.execute("SELECT * FROM runs WHERE id=?", (task["run_id"],)).fetchone()
        if (
            reviewed_scope(root, task, run, saved["evidence"]) != saved
            or saved.get("kind") != "ops_hourly_managed_tf_sample_admission"
            or json.loads(task["spec"]).get("key") != "ALIYUN::MSE::Cluster"
            or review.get("original_admission") != {"path": saved["evidence"], "sha256": saved["evidence_sha256"]}
            or review.get("frozen_plan_sha256") != hashlib.sha256(run["plan"].encode()).hexdigest()
            or packet.get("budget_cents") != saved["budget_cents"]
            or {v["account"] for v in packet["scenes"].values()} != set(saved["accounts"])
            or old.get("task_id") != packet["task_id"]
            or old.get("execution_grants") != saved["execution_grants"]
        ):
            raise WorkflowError("MSE correction cannot replace original admission, account, calls or budget")
        if operation_review(root, state, task, packet, proof) is None:
            raise WorkflowError("MSE professional successor requires its own independent hourly review")
    expected, before, after = corrected_grants(old["execution_grants"]["TF"])
    if (
        packet["execution_grants"]["TF"] != expected
        or review.get("original_template_sha256") != hashlib.sha256(before.encode()).hexdigest()
        or review.get("corrected_template_sha256") != hashlib.sha256(after.encode()).hexdigest()
    ):
        raise WorkflowError("MSE professional correction changes only edition and node count, not quotas")
    with closing(sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True)) as gate:
        validate_external_cleanup(root, old, request, review.get("original_authority"), gate)
    for path in (root / "creation-authorizations").glob("*.json"):
        approval = read_json(path)
        if approval.get("task_id") != packet["task_id"]:
            continue
        previous = _document(
            root, {"path": approval["authorization_evidence"], "sha256": approval["authorization_sha256"]}
        )
        prior = previous.get("temporary_template_corrections", {})
        if any(v.get("kind") == KIND for v in prior.values()) and (
            prior != reviews or approval.get("additional_source_requests") != [request]
        ):
            raise WorkflowError("MSE original generation already has its single reviewed professional correction")
    return True


def _events(root: Path, report: dict[str, Any], calls: list[dict[str, Any]], after: float) -> list[dict[str, Any]]:
    events = report.get("events", [])
    if [event.get("call") for event in events] != calls:
        raise WorkflowError("MSE external cleanup exact bounded calls differ")
    bodies = []
    for event in events:
        body = _document(root, event)
        start, end = event.get("started_at"), event.get("completed_at")
        if (
            event.get("error") is not False
            or not body.get("RequestId")
            or event.get("request_id") != body["RequestId"]
            or type(start) not in (int, float)
            or type(end) not in (int, float)
            or start <= after
            or end < start
        ):
            raise WorkflowError("MSE external cleanup raw request/order/error evidence differs")
        after = end
        bodies.append(body)
    return bodies


def validate_external_cleanup(
    root: Path,
    old: dict[str, Any],
    request: dict[str, Any],
    authority: str,
    gate: sqlite3.Connection,
    *,
    reviewing_job_id: str | None = None,
) -> None:
    """Only a failed original hosted MSE generation, actually deleted by OPS."""
    from .creation import execution_digest

    evidence = _document(root, request.get("external_cleanup", {}))
    task, scope = old["task_id"], request["prior_scope"]
    if (
        evidence.get("kind") != CLEANUP_KIND
        or evidence.get("approved") is not True
        or evidence.get("task_id") != task
        or evidence.get("scope") != scope
        or evidence.get("authority") != authority
        or authority != execution_digest(old) + ":TF"
        or not re.fullmatch(r"[a-f0-9]{32}:TF", scope)
        or evidence.get("model_wire_unchanged") is not True
        or evidence.get("pass_added") is not False
    ):
        raise WorkflowError("MSE external cleanup requires exact independent original-generation provenance")
    entries = old["execution_grants"]["TF"]
    corrected_grants(entries)  # Refuse other resource families and successful/pro predecessors.
    create = next(entry for entry in entries if entry["kind"] == "create")
    delete = next(entry for entry in entries if entry["kind"] == "delete")
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as state:
        owner = state.execute(
            "SELECT task_id,dimension,state,resource,evidence FROM owned WHERE id=?", (evidence.get("ownership_id"),)
        ).fetchone()
        if not owner or owner[:3] != (task, "TF", "deleted") or json.loads(owner[4]) != evidence.get("owner_cleanup"):
            raise WorkflowError("MSE predecessor needs the exact committed deleted owner")
        if not state.execute(
            "SELECT 1 FROM jobs WHERE id=? AND task_id=? AND role='glm' AND state='complete' AND turn_id IS NOT NULL",
            (scope.split(":")[0], task),
        ).fetchone():
            raise WorkflowError("MSE predecessor GLM must be terminal")
        if reviewing_job_id is not None and (
            not state.execute(
                "SELECT 1 FROM jobs WHERE id=? AND task_id=? AND role='sol' AND purpose='review' "
                "AND state='running' AND turn_id IS NOT NULL",
                (reviewing_job_id, task),
            ).fetchone()
            or state.execute(
                "SELECT 1 FROM jobs WHERE task_id=? AND state!='complete' AND id!=?", (task, reviewing_job_id)
            ).fetchone()
        ):
            raise WorkflowError("historical MSE cleanup requires the exact sole active Sol review")
    resource = json.loads(owner[3])
    account, region, stack = resource["account"], resource["region"], resource["id"]
    cleanup = _document(root, evidence["owner_cleanup"])
    audit = _document(root, evidence["audit"])
    if (
        cleanup.get("ownership_id") != evidence["ownership_id"]
        or cleanup.get("resource") != resource
        or cleanup.get("absent") is not True
        or cleanup.get("failed") is not False
        or {"path": cleanup.get("source_evidence"), "sha256": cleanup.get("source_sha256")} != evidence["audit"]
        or audit.get("task_id") != task
        or audit.get("original_job_id") != scope.split(":")[0]
        or audit.get("account") != account
        or audit.get("region") != region
        or audit.get("original_wire_unchanged") is not True
        or audit.get("pass_added") is not False
        or {scene["account"] for scene in old["scenes"].values()} != {account}
        or create["call"].get("region_id") != region
    ):
        raise WorkflowError("MSE independent cleanup owner/audit/account binding differs")
    for ref in audit.get("sources", []):
        _document(root, ref)
    rows = gate.execute(
        "SELECT grant_id,state,response,created,session,credential FROM attempts WHERE scope=? ORDER BY created",
        (scope,),
    ).fetchall()
    ids = {entry["id"] for entry in entries}
    if any(row[0] not in ids for row in rows):
        raise WorkflowError("MSE original wire contains an orphaned call")
    for entry in entries:
        if gate.execute(
            "SELECT kind,call,authority FROM grants WHERE scope=? AND id=?", (scope, entry["id"])
        ).fetchone() != (
            entry["kind"],
            canonical(entry["call"]),
            authority,
        ):
            raise WorkflowError("MSE original installed call or authority changed")
    creates = [row for row in rows if row[0] == create["id"]]
    if len(creates) != 1 or any(row[1] != "succeeded" for row in rows):
        raise WorkflowError("MSE original actual wire must have one successful root create and no unknown calls")
    original_create = creates[0]
    if any(row[4:6] != original_create[4:6] for row in rows):
        raise WorkflowError("MSE original wire changed session or signer")
    if (
        json.loads(original_create[2]).get("StackId") != stack
        or not json.loads(original_create[2]).get("RequestId")
        or gate.execute(
            "SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
            (scope, original_create[4], original_create[5]),
        ).fetchone()
        != (account,)
    ):
        raise WorkflowError("MSE original signed create and identity differ")
    for entry, used in ((create, 1), (delete, 0)):
        if (
            gate.execute(
                "SELECT authority,quota,used FROM grants WHERE scope=? AND id=?", (scope, entry["id"])
            ).fetchone()
            != (authority, 1, used)
            or gate.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?", (authority, entry["id"])
            ).fetchone()
            != (used,)
            or len([row for row in rows if row[0] == entry["id"]]) != used
        ):
            raise WorkflowError("MSE correction must preserve original create1/delete0 counters")
    status_grants = [
        entry["id"]
        for entry in entries
        if entry.get("call", {}).get("action") == "GetStack"
        and entry.get("call", {}).get("product") == "ros"
        and entry.get("read_policy", {}).get("phase") == "execute"
    ]
    if len(status_grants) != 1:
        raise WorkflowError("MSE original execution needs its exact own stack status grant")
    failures = [json.loads(row[2]) for row in rows if row[0] == status_grants[0]]
    failures = [value.get("body", value) for value in failures]
    failures = [
        value
        for value in failures
        if value.get("StackId") == stack and value.get("Status") == "CREATE_ROLLBACK_COMPLETE"
    ]
    if len(failures) != 1 or (
        failures[0].get("OperationInfo", {}).get("Code"),
        failures[0].get("OperationInfo", {}).get("ResourceType"),
    ) != ("IllegalRequest", "alicloud_mse_cluster"):
        raise WorkflowError("MSE correction needs the actual rejected order and rollback terminal")
    failure = failures[0]
    if (
        failure.get("StackType") != "Terraform"
        or failure.get("RegionId") != region
        or (failure.get("StackName") != create["call"]["params"]["StackName"])
    ):
        raise WorkflowError("MSE failed source must identify its exact hosted stack and region")
    findings = audit.get("findings", {})
    vpc, vswitch = findings.get("vpc", {}).get("id"), findings.get("vswitch", {}).get("id")
    alias = create["call"]["params"]["StackName"]
    logs = "\n".join(str(log.get("Content", "")) for log in failure.get("Log", {}).get("TerraformLogs", []))
    if (
        not vpc
        or not vswitch
        or not re.search(r"alicloud_vpc\.sample_vpc: Creation complete[^\n]*\[id=" + re.escape(vpc) + r"\]", logs)
        or not re.search(
            r"alicloud_vswitch\.sample_vswitch: Creation complete[^\n]*\[id=" + re.escape(vswitch) + r"\]", logs
        )
        or re.search(r"alicloud_mse_cluster\.sample: Creation complete", logs)
    ):
        raise WorkflowError("MSE failed generation must bind actually created own networks, never a successful cluster")

    def call(product: str, action: str, version: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"product": product, "action": action, "version": version, "region_id": region, "params": params}

    get = call("ros", "GetStack", "2019-09-10", {"RegionId": region, "StackId": stack, "OutputOption": "Enabled"})
    identity = call("sts", "GetCallerIdentity", "2015-04-01", {})
    deletion = _document(root, evidence["delete_result"])
    post = _document(root, evidence["absence_result"])
    if (
        deletion.get("task_id") != task
        or deletion.get("original_job_id") != scope.split(":")[0]
        or deletion.get("original_wire_unchanged") is not True
        or deletion.get("maximum_delete_calls") != 1
        or post.get("task_id") != task
        or post.get("cloud_writes") != 0
    ):
        raise WorkflowError("MSE cleanup reports must retain exact task and single OPS delete")
    bodies = _events(
        root,
        deletion,
        [
            identity,
            get,
            call("ros", "ListStackResources", "2019-09-10", {"RegionId": region, "StackId": stack}),
            call(
                "ros", "DeleteStack", "2019-09-10", {"RegionId": region, "StackId": stack, "RetainAllResources": False}
            ),
            get,
        ],
        max(row[3] for row in rows),
    )
    if (
        bodies[0].get("AccountId") != account
        or bodies[1].get("StackId") != stack
        or bodies[1].get("Status") != "CREATE_ROLLBACK_COMPLETE"
        or bodies[4].get("StackId") != stack
        or bodies[4].get("Status") != "DELETE_COMPLETE"
    ):
        raise WorkflowError("MSE OPS same-account original root deletion was not independently terminal")
    negatives = _events(
        root,
        post,
        [
            identity,
            call(
                "vpc", "DescribeVpcs", "2016-04-28", {"RegionId": region, "VpcId": vpc, "PageNumber": 1, "PageSize": 10}
            ),
            call(
                "vpc",
                "DescribeVSwitches",
                "2016-04-28",
                {"RegionId": region, "VSwitchId": vswitch, "PageNumber": 1, "PageSize": 10},
            ),
            call(
                "mse",
                "ListClusters",
                "2019-05-31",
                {"RegionId": region, "ClusterAliasName": alias, "PageNum": 1, "PageSize": 10},
            ),
        ],
        deletion["events"][-1]["completed_at"],
    )
    if (
        negatives[0].get("AccountId") != account
        or any(v.get("TotalCount") != 0 for v in negatives[1:])
        or negatives[1].get("Vpcs") != {"Vpc": []}
        or negatives[2].get("VSwitches") != {"VSwitch": []}
        or negatives[3].get("Data") != []
        or negatives[3].get("Success") is not True
    ):
        raise WorkflowError("MSE exact original networks and target alias need real post-delete empty evidence")


def reconciled_failed_member(
    root: Path, connection: sqlite3.Connection, task_id: str, audit: dict[str, Any],
) -> set[str]:
    """Resolve only the synthetic member of an already-cleaned rejected dev stack.

    The root was really created, not rejected. Reuse the full original-generation
    cleanup validator instead of relabeling its wire or accepting a generic
    uncertainty summary. A later generation retains its independent owner checks.
    """
    from scripts.web.e2e.resource_link_workflow.execution_gate import creations

    from .ownership import evidence_generation, wire_evidence_generation

    old = _document(root, audit.get("predecessor", {}))
    request = audit.get("cleanup_request", {})
    scope = request.get("prior_scope", "")
    generation = scope.split(":")[0]
    if (
        audit.get("schema_version") != 1 or audit.get("kind") != RECONCILIATION_KIND
        or audit.get("task_id") != task_id or old.get("task_id") != task_id
        or audit.get("original_job_id") != generation or request.get("dimension") != "TF"
        or audit.get("model_wire_unchanged") is not True or audit.get("pass_added") is not False
    ):
        raise WorkflowError("MSE member reconciliation requires exact original task/generation, not a verdict")
    with closing(sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True)) as gate:
        validate_external_cleanup(root, old, request, audit.get("original_authority"), gate)
    records = creations(root / "execution-gate.sqlite3", [generation])
    if records != audit.get("original_records") or len(records) != 2:
        raise WorkflowError("MSE reconciliation cannot replace original root or partial-member evidence")
    parent, member = records
    if (
        parent.get("state") != "succeeded" or not parent.get("resource", {}).get("id")
        or parent.get("dimension") != "TF" or parent.get("managed_child")
        or member.get("attempt_id") != parent["attempt_id"] + "-mse-unresolved"
        or member.get("parent_attempt_id") != parent["attempt_id"]
        or member.get("managed_child") is not True or member.get("state") != "unknown"
        or member.get("resource", {}).get("id") is not None
        or member.get("resource", {}).get("resource_type") != "unresolved_managed_children"
    ):
        raise WorkflowError("only the original unresolved synthetic MSE member may be reconciled")
    for owner in connection.execute("SELECT * FROM owned WHERE task_id=? AND state!='deleted'", (task_id,)):
        evidence = json.loads(owner["evidence"])
        origin = evidence_generation(connection, root, task_id, evidence) or wire_evidence_generation(
            connection, root, task_id, owner["dimension"], json.loads(owner["resource"]), evidence,
        )
        if not origin or origin == generation:
            raise WorkflowError("MSE predecessor has outstanding or unbound ownership")
    return {member["attempt_id"]}
