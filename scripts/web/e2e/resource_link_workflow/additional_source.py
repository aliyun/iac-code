"""Explicit OPS-approved additional sample; never renew a consumed entitlement."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

from .files import WorkflowError, contained, digest, read_json
from .phases import known_original_rejected_oss_detail_read, known_original_resolved_read

LOADED_SHA = digest(Path(__file__))


def validate_additional_source(root: Path, packet: dict[str, Any], proof: dict[str, Any]) -> None:
    request = packet.get("additional_source_request")
    multiple = packet.get("additional_source_requests")
    if request is None and multiple is None:
        return
    if request is not None and multiple is not None:
        raise WorkflowError("use one explicit additional-operation review format")
    requests = multiple if multiple is not None else [request]
    plural = multiple is not None
    if (
        not isinstance(requests, list)
        or not requests
        or proof.get("kind") != ("ops_additional_sources_review" if plural else "ops_additional_source_review")
        or proof.get("approved") is not True
        or proof.get("additional_sources_approved" if plural else "additional_source_approved")
        != (requests if plural else request)
    ):
        raise WorkflowError("additional sample requires a distinct explicit OPS operation review")
    validate_paid_operation_review(root, packet, proof)
    dimensions, identifiers = set(), set()
    for item in requests:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("request_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,120}", item["request_id"])
            or item.get("dimension") not in {"ROS", "TF", "CREATE", "QUERY"}
            or (not plural and item.get("dimension") == "QUERY")
            or item["dimension"] in dimensions
            or item["request_id"] in identifiers
        ):
            raise WorkflowError("additional operations must have distinct reviewed dimensions and IDs")
        dimensions.add(item["dimension"])
        identifiers.add(item["request_id"])
    carry = proof.get("carry_forward_dimensions", {})
    if not isinstance(carry, dict) or set(carry) != set(packet["execution_grants"]) - dimensions:
        raise WorkflowError("additional sample extra dimensions require exact original-authority carry-forward")
    from .creation import carry_forward_authorities

    carry_forward_authorities(root, packet, proof)
    parameter_reviews = proof.get("query_parameter_reviews", {})
    if not isinstance(parameter_reviews, dict) or set(parameter_reviews) - {
        item["request_id"] for item in requests if item["dimension"] == "QUERY"
    }:
        raise WorkflowError("query parameter review must bind only its exact additional QUERY operation")
    for item in requests:
        _validate_original_operation(root, packet, item, proof)


def validate_paid_operation_review(root: Path, packet: dict[str, Any], proof: dict[str, Any]) -> None:
    """Paid follow-ups need real human permission and a cumulative OPS fee review."""
    from .creation import execution_digest

    budget = packet.get("budget_cents")
    if type(budget) is not int or budget < 0:
        raise WorkflowError("additional operation budget must be a nonnegative integer")
    review = proof.get("paid_operation_review")
    reuse = proof.get("budget_reservation_reuse")
    if budget == 0:
        if review is not None or reuse is not None:
            raise WorkflowError("paid operation must retain its truthful nonzero budget")
        return
    if not isinstance(review, dict):
        raise WorkflowError("paid additional operation requires human permission and cumulative fee evidence")
    human_path = contained(root, review.get("human_authorization", "missing"))
    cost_path = contained(root, review.get("cost_evidence", "missing"))
    if digest(human_path) != review.get("human_sha256") or digest(cost_path) != review.get("cost_sha256"):
        raise WorkflowError("paid operation human or cost evidence changed")
    human, cost = read_json(human_path), read_json(cost_path)
    task = packet["task_id"]
    requests = packet.get("additional_source_requests", [])
    if "additional_source_request" in packet:
        requests = [packet["additional_source_request"]]
    accounts = {binding["account"] for binding in packet["scenes"].values()}
    if (
        human.get("kind") != "human_fee_authorization" or human.get("approved") is not True
        or human.get("task_id") != task or human.get("plan_id") != task.rsplit(":", 1)[0]
        or not human.get("verbatim_user_message") or type(human.get("max_budget_cents")) is not int
        or human["max_budget_cents"] < budget
        or cost.get("kind") != "ops_cumulative_fee_review" or cost.get("approved") is not True
        or cost.get("task_id") != task or accounts != {cost.get("account")}
        or cost.get("execution_sha256") != execution_digest(packet)
        or cost.get("additional_operations") != requests
        or cost.get("human_authorization") != review["human_authorization"]
        or cost.get("human_sha256") != review["human_sha256"]
        or type(cost.get("budget_cents")) is not int or cost["budget_cents"] != budget
        or cost.get("includes_previous_operations") is not True
        or not cost.get("estimate_basis") or not cost.get("limitations")
        or not isinstance(cost.get("official_sources"), list) or not cost["official_sources"]
    ):
        raise WorkflowError("paid operation must bind human scope, exact operation and cumulative fee estimate")
    for source in cost["official_sources"]:
        if digest(contained(root, source["path"])) != source.get("sha256"):
            raise WorkflowError("paid operation official fee evidence changed")
    if reuse is None:
        return  # Ordinary positive incremental reservation remains additive.
    if reuse != {"task_id": task, "reserved_cents": budget,
                 "cost_evidence": review["cost_evidence"], "cost_sha256": review["cost_sha256"]}:
        raise WorkflowError("budget reuse must bind exact task, existing reserve and cumulative fee evidence")
    with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as state:
        row = state.execute(
            "SELECT b.amount,r.budget FROM budget_reservations b JOIN tasks t ON t.id=b.task_id "
            "JOIN runs r ON r.id=t.run_id WHERE b.task_id=?", (task,),
        ).fetchone()
    if not row or row[0] != budget or row[1] < budget:
        raise WorkflowError("budget reuse cannot invent, release or enlarge an existing reservation")


def dispatch_reservation_increment(root: Path, packet: dict[str, Any]) -> int:
    """Only an immutable OPS-reviewed cumulative envelope avoids double reserving."""
    from .creation import authorization_path, carry_forward_authorities, validate_authorization

    reviewed_keys = ("additional_source_request", "additional_source_requests", "execution_selection")
    path = authorization_path(root, packet)
    if not any(key in packet for key in reviewed_keys):
        if not path.is_file():
            return packet.get("budget_cents", 0)
        validate_authorization(root, packet["task_id"].rsplit(":", 1)[-1], packet)
        approval = read_json(path)
        proof = read_json(contained(root, approval.get("authorization_evidence", "missing")))
        if not any(source.get(key) is not None
                   for source in proof.get("carry_forward_dimensions", {}).values()
                   for key in ("identity_continuation", "read_continuation")):
            return packet.get("budget_cents", 0)
        authorities = carry_forward_authorities(root, packet, proof, review_admission=False)
        for dimension in proof["carry_forward_dimensions"]:
            if approval.get("dimension_authorities", {}).get(dimension) != authorities[dimension]:
                raise WorkflowError("continued budget reservation must retain original cumulative authority")
    validate_authorization(root, packet["task_id"].rsplit(":", 1)[-1], packet)
    approval = read_json(authorization_path(root, packet))
    proof = read_json(contained(root, approval["authorization_evidence"]))
    validate_paid_operation_review(root, packet, proof)
    return 0 if proof.get("budget_reservation_reuse") is not None else packet["budget_cents"]


def _validate_original_operation(
    root: Path, packet: dict[str, Any], request: dict[str, Any], proof: dict[str, Any],
) -> None:
    from .creation import (
        authorization_path,
        execution_authority,
        ros_snat_parent_binding,
        validate_authorization,
        validate_execution,
    )

    dimension = request.get("dimension")
    old_path = contained(root, request.get("predecessor_proposal", "missing"))
    remediation = contained(root, request.get("remediation_evidence", "missing"))
    if digest(old_path) != request.get("predecessor_sha256") or digest(remediation) != request.get(
        "remediation_sha256"
    ):
        raise WorkflowError("additional sample predecessor or remediation evidence changed")
    old = read_json(old_path)
    if proof.get("native_nas_corrections"):
        from .nas_correction import reviewed_correction

        reviewed_correction(root, old, packet, request, proof)
        return
    grants = packet["execution_grants"]
    accounts = {v["account"] for v in packet["scenes"].values()}
    parent = ros_snat_parent_binding(packet)
    parameter_review = request["request_id"] in proof.get("query_parameter_reviews", {})
    reviewed_custom = _reviewed_sae_custom_calls(root, old, packet, request, proof) if parameter_review else False
    from .temporary_samples import reviewed_tf_template_correction

    reviewed_correction = reviewed_tf_template_correction(root, old, packet, request, proof)
    if parent is not None:
        validate_execution(old, {"ROS"}, require_read_policy=True, readonly_dimensions={"ROS"})
        roots = [entry for entry in old["execution_grants"]["ROS"]
                 if entry["call"]["product"] == "ros" and entry["call"]["action"] == "GetStackResource"]
        if (
            len(roots) != 1 or roots[0]["call"]["params"].get("StackId") != parent["stack_id"]
            or roots[0]["call"]["params"].get("LogicalResourceId") != parent["logical_resource_id"]
            or set(old["scenes"]) != set(packet["scenes"])
            or any(value.get("identifier") != parent["identifier"]
                   or value.get("expected_type") != parent["resource_type"]
                   or value.get("region") != parent["region_id"] for value in old["scenes"].values())
        ):
            raise WorkflowError("ROS parent retest must retain the original exact ROS resource and scenes")
    if (
        dimension not in {"ROS", "TF", "CREATE", "QUERY"}
        or dimension not in grants
        or old["task_id"] != packet["task_id"]
        or (parent is None and not reviewed_custom and not reviewed_correction and not _same_query_calls(
            old["execution_grants"].get(dimension), grants[dimension], request
        ))
        or len(accounts) != 1
        or not all(isinstance(account, str) and account for account in accounts)
        or accounts != {v["account"] for v in old["scenes"].values()}
    ):
        raise WorkflowError("additional sample must retain exact original task, account and calls")
    # Extra dimensions get no new allowance: verify every original proposal, call,
    # authorization and account before retaining its original cumulative authority.
    scope = request.get("prior_scope", "")
    if not re.fullmatch(r"[a-f0-9]{32}:" + re.escape(dimension), scope):
        raise WorkflowError("additional sample prior scope is invalid")
    initial_query = dimension == "QUERY" and not authorization_path(root, old).exists()
    if initial_query:
        _validate_initial_query_approval(root, old, scope)
    else:
        validate_authorization(root, packet["task_id"].rsplit(":", 1)[-1], old)
    with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as state:
        job = state.execute("SELECT task_id,role,state,turn_id FROM jobs WHERE id=?", (scope.split(":")[0],)).fetchone()
        outstanding = state.execute(
            "SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'", (packet["task_id"],)
        ).fetchone()
        if not job or job[:3] != (packet["task_id"], "glm", "complete") or not job[3] or outstanding:
            raise WorkflowError("additional sample requires terminal predecessor and verified cleanup")
    authority = execution_authority(root, old, dimension)
    writes = [entry for entry in packet["execution_grants"][dimension] if entry["kind"] in {"create", "delete"}]
    if dimension != "QUERY" and parent is None and (
        sorted(entry["kind"] for entry in writes) != ["create", "delete"]
        or any(entry["quota"] != 1 for entry in writes)
    ):
        raise WorkflowError("additional sample permits exactly one original create and response-bound delete")
    if dimension == "QUERY" and (
        not grants[dimension]
        or any(entry["kind"] not in {"identity", "read"} for entry in grants[dimension])
        or not any(entry["kind"] == "identity" and entry["quota"] == 1 for entry in grants[dimension])
        or not any(entry["kind"] == "read" for entry in grants[dimension])
    ):
        raise WorkflowError("additional QUERY is only an explicitly reviewed original bounded read and identity")
    with closing(sqlite3.connect(f"file:{(root / 'execution-gate.sqlite3').as_posix()}?mode=ro", uri=True)) as gate:
        unknown = gate.cursor()
        unknown.row_factory = sqlite3.Row
        results = unknown.execute(
            "SELECT a.*,g.kind,g.call FROM attempts a LEFT JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.state='unknown'",
            (scope,),
        ).fetchall()
        # Reuse the narrow negative-read classifier, never rewrite a journal or
        # excuse unknown writes. LEFT JOIN keeps orphaned attempts fail-closed.
        if any(
            not known_original_resolved_read(gate, row) and not known_original_rejected_oss_detail_read(gate, row)
            for row in results
        ):
            raise WorkflowError("additional sample predecessor has an unknown wire result")
        for entry in writes:
            limit = gate.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?", (authority, entry["id"])
            ).fetchone()
            grant = gate.execute(
                "SELECT authority,quota,used FROM grants WHERE scope=? AND id=?", (scope, entry["id"])
            ).fetchone()
            attempts = gate.execute(
                "SELECT state FROM attempts WHERE scope=? AND grant_id=?", (scope, entry["id"])
            ).fetchall()
            if entry["kind"] == "delete" and request.get("external_cleanup") is not None:
                if limit != (0,) or grant != (authority, 1, 0) or attempts:
                    raise WorkflowError("external cleanup must preserve the original unused model delete")
                if dimension == "TF" and reviewed_correction:
                    from .mse_correction import validate_external_cleanup

                    validate_external_cleanup(root, old, request, authority, gate)
                else:
                    _validate_external_group_cleanup(root, packet, request, authority, gate)
            elif limit != (1,) or grant != (authority, 1, 1) or attempts != [("succeeded",)]:
                raise WorkflowError("additional sample needs exact successful consumed create/delete provenance")
        if request.get("external_cleanup") is not None and dimension != "ROS" and not (
            dimension == "TF" and reviewed_correction
        ):
            raise WorkflowError("external cleanup provenance is limited to the reviewed empty native ROS group")
        if dimension == "QUERY" or parent is not None:
            if initial_query or parent is not None or parameter_review:
                identities = gate.execute("SELECT DISTINCT account FROM identities WHERE scope=?", (scope,)).fetchall()
                if identities != [(next(iter(accounts)),)]:
                    raise WorkflowError("initial QUERY needs original signed same-account identity provenance")
            # The predecessor may include a separately reviewed identity
            # continuation. Verify its real quota, not the successor's one read.
            for entry in old["execution_grants"][dimension]:
                if initial_query or parent is not None or parameter_review:
                    binding = gate.execute(
                        "SELECT kind,call FROM grants WHERE scope=? AND id=?", (scope, entry["id"]),
                    ).fetchone()
                    if binding != (entry["kind"], canonical(entry["call"])):
                        raise WorkflowError("initial QUERY original wire calls differ from approved packet")
                grant = gate.execute(
                    "SELECT authority,quota,used FROM grants WHERE scope=? AND id=?", (scope, entry["id"])
                ).fetchone()
                attempts = gate.execute(
                    "SELECT state FROM attempts WHERE scope=? AND grant_id=?", (scope, entry["id"])
                ).fetchall()
                if (
                    not grant
                    or grant[0] != authority
                    or grant[1] != entry["quota"]
                    or not 1 <= grant[2] <= grant[1]
                    or len(attempts) != grant[2]
                    or any(row != ("succeeded",) for row in attempts)
                ):
                    raise WorkflowError("additional QUERY requires original successful bounded read provenance")
                if parameter_review:
                    limit = gate.execute(
                        "SELECT used FROM authority_limits WHERE authority=? AND id=?", (authority, entry["id"]),
                    ).fetchone()
                    if grant[1:] != (1, 1) or limit != (1,) or attempts != [("succeeded",)]:
                        raise WorkflowError("reviewed SAE read requires exact exhausted original one-call authority")
                    if entry["kind"] == "identity":
                        response = json.loads(gate.execute(
                            "SELECT response FROM attempts WHERE scope=? AND grant_id=?", (scope, entry["id"]),
                        ).fetchone()[0])
                        if not isinstance(response, dict) or response.get("AccountId") != next(iter(accounts)):
                            raise WorkflowError("reviewed SAE read requires the original signed same-account identity")
                if parent is not None:
                    limit = gate.execute(
                        "SELECT used FROM authority_limits WHERE authority=? AND id=?", (authority, entry["id"])
                    ).fetchone()
                    if grant[2] != grant[1] or limit != (grant[1],):
                        raise WorkflowError("ROS parent retest requires exhausted unchanged predecessor reads")
                    responses = [json.loads(row[0]) for row in gate.execute(
                        "SELECT response FROM attempts WHERE scope=? AND grant_id=?", (scope, entry["id"])
                    )]
                    if entry["kind"] == "identity" and any(
                        value.get("AccountId") != next(iter(accounts)) for value in responses
                    ):
                        raise WorkflowError("ROS predecessor signed identity differs")
                    if entry["call"]["action"] == "GetStackResource":
                        if not all(
                            (value.get("Resource") or value).get("PhysicalResourceId") == parent["identifier"]
                            and (value.get("Resource") or value).get("StackId") == parent["stack_id"]
                            and (value.get("Resource") or value).get("LogicalResourceId")
                            == parent["logical_resource_id"]
                            and (value.get("Resource") or value).get("ResourceType") == parent["resource_type"]
                            and (value.get("Resource") or value).get("Status") == "CREATE_COMPLETE"
                            and value.get("RequestId") for value in responses
                        ):
                            raise WorkflowError("ROS predecessor raw resource does not prove the exact live identity")
            if request.get("query_identity_quota_reduction") is not None:
                for entry in old["execution_grants"][dimension]:
                    binding = gate.execute(
                        "SELECT kind,call FROM grants WHERE scope=? AND id=?", (scope, entry["id"]),
                    ).fetchone()
                    if binding != (entry["kind"], canonical(entry["call"])):
                        raise WorkflowError("QUERY identity reduction original read calls differ")
                _validate_query_identity_history(root, old, request, authority, gate)
    # A changed request document is not a new allowance for the same operation ID.
    for path in (root / "creation-authorizations").glob("*.json"):
        approval = read_json(path)
        previous_requests = approval.get("additional_source_requests", [])
        if "additional_source_request" in approval:
            previous_requests = [*previous_requests, approval["additional_source_request"]]
        for previous in previous_requests:
            if (parent is not None and approval.get("task_id") == packet["task_id"]
                    and previous.get("ros_snat_parent_binding") == parent and previous != request):
                raise WorkflowError("ROS parent operation already reviewed; do not rename to copy its allowance")
            if previous.get("request_id") == request["request_id"] and previous != request:
                raise WorkflowError("additional sample operation ID already bound to different evidence")


def _validate_initial_query_approval(root: Path, old: dict[str, Any], scope: str) -> None:
    """Prove the real initial Sol approval, never invent retrospective creation permission."""
    from .creation import requires_execution_review, validate_execution

    if (
        requires_execution_review(old) or old.get("budget_cents") != 0
        or set(old.get("requests", {})) != {"QUERY"}
        or set(old.get("execution_grants", {})) != {"QUERY"}
        or old.get("profile") != "resource-link-test"
    ):
        raise WorkflowError("missing authorization is only valid for an original initial read-only QUERY")
    validate_execution(old, {"QUERY"})
    try:
        with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as db:
            job = db.execute("SELECT task_id,role,state,turn_id,created FROM jobs WHERE id=?",
                             (scope.split(":")[0],)).fetchone()
            if not job or job[:3] != (old["task_id"], "glm", "complete") or not job[3]:
                raise WorkflowError("initial QUERY requires its exact terminal original GLM")
            approvals = db.execute(
                "SELECT data,created FROM events WHERE kind='packet_approved' AND created<=? ORDER BY id DESC",
                (job[4],),
            ).fetchall()
            recorded = next(((json.loads(data), at) for data, at in approvals
                             if json.loads(data).get("task_id") == old["task_id"]), None)
            if not recorded:
                raise WorkflowError("initial QUERY has no original program packet approval")
            event, approved_at = recorded
            for candidate in (root / "packets").glob("*/packet.json"):
                if digest(candidate) != event.get("packet_sha") or read_json(candidate) != old:
                    continue
                prepare = db.execute("SELECT task_id,role,purpose,state,turn_id,created FROM jobs WHERE id=?",
                                     (candidate.parent.name,)).fetchone()
                if (
                    prepare and prepare[:4] in {
                        (old["task_id"], "sol", "prepare", "complete"),
                        (old["task_id"], "sol", "repair", "complete"),
                    } and prepare[4] and prepare[5] <= approved_at <= job[4]
                ):
                    return
    except (sqlite3.Error, ValueError, TypeError, AttributeError) as error:
        raise WorkflowError("initial QUERY approval provenance is missing or invalid") from error
    raise WorkflowError("initial QUERY must match the unchanged last approved packet and terminal Sol")


def _reviewed_sae_custom_calls(
    root: Path, old: dict[str, Any], packet: dict[str, Any], request: dict[str, Any], proof: dict[str, Any],
) -> bool:
    """One independently reviewed custom-namespace read, never arbitrary parameter drift.

    The original terminal job, signed calls and consumed authority are still
    validated below. This adds no writes and never updates the old journal.
    """
    review = proof["query_parameter_reviews"][request["request_id"]]
    if isinstance(review, dict) and review.get("kind") == "ops_sae_existing_application_sample_review":
        return _reviewed_sae_application_calls(root, old, packet, request, review)
    original = old.get("execution_grants", {}).get("QUERY")
    if (
        request.get("dimension") != "QUERY" or request.get("query_identity_quota_reduction") is not None
        or not isinstance(review, dict) or review.get("kind") != "ops_sae_include_custom_query_review"
        or review.get("approved") is not True or not isinstance(original, list) or len(original) != 2
    ):
        raise WorkflowError("SAE parameter change requires its explicit independent OPS review")
    identities = [entry for entry in original if entry.get("kind") == "identity"]
    reads = [entry for entry in original if entry.get("kind") == "read"]
    if len(identities) != 1 or len(reads) != 1:
        raise WorkflowError("reviewed SAE operation is only one original identity and one metadata read")
    identity, read = identities[0], reads[0]
    call = read.get("call", {})
    region = call.get("region_id")
    params = {"RegionId": region}
    approved = {**params, "ContainCustom": True}
    if (
        not isinstance(region, str) or not region.startswith("cn-")
        or any(type(entry.get("quota")) is not int or entry["quota"] != 1 for entry in original)
        or identity.get("call", {}).get("product") != "sts"
        or identity["call"].get("action") != "GetCallerIdentity" or identity["call"].get("params") != {}
        or identity["call"].get("region_id") != region
        or call.get("product") != "sae" or call.get("action") != "DescribeNamespaceList"
        or call.get("version") != "2019-05-06" or call.get("params") != params
        or read.get("read_policy") != {"phase": "execute"}
        or review.get("grant_id") != read.get("id") or review.get("original_params") != params
        or review.get("approved_params") != approved
        or review.get("approved_params", {}).get("ContainCustom") is not True
        or not packet.get("scenes") or any(
            scene.get("expected_type") != "ALIYUN::SAE::Namespace" or scene.get("region") != region
            for scene in packet["scenes"].values()
        )
    ):
        raise WorkflowError("reviewed SAE change may only include custom namespaces on the original regional read")
    expected = json.loads(json.dumps(original))
    next(entry for entry in expected if entry["kind"] == "read")["call"]["params"] = approved
    successor = packet.get("execution_grants", {}).get("QUERY")
    if successor != expected or next(
        (entry.get("call", {}).get("params", {}).get("ContainCustom") for entry in successor or []
         if entry.get("kind") == "read"), None
    ) is not True:
        raise WorkflowError("reviewed SAE change cannot alter another call, quota, policy or parameter")
    official = contained(root, review.get("official_evidence", "missing"))
    if digest(official) != review.get("official_sha256") or read_json(official).get(
        "kind"
    ) != "ops_official_custom_identity_review":
        raise WorkflowError("reviewed SAE change requires unchanged official-source evidence")
    return True


def _reviewed_sae_application_calls(
    root: Path, old: dict[str, Any], packet: dict[str, Any], request: dict[str, Any], review: dict[str, Any],
) -> bool:
    """One reviewed existing application, not arbitrary QUERY parameter drift.

    Outer validation still proves terminal original calls, consumed authority,
    original Sol approval and no outstanding owners. This only permits the
    exact FieldValue replacement joined to OPS's original bounded diagnosis.
    """
    original = old.get("execution_grants", {}).get("QUERY", [])
    reads = [entry for entry in original if entry.get("kind") == "read"]
    identities = [entry for entry in original if entry.get("kind") == "identity"]
    selection = request.get("query_sample_reselection")
    if (
        request.get("dimension") != "QUERY" or request.get("query_identity_quota_reduction") is not None
        or review.get("approved") is not True or not isinstance(selection, dict)
        or review.get("sample_selection_approved") != selection
        or len(original) != 2 or len(reads) != 1 or len(identities) != 1
        or any(type(entry.get("quota")) is not int or entry["quota"] != 1 for entry in original)
        or packet.get("budget_cents") != 0 or packet.get("profile") != "resource-link-test"
    ):
        raise WorkflowError("SAE existing sample requires its exact independent one-read OPS review")
    read, identity = reads[0], identities[0]
    call, identity_call = read["call"], identity["call"]
    region = call.get("region_id")
    previous, selected = selection.get("original_value"), selection.get("proposed_value")
    uuid = r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}"
    before = {"FieldType": "appIds", "FieldValue": previous, "CurrentPage": 1, "PageSize": 10}
    after = {**before, "FieldValue": selected}
    if (
        not isinstance(region, str) or not region.startswith("cn-")
        or not isinstance(previous, str) or not re.fullmatch(uuid, previous)
        or not isinstance(selected, str) or not re.fullmatch(uuid, selected) or selected == previous
        or canonical(identity_call) != canonical({
            "product": "sts", "action": "GetCallerIdentity", "version": "2015-04-01",
            "region_id": region, "params": {},
        })
        or canonical(call) != canonical({
            "product": "sae", "action": "ListApplications", "version": "2019-05-06",
            "region_id": region, "params": before,
        })
        or read.get("read_policy") != {"phase": "execute"}
        or selection.get("grant_id") != read["id"] or selection.get("changed_param") != "FieldValue"
        or selection.get("same_region") != region
        or selection.get("all_other_call_fields_and_quotas_unchanged") is not True
        or review.get("grant_id") != read["id"] or canonical(review.get("original_params")) != canonical(before)
        or canonical(review.get("approved_params")) != canonical(after)
    ):
        raise WorkflowError("SAE sample review may only replace one original exact application ID")
    expected = json.loads(json.dumps(original))
    next(entry for entry in expected if entry["kind"] == "read")["call"]["params"] = after
    if canonical(packet.get("execution_grants", {}).get("QUERY")) != canonical(expected):
        raise WorkflowError("SAE sample review cannot change another call, parameter, policy or quota")

    def evidence(path: str, sha: str) -> dict[str, Any]:
        file = contained(root, path)
        if digest(file) != sha:
            raise WorkflowError("SAE reviewed original sample evidence changed")
        value = read_json(file)
        if not isinstance(value, dict):
            raise WorkflowError("SAE sample evidence must retain its original object")
        return value

    diagnosis = evidence(review.get("diagnostic_evidence", "missing"), review.get("diagnostic_sha256", ""))
    if diagnosis.get("kind") != "ops_sae_existing_application_group_diagnosis" or diagnosis.get(
        "task_id"
    ) != packet["task_id"]:
        raise WorkflowError("SAE sample review needs its own original OPS diagnosis")
    events = diagnosis.get("events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise WorkflowError("SAE sample diagnosis requires its original call records")
    list_call = {**call, "params": {"CurrentPage": 1, "PageSize": 10}}
    group_call = {**call, "action": "DescribeApplicationGroups",
                  "params": {"AppId": selected, "CurrentPage": 1, "PageSize": 10}}
    bodies, selected_events = [], []
    for expected_call in (identity_call, list_call, group_call):
        matches = [event for event in events if canonical(event.get("call")) == canonical(expected_call)]
        if len(matches) != 1:
            raise WorkflowError("SAE sample needs unique original identity/list/own-group calls")
        event = matches[0]
        body = evidence(event.get("path", "missing"), event.get("sha256", ""))
        if (event.get("error") is not False or not body.get("RequestId")
                or event.get("request_id") != body["RequestId"]
                or type(event.get("started_at")) not in {int, float}
                or type(event.get("completed_at")) not in {int, float}
                or event["completed_at"] < event["started_at"]):
            raise WorkflowError("SAE sample requires actual successful ordered original responses")
        bodies.append(body)
        selected_events.append(event)
    signed, listed, grouped = bodies
    account = signed.get("AccountId")
    if (not isinstance(account, str) or not account or selection.get("same_account") != account
            or any(selected_events[i]["completed_at"] > selected_events[i + 1]["started_at"] for i in range(2))
            or selection.get("source_evidence") != selected_events[1]["path"]
            or selection.get("source_sha256") != selected_events[1]["sha256"]
            or selection.get("group_evidence") != selected_events[2]["path"]
            or selection.get("group_sha256") != selected_events[2]["sha256"]):
        raise WorkflowError("SAE selected row/group must bind the same original signed account and requests")
    data = listed.get("Data")
    if not isinstance(data, dict):
        raise WorkflowError("SAE sample requires its original application-list object")
    rows = data.get("Applications", [])
    pointer = selection.get("source_pointer", "")
    match = re.fullmatch(r"/Data/Applications/(0|[1-9][0-9]*)", pointer) if isinstance(pointer, str) else None
    groups = grouped.get("Data")
    if (not match or not isinstance(rows, list) or not 0 <= int(match.group(1)) < len(rows) <= 10
            or any(value.get("Success") is not True or type(value.get("Code")) is not int or value["Code"] != 200
                   for value in (listed, grouped))
            or any(type(value.get(key)) is not int or value[key] != expected
                   for value in (listed, data) for key, expected in (("CurrentPage", 1), ("PageSize", 10)))
            or not isinstance(groups, list) or not 1 <= len(groups) <= 10):
        raise WorkflowError("SAE selected sample must come from the bounded successful original first page")
    row = rows[int(match.group(1))]
    defaults = [group for group in groups if isinstance(group, dict) and group.get("GroupName") == "_DEFAULT_GROUP"
                and type(group.get("GroupType")) is int and group["GroupType"] == 0
                and isinstance(group.get("GroupId"), str) and re.fullmatch(uuid, group["GroupId"])]
    if (not isinstance(row, dict) or row.get("AppId") != selected or row.get("RegionId") != region
            or len([item for item in rows if isinstance(item, dict) and item.get("AppId") == selected]) != 1
            or not row.get("AppName") or not row.get("AppDescription") or len(defaults) != 1
            or review.get("default_group_id") != defaults[0]["GroupId"]):
        raise WorkflowError("SAE sample must have its own unique application row and nonempty default group")
    scenes, old_scenes = packet.get("scenes", {}), old.get("scenes", {})
    if (not scenes or set(scenes) != set(old_scenes) or any(
        scene.get("account") != account or scene.get("region") != region
        or scene.get("identifier") != selected or scene.get("expected_type") != "ALIYUN::SAE::Application"
        or scene.get("source_evidence") != selection["source_evidence"]
        or scene.get("source_sha256") != selection["source_sha256"] or scene.get("source_pointer") != pointer
        or scene.get("render_text") != [row["AppName"], row["AppDescription"]]
        or old_scenes[key].get("identifier") != previous or old_scenes[key].get("account") != account
        or old_scenes[key].get("region") != region or old_scenes[key].get("expected_type") != "ALIYUN::SAE::Application"
        for key, scene in scenes.items()
    )):
        raise WorkflowError("SAE replacement must preserve exact original scenes and its selected own metadata")
    official = evidence(review.get("official_evidence", "missing"), review.get("official_sha256", ""))
    if official.get("kind") != "ops_official_sae_existing_application_review" or not official.get("sources"):
        raise WorkflowError("SAE sample requires the independently reviewed official original API sources")
    for source in official["sources"]:
        evidence(source["path"], source["sha256"])
    return True


def _same_query_calls(original: Any, successor: list[dict[str, Any]], request: dict[str, Any]) -> bool:
    """An explicit new QUERY may reduce a continued identity quota to one.

    No predecessor, authority, write/read allowance or journal is changed.
    Without the explicit reviewed reduction, retain exact legacy comparison.
    """
    reduction = request.get("query_identity_quota_reduction")
    if reduction is None:
        return original == successor
    if request.get("dimension") != "QUERY" or not isinstance(original, list):
        return False
    identities = [entry for entry in original if entry.get("kind") == "identity"]
    if len(identities) != 1:
        return False
    identity = identities[0]
    quota = identity.get("quota")
    if (
        type(quota) is not int or not 2 <= quota <= 100
        or reduction != {"grant_id": identity["id"], "previous_quota": quota, "new_quota": 1}
        or type(reduction.get("previous_quota")) is not int
        or type(reduction.get("new_quota")) is not int
        or identity["call"].get("product", "").lower() != "sts"
        or identity["call"].get("action") != "GetCallerIdentity"
        or identity["call"].get("params") != {}
    ):
        return False
    expected = json.loads(json.dumps(original))
    for entry in expected:
        if entry["id"] == identity["id"]:
            entry["quota"] = 1
    return expected == successor


def _validate_query_identity_history(
    root: Path, old: dict[str, Any], request: dict[str, Any], authority: str, gate: sqlite3.Connection,
) -> None:
    """Require exhausted, successful, terminal same-account original reads."""
    identity = next(entry for entry in old["execution_grants"]["QUERY"] if entry["kind"] == "identity")
    limit = gate.execute(
        "SELECT used FROM authority_limits WHERE authority=? AND id=?", (authority, identity["id"]),
    ).fetchone()
    grants = gate.execute(
        "SELECT scope,quota,used,kind,call FROM grants WHERE authority=? AND id=?", (authority, identity["id"]),
    ).fetchall()
    if (
        limit != (identity["quota"],) or not grants
        or sum(entry[2] for entry in grants) != identity["quota"]
        or request["prior_scope"] not in {entry[0] for entry in grants}
    ):
        raise WorkflowError("QUERY identity reduction requires exhausted original cumulative provenance")
    if gate.execute(
        "SELECT 1 FROM attempts WHERE state!='succeeded' AND scope IN "
        "(SELECT scope FROM grants WHERE authority=?)", (authority,),
    ).fetchone():
        raise WorkflowError("QUERY identity reduction refuses unknown original wire results")
    account = next(iter(old["scenes"].values()))["account"]
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as state:
        for scope, quota, used, kind, call in grants:
            job = state.execute(
                "SELECT task_id,role,state,turn_id FROM jobs WHERE id=?", (scope.split(":")[0],),
            ).fetchone()
            results = gate.execute(
                "SELECT state,response FROM attempts WHERE scope=? AND grant_id=?", (scope, identity["id"]),
            ).fetchall()
            if (
                not re.fullmatch(r"[a-f0-9]{32}:QUERY", scope)
                or not job or job[:3] != (old["task_id"], "glm", "complete") or not job[3]
                or kind != "identity" or call != canonical(identity["call"])
                or type(quota) is not int or type(used) is not int
                or not 0 <= used <= quota <= identity["quota"] or len(results) != used
            ):
                raise WorkflowError("QUERY identity reduction original terminal scope or calls differ")
            for result, response in results:
                try:
                    body = json.loads(response)
                except (ValueError, TypeError):
                    body = None
                if result != "succeeded" or not isinstance(body, dict) or body.get("AccountId") != account:
                    raise WorkflowError("QUERY identity reduction needs successful original signed account reads")


def _bound_document(root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    path = contained(root, binding.get("path", "missing"))
    if digest(path) != binding.get("sha256"):
        raise WorkflowError("external cleanup immutable evidence changed")
    return read_json(path)


def reviewed_external_cleanup(root: Path, task: str, scope: str, reviewing_job_id: str) -> bool:
    """Revalidate an approved historical OPS cleanup, not a missing model read.

    Only the original empty native ROS group exception is supported. The same
    signed creation, unused model deletion, SDK chain and native owner proofs
    are checked again at acceptance; current/other scopes keep their read gate.
    """
    from .creation import execution_authority, validate_authorization

    if not re.fullmatch(r"[a-f0-9]{32}:(?:ROS|TF)", scope):
        return False
    dimension = scope.rsplit(":", 1)[-1]
    for path in (root / "creation-authorizations").glob("*.json"):
        approval = read_json(path)
        if approval.get("task_id") != task:
            continue
        requests = approval.get("additional_source_requests", [])
        if "additional_source_request" in approval:
            requests = [approval["additional_source_request"]]
        for request in requests:
            if request.get("prior_scope") != scope or request.get("external_cleanup") is None:
                continue
            review = _bound_document(root, {"path": approval.get("authorization_evidence"),
                                           "sha256": approval.get("authorization_sha256")})
            plural = "additional_source_requests" in approval
            if (approval.get("authorized") is not True or approval.get("identity_mode") != "existing_account"
                    or review.get("approved") is not True
                    or review.get("kind") != ("ops_additional_sources_review" if plural
                                               else "ops_additional_source_review")
                    or review.get("additional_sources_approved" if plural else "additional_source_approved")
                    != (requests if plural else request) or request.get("dimension") != dimension):
                raise WorkflowError("historical external cleanup requires the original exact OPS approval")
            packet = _bound_document(root, {"path": request.get("predecessor_proposal"),
                                           "sha256": request.get("predecessor_sha256")})
            if packet.get("task_id") != task:
                raise WorkflowError("historical external cleanup predecessor belongs to another task")
            validate_authorization(root, task.rsplit(":", 1)[-1], packet)
            authority = execution_authority(root, packet, dimension)
            with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as state:
                if not state.execute(
                    "SELECT 1 FROM jobs WHERE id=? AND task_id=? AND role='glm' AND state='complete' "
                    "AND turn_id IS NOT NULL", (scope.split(":")[0], task),
                ).fetchone():
                    raise WorkflowError("historical external cleanup requires the original terminal GLM")
            with closing(sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True)) as gate:
                cursor = gate.cursor()
                cursor.row_factory = sqlite3.Row
                unknown = cursor.execute(
                    "SELECT a.*,g.kind,g.call FROM attempts a LEFT JOIN grants g "
                    "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.state='unknown'", (scope,),
                ).fetchall()
                if any(not known_original_resolved_read(gate, row) for row in unknown):
                    raise WorkflowError("historical external cleanup has an unresolved original wire result")
                for entry in packet["execution_grants"][dimension]:
                    if entry["kind"] not in {"create", "delete"}:
                        continue
                    used = 1 if entry["kind"] == "create" else 0
                    if (entry["quota"] != 1 or gate.execute(
                            "SELECT used FROM authority_limits WHERE authority=? AND id=?",
                            (authority, entry["id"])).fetchone() != (used,)
                            or gate.execute("SELECT authority,quota,used FROM grants WHERE scope=? AND id=?",
                                            (scope, entry["id"])).fetchone() != (authority, 1, used)
                            or gate.execute("SELECT state FROM attempts WHERE scope=? AND grant_id=?",
                                            (scope, entry["id"])).fetchall() != [("succeeded",)] * used):
                        raise WorkflowError("historical OPS cleanup cannot replace a changed model write journal")
                if dimension == "TF":
                    from .mse_correction import validate_external_cleanup

                    validate_external_cleanup(root, packet, request, authority, gate,
                                              reviewing_job_id=reviewing_job_id)
                else:
                    _validate_external_group_cleanup(root, packet, request, authority, gate,
                                                     reviewing_job_id=reviewing_job_id)
            return True
    return False


def _validate_external_group_cleanup(root: Path, packet: dict[str, Any], request: dict[str, Any],
                                     authority: str, gate: sqlite3.Connection, *,
                                     reviewing_job_id: str | None = None) -> None:
    """Read actual OPS deletion; never synthesize a model attempt, absence or quota."""
    from .pending_delete import validate as validate_pending

    proof = _bound_document(root, request["external_cleanup"])
    task, scope = packet["task_id"], request["prior_scope"]
    if (proof.get("kind") != "ops_external_ros_group_cleanup_provenance" or proof.get("approved") is not True
            or proof.get("task_id") != task or proof.get("scope") != scope or proof.get("authority") != authority
            or proof.get("pass_added") is not False or proof.get("model_wire_unchanged") is not True):
        raise WorkflowError("external cleanup requires an exact independent OPS provenance review")
    entries = packet["execution_grants"]["ROS"]
    create = next(e for e in entries if e["kind"] == "create")
    delete = next(e for e in entries if e["kind"] == "delete")
    call = create.get("call", {})
    template = json.loads(call.get("params", {}).get("TemplateBody", "{}"))
    resources = template.get("Resources", {})
    if (call.get("product") != "ros" or call.get("action") != "CreateStack"
            or call.get("version") != "2019-09-10" or template.get("Transform") is not None
            or len(resources) != 1 or next(iter(resources.values())).get("Type")
            != "ALIYUN::ResourceManager::ResourceGroup" or delete.get("parent") != create["id"]
            or delete.get("target_param") != "StackId"):
        raise WorkflowError("external cleanup requires a single response-bound native ROS ResourceGroup")
    wire = _bound_document(root, proof["original_creation"])
    row = gate.execute("SELECT scope,grant_id,state,session,response,created,credential FROM attempts WHERE id=?",
                       (wire.get("attempt_id"),)).fetchone()
    resource = wire.get("resource", {})
    account, region, stack_id = resource.get("account"), resource.get("region"), resource.get("id")
    if (not row or row[:4] != (scope, create["id"], "succeeded", wire.get("session_id"))
            or json.loads(row[4]) != wire.get("response") or wire.get("job_id") != scope.split(":")[0]
            or wire.get("dimension") != "ROS" or wire.get("state") != "succeeded"
            or not isinstance(stack_id, str) or not stack_id or not wire["response"].get("RequestId")
            or wire["response"].get("StackId") != stack_id or region != call.get("region_id")
            or {s["account"] for s in packet["scenes"].values()} != {account}
            or gate.execute("SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
                            (scope, row[3], row[6])).fetchone() != (account,)):
        raise WorkflowError("external cleanup original signed creation/session/generation mismatch")
    result = _bound_document(root, proof["delete_result"])
    review = _bound_document(root, {"path": result.get("review_evidence"), "sha256": result.get("review_sha256")})
    exact_delete = delete.get("call", {}) | {"params": delete.get("call", {}).get("params", {}) | {"StackId": stack_id}}
    if (review.get("kind") != "ops_original_owned_stack_cleanup_review" or review.get("approved") is not True
            or review.get("task_id") != task or review.get("scope") != scope or review.get("account") != account
            or review.get("region") != region or review.get("original_create_attempt") != wire["attempt_id"]
            or review.get("exact_delete_call") != exact_delete or review.get("max_delete_calls") != 1
            or review.get("automatic_retries") != 0 or review.get("cloud_create_calls") != 0
            or result.get("task_id") != task or result.get("delete_submitted") is not True
            or result.get("original_model_wire_unchanged") is not True or result.get("pass_added") is not False):
        raise WorkflowError("external cleanup original bounded deletion review mismatch")
    calls = result.get("calls", [])
    if [(c.get("product"), c.get("action")) for c in calls] != [
        ("sts", "GetCallerIdentity"), ("ros", "GetStack"), ("ros", "ListStackResources"),
        ("resourcemanager", "GetResourceGroup"), ("resourcemanager", "ListResources"), ("ros", "DeleteStack"),
    ]:
        raise WorkflowError("external cleanup needs the original identity/stack/sole-child/empty/delete chain")
    raws = []
    end = row[5]
    for c in calls:
        raw = _bound_document(root, {"path": c.get("source"), "sha256": c.get("source_sha256")})
        intent = _bound_document(root, {"path": c.get("intent"), "sha256": c.get("intent_sha256")})
        expected_intent = {k: c[k] for k in ("product", "action", "version", "endpoint", "params", "started",
                                            "profile", "max_attempts")}
        body = raw.get("body", {})
        if (intent != expected_intent or c["profile"] != "resource-link-test" or c["max_attempts"] != 1
                or type(raw.get("statusCode")) is not int or raw["statusCode"] != 200
                or not body.get("RequestId") or raw.get("headers", {}).get("x-acs-request-id") != body["RequestId"]
                or c["endpoint"] != c["product"] + ".aliyuncs.com"
                or c["version"] != {"sts": "2015-04-01", "ros": "2019-09-10",
                                    "resourcemanager": "2020-03-31"}[c["product"]]
                or type(c["started"]) not in (int, float) or c["started"] <= end
                or type(raw.get("finished")) not in (int, float) or raw["finished"] < c["started"]):
            raise WorkflowError("external cleanup original SDK response/intent/time mismatch")
        end = raw["finished"]
        raws.append(body)
    group = raws[3].get("ResourceGroup", {})
    group_id = group.get("Id")
    logical_id, spec = next(iter(resources.items()))
    child = raws[2].get("Resources", [])
    if (raws[0].get("AccountId") != account or raws[1].get("StackId") != stack_id
            or raws[1].get("StackName") != call["params"].get("StackName") or raws[1].get("RegionId") != region
            or calls[1]["params"] != {"RegionId": region, "StackId": stack_id}
            or calls[2]["params"] != calls[1]["params"] or len(child) != 1
            or child[0].get("StackId") != stack_id or child[0].get("LogicalResourceId") != logical_id
            or child[0].get("ResourceType") != spec["Type"] or child[0].get("PhysicalResourceId") != group_id
            or not group_id or group.get("AccountId") != account or group.get("Status") != "OK"
            or group.get("Name") != spec["Properties"].get("Name")
            or group.get("DisplayName") != spec["Properties"].get("DisplayName")
            or calls[3]["params"] != {"ResourceGroupId": group_id}
            or calls[4]["params"] != {"ResourceGroupId": group_id, "PageNumber": 1, "PageSize": 10}
            or raws[4].get("TotalCount") != 0 or raws[4].get("Resources") != {"Resource": []}
            or calls[5]["params"] != exact_delete["params"]
            or result.get("delete_success_request_id") != raws[5]["RequestId"]):
        raise WorkflowError("external cleanup must bind the original sole empty group and exact stack delete")
    absence = _bound_document(root, proof["stack_absence"])
    pending = _bound_document(root, proof["group_pending_delete"])
    post = _bound_document(root, {"path": absence.get("source_evidence"), "sha256": absence.get("source_sha256")})
    if (absence.get("task_id") != task or absence.get("resource") != resource or absence.get("absent") is not True
            or absence.get("failed") is not False or absence.get("original_creation") != proof["original_creation"]
            or post.get("task_id") != task or post.get("original_scope") != scope or post.get("account") != account
            or post.get("cloud_writes") != 0 or post.get("profile") != "resource-link-test"
            or post.get("original_delete_evidence") != proof["delete_result"]["path"]
            or post.get("original_delete_sha256") != proof["delete_result"]["sha256"]
            or pending.get("source_evidence") != absence["source_evidence"]
            or pending.get("source_sha256") != absence["source_sha256"]):
        raise WorkflowError("external cleanup must bind actual post-delete absence and honest group disposition")
    post_calls = post.get("calls", [])
    signed = [c for c in post_calls if c.get("action") == "GetCallerIdentity" and c.get("product") == "sts"
              and c.get("status_code") == 200 and c.get("response", {}).get("AccountId") == account]
    terminal = [c for c in post_calls if c.get("product") == "ros" and c.get("action") == "GetStack"
                and c.get("params") == {"RegionId": region, "StackId": stack_id} and c.get("status_code") == 200
                and c.get("response", {}).get("StackId") == stack_id
                and c.get("response", {}).get("Status") == "DELETE_COMPLETE" and c.get("response", {}).get("RequestId")]
    if len(signed) != 1 or len(terminal) != 1 or terminal[0].get("started", 0) <= end:
        raise WorkflowError("external cleanup needs actual same-account delete-terminal read after deletion")
    with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as state:
        if reviewing_job_id is not None and not state.execute(
            "SELECT 1 FROM jobs WHERE id=? AND task_id=? AND role='sol' AND purpose='review' "
            "AND state='running' AND turn_id IS NOT NULL", (reviewing_job_id, task),
        ).fetchone():
            raise WorkflowError("historical external cleanup requires the exact active Sol review")
        terminal_sql = "SELECT 1 FROM jobs WHERE task_id=? AND state!='complete'"
        terminal_args = (task,)
        if reviewing_job_id is not None:
            terminal_sql += " AND id!=?"
            terminal_args += (reviewing_job_id,)
        if state.execute(terminal_sql, terminal_args).fetchone():
            raise WorkflowError("external cleanup requires this task's model turns to be terminal")
        reviewed_owners = review.get("original_owned", [])
        if {o.get("id") for o in reviewed_owners} != {absence.get("ownership_id"), pending.get("ownership_id")}:
            raise WorkflowError("external cleanup review must bind both original owned objects")
        for binding, item in ((proof["stack_absence"], absence), (proof["group_pending_delete"], pending)):
            owner = state.execute("SELECT task_id,dimension,state,resource,evidence FROM owned WHERE id=?",
                                  (item.get("ownership_id"),)).fetchone()
            if (not owner or owner[:3] != (task, "ROS", "deleted") or json.loads(owner[3]) != item.get("resource")
                    or json.loads(owner[4]) != binding):
                raise WorkflowError("external cleanup needs native exact deleted owner/proof bindings")
        if pending["resource"].get("stack_id") != stack_id or pending["resource"].get("id") != group_id:
            raise WorkflowError("external cleanup child is not the original stack's response-bound group")
        validate_pending(root, task, pending["ownership_id"], pending["resource"], pending)
        events = state.execute("SELECT data FROM events WHERE kind='ops_user_accepted_pending_delete'").fetchall()
        if not any(json.loads(e[0]).get("task_id") == task
                   and json.loads(e[0]).get("ownership_id") == pending["ownership_id"]
                   and json.loads(e[0]).get("actual_absence_verified") is False
                   and json.loads(e[0]).get("evidence") == proof["group_pending_delete"] for e in events):
            raise WorkflowError("external cleanup requires the original native human PendingDelete acceptance event")
