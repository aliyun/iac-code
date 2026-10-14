"""OPS reviews exact operations, not whether the existing account is an administrator."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow.execution_gate import GateDeniedError, canonical, validate_grants

from .additional_source import validate_additional_source, validate_paid_operation_review
from .files import WorkflowError, contained, digest, read_json
from .phases import known_original_resolved_read

CREATION_LOADED_SHA = digest(Path(__file__))


def validate_stack_timeouts(packet: dict[str, Any]) -> None:
    """Validate cloud API parameters, never impose a model execution deadline."""
    for entries in packet.get("execution_grants", {}).values():
        for entry in entries:
            call = entry.get("call", {})
            if entry.get("kind") != "create" or (call.get("product", "").lower(), call.get("action")) != (
                "ros",
                "CreateStack",
            ):
                continue
            minutes = call.get("params", {}).get("TimeoutInMinutes", 60)
            if type(minutes) is not int or not 10 <= minutes <= 1440:
                raise WorkflowError("ROS creation timeout must be a valid API integer between 10 and 1440")


def execution_digest(packet: dict[str, Any]) -> str:
    execution = {
        "task_id": packet["task_id"],
        "budget_cents": packet["budget_cents"],
        "execution_grants": packet["execution_grants"],
        "accounts": sorted({binding["account"] for binding in packet["scenes"].values()}),
    }
    # An explicit new operation review, never a name/attempt change or counter refill.
    # Preserve every legacy digest byte-for-byte when no additional source is requested.
    if "additional_source_request" in packet:
        execution["additional_source_request"] = packet["additional_source_request"]
    if "additional_source_requests" in packet:
        execution["additional_source_requests"] = packet["additional_source_requests"]
    if "execution_selection" in packet:
        execution["execution_selection"] = packet["execution_selection"]
    if "query_identity_continuation" in packet:
        execution["query_identity_continuation"] = packet["query_identity_continuation"]
    if "preparation_selection" in packet:
        execution["preparation_selection"] = packet["preparation_selection"]
    if "preparation_selection_continuation" in packet:
        execution["preparation_selection_continuation"] = packet["preparation_selection_continuation"]
    if "hourly_cost" in packet:
        execution["hourly_cost"] = packet["hourly_cost"]
    return hashlib.sha256(canonical(execution).encode()).hexdigest()


def authorization_path(root: Path, packet: dict[str, Any]) -> Path:
    # Four rolling slots need independent authorizations; another resource cannot replace this one.
    return root / "creation-authorizations" / f"{execution_digest(packet)}.json"


def requires_execution_review(packet: dict[str, Any]) -> bool:
    """Initial QUERY uses Sol approval; additional operations require independent OPS review."""
    return bool((set(packet.get("execution_grants", {})) | set(packet.get("requests", {}))) - {"QUERY"}) or any(
        key in packet for key in (
            "additional_source_request", "additional_source_requests", "execution_selection", "preparation_selection",
            "preparation_selection_continuation", "hourly_cost",
        )
    ) or _continued_initial_query(packet)


def _continued_initial_query(packet: dict[str, Any]) -> bool:
    """Explicit continuation is not an unused initial QUERY permission."""
    return "query_identity_continuation" in packet


def execution_authority(root: Path, packet: dict[str, Any], dimension: str) -> str:
    """OPS-owned bindings retain cumulative limits for an unchanged dimension."""
    default = f"{execution_digest(packet)}:{dimension}"
    path = authorization_path(root, packet)
    approval = read_json(path) if path.exists() else {}
    return approval.get("dimension_authorities", {}).get(dimension, default)


def reviewed_authorities(root: Path, packet: dict[str, Any], proof: dict[str, Any]) -> dict[str, str]:
    """Carry exact old calls, including quota, without copying/resetting the wire journal."""
    validate_additional_source(root, packet, proof)
    bindings = carry_forward_authorities(root, packet, proof)
    # A reviewed operation retains one cumulative allowance even if combined
    # with other independently reviewed dimensions in a later packet.
    for request in packet.get("additional_source_requests", []):
        operation = {"task_id": packet["task_id"], "operation": request}
        bindings[request["dimension"]] = (
            hashlib.sha256(canonical(operation).encode()).hexdigest() + ":" + request["dimension"]
        )
    return bindings


def carry_forward_authorities(
    root: Path, packet: dict[str, Any], proof: dict[str, Any], *, review_admission: bool = True,
) -> dict[str, str]:
    """Read-only validation shared with mixed additional-source admission."""
    bindings = {dimension: f"{execution_digest(packet)}:{dimension}" for dimension in packet["execution_grants"]}
    carry = proof.get("carry_forward_dimensions", {})
    if not isinstance(carry, dict) or set(carry) - set(bindings):
        raise WorkflowError("invalid reviewed dimension carry-forward")
    if "preparation_selection" in packet:
        from .selection import validate_preparation_selection, validate_review

        validate_review(root, packet, proof)
        validate_preparation_selection(root, packet, admission=review_admission)
        if "preparation_selection_continuation" in packet:
            request = packet["additional_source_requests"][0]
            operation = {"task_id": packet["task_id"], "operation": request}
            bindings["QUERY"] = hashlib.sha256(canonical(operation).encode()).hexdigest() + ":QUERY"
        return bindings
    if _continued_initial_query(packet) and (
        set(packet.get("requests", {})) != {"QUERY"} or set(bindings) != {"QUERY"}
        or packet.get("budget_cents") != 0 or set(carry) != {"QUERY"}
        or not isinstance(packet["query_identity_continuation"], dict)
        or not packet["query_identity_continuation"]
        or carry["QUERY"].get("identity_continuation") != packet["query_identity_continuation"]
        or any(key in packet for key in (
            "additional_source_request", "additional_source_requests", "execution_selection",
        ))
    ):
        raise WorkflowError("initial QUERY continuation must bind only its exact reviewed identity request")
    composed = proof.get("kind") in {
        "ops_additional_source_review", "ops_additional_sources_review", "ops_read_continuations_review",
    }
    if composed:
        if proof.get("approved") is not True:
            raise WorkflowError("mixed read operations require independent OPS approval")
        for key in ("identity_continuation", "read_continuation"):
            reviews = proof.get(key + "_reviews", {})
            expected = {dimension for dimension, source in carry.items() if source.get(key) is not None}
            if not isinstance(reviews, dict) or set(reviews) != expected:
                raise WorkflowError("mixed operations need an exact explicit read review per continued dimension")
    for dimension, source in carry.items():
        old_path = contained(root, source["proposal"])
        if digest(old_path) != source["proposal_sha256"]:
            raise WorkflowError("original carry-forward proposal SHA changed")
        old = read_json(old_path)
        if old["task_id"] != packet["task_id"]:
            raise WorkflowError("carry-forward must retain identical task, calls, templates and quotas")
        continuation = source.get("identity_continuation")
        if dimension == "QUERY" and not authorization_path(root, old).exists() and isinstance(continuation, dict):
            # Initial read-only QUERY is legitimately approved by Sol, not OPS.
            # Prove that immutable original approval instead of inventing one.
            from .additional_source import _validate_initial_query_approval

            _validate_initial_query_approval(root, old, continuation.get("prior_scope", ""))
        else:
            validate_authorization(root, packet["task_id"].rsplit(":", 1)[-1], old)
        if execution_digest(old) != source["execution_sha256"]:
            raise WorkflowError("original carry-forward execution SHA changed")
        if sorted({v["account"] for v in old["scenes"].values()}) != sorted(
            {v["account"] for v in packet["scenes"].values()}
        ):
            raise WorkflowError("carry-forward account changed")
        authority = execution_authority(root, old, dimension)
        continuation_keys = [key for key in ("identity_continuation", "read_continuation")
                             if source.get(key) is not None]
        if len(continuation_keys) > 1:
            raise WorkflowError("use one explicit read continuation format per dimension")
        if continuation_keys:
            key = continuation_keys[0]
            identity_proof = proof
            if composed:
                identity_proof = proof[key + "_reviews"][dimension]
                if (
                    not isinstance(identity_proof, dict)
                    or identity_proof.get("dimension") != dimension
                    or identity_proof.get(key + "_approved") != source[key]
                    or identity_proof.get("proposal_sha256") != proof.get("proposal_sha256")
                    or not re.fullmatch(r"[a-f0-9]{64}", str(identity_proof.get("proposal_sha256", "")))
                ):
                    raise WorkflowError("mixed identity review must bind exact request, dimension and final proposal")
            _review_identity_continuation(
                root, packet, identity_proof, dimension, source, old, authority, review_admission=review_admission,
            )
            # Paid packets remain paid; the outer review holds the exact human
            # and cumulative cost evidence, independently from any read review.
            validate_paid_operation_review(root, packet, proof)
        elif old["execution_grants"].get(dimension) != packet["execution_grants"][dimension]:
            raise WorkflowError("carry-forward must retain identical task, calls, templates and quotas")
        bindings[dimension] = authority
    return bindings


def _review_identity_continuation(
    root: Path,
    packet: dict[str, Any],
    proof: dict[str, Any],
    dimension: str,
    source: dict[str, Any],
    old: dict[str, Any],
    authority: str,
    *,
    review_admission: bool = True,
) -> None:
    """Explicitly reviewed exhausted reads; never a fresh write allowance."""
    read_continuation = source.get("read_continuation") is not None
    key = "read_continuation" if read_continuation else "identity_continuation"
    request = source[key]
    if (
        proof.get("kind") != "ops_" + key + "_review"
        or proof.get("approved") is not True
        or proof.get("proposal_execution_sha256") != execution_digest(packet)
        or not isinstance(request, dict)
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,120}", str(request.get("request_id", "")))
        or not re.fullmatch(r"[a-f0-9]{32}:" + dimension, str(request.get("prior_scope", "")))
        or (read_continuation and proof.get(key + "_approved") != request)
    ):
        raise WorkflowError("identity continuation requires exact independent OPS read-only review")
    remediation = contained(root, request.get("remediation_evidence", "missing"))
    if not remediation.is_file() or digest(remediation) != request.get("remediation_sha256"):
        raise WorkflowError("identity continuation remediation evidence changed")
    entries = old["execution_grants"].get(dimension, [])
    identities = [entry for entry in entries if entry.get("kind") == "identity"]
    if len(identities) != 1:
        raise WorkflowError("identity continuation requires one original signed identity call")
    identity = identities[0]
    selected = request.get("grant_ids") if read_continuation else [identity["id"]]
    if (
        not isinstance(selected, list) or not selected
        or any(not isinstance(identifier, str) for identifier in selected)
        or len(set(selected)) != len(selected) or identity["id"] not in selected
        or set(selected) - {entry["id"] for entry in entries if entry["kind"] in {"identity", "read"}}
    ):
        raise WorkflowError("read continuation must explicitly select distinct original reads including identity")
    expected = json.loads(json.dumps(entries))
    for entry in expected:
        if entry["id"] in selected:
            if entry["quota"] >= 100:
                raise WorkflowError("read continuation exceeds the bounded original read limit")
            entry["quota"] += 1
    if (
        expected != packet["execution_grants"][dimension]
        or identity["call"].get("product", "").lower() != "sts"
        or identity["call"].get("action") != "GetCallerIdentity"
        or identity["call"].get("params") != {}
    ):
        raise WorkflowError("only one extra identity read; all other calls and quotas must remain identical")
    if not review_admission:
        # Native authorize-creation already proved exhausted terminal predecessor
        # scopes before freezing this exact review and cumulative authority. Once
        # installed, the authorized successor is live and consumes its allowance;
        # repeating that pre-admission test would reject even wait/context/cleanup.
        # Runtime still checks unchanged proposals, remedies, calls, review SHA,
        # account, budget and authority binding; the wire gate enforces live counts.
        return
    scope = request["prior_scope"]
    with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as state:
        job = state.execute("SELECT task_id,role,state,turn_id FROM jobs WHERE id=?", (scope.split(":")[0],)).fetchone()
        outstanding = state.execute(
            "SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'", (packet["task_id"],)
        ).fetchone()
        if not job or job[:3] != (packet["task_id"], "glm", "complete") or not job[3] or outstanding:
            raise WorkflowError("identity continuation requires terminal original execution and verified cleanup")
    with closing(sqlite3.connect(f"file:{(root / 'execution-gate.sqlite3').as_posix()}?mode=ro", uri=True)) as gate:
        unknown = gate.cursor()
        unknown.row_factory = sqlite3.Row
        rows = unknown.execute(
            "SELECT a.*,g.kind,g.call FROM attempts a LEFT JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE "
            "(a.scope=? OR a.scope IN (SELECT scope FROM grants WHERE authority=?)) AND a.state='unknown'",
            (scope, authority),
        ).fetchall()
        if any(not known_original_resolved_read(gate, row) for row in rows):
            raise WorkflowError("identity continuation refuses unknown original wire results")
        for entry in entries:
            limit = gate.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?", (authority, entry["id"])
            ).fetchone()
            grant = gate.execute(
                "SELECT authority,quota,used,kind,call FROM grants WHERE scope=? AND id=?", (scope, entry["id"])
            ).fetchone()
            if (
                not limit
                or not grant
                or grant[:2] != (authority, entry["quota"])
                or (entry["id"] not in selected and grant[2] != limit[0])
                or not 0 <= grant[2] <= limit[0]
                or grant[3:] != (entry["kind"], canonical(entry["call"]))
            ):
                raise WorkflowError("identity continuation original scope or cumulative authority differs")
            if entry["kind"] in {"create", "delete"} and (
                limit != (0,)
                or gate.execute("SELECT 1 FROM attempts WHERE scope=? AND grant_id=?", (scope, entry["id"])).fetchone()
            ):
                raise WorkflowError("identity continuation cannot replenish a used write allowance")
            if entry["id"] in selected:
                # A carried authority accumulates across terminal executions.
                # Each scope's local used count is not the cumulative balance.
                identity_grants = gate.execute(
                    "SELECT scope,quota,used,kind,call FROM grants WHERE authority=? AND id=?",
                    (authority, entry["id"]),
                ).fetchall()
                attempts = []
                with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as state:
                    for prior_scope, quota, used, kind, call in identity_grants:
                        prior_job = state.execute(
                            "SELECT task_id,role,state,turn_id FROM jobs WHERE id=?",
                            (prior_scope.split(":")[0],),
                        ).fetchone()
                        cursor = gate.cursor()
                        cursor.row_factory = sqlite3.Row
                        results = cursor.execute(
                            "SELECT a.*,g.kind,g.call FROM attempts a LEFT JOIN grants g "
                            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.grant_id=?",
                            (prior_scope, entry["id"]),
                        ).fetchall()
                        if (
                            not prior_scope.endswith(":" + dimension)
                            or not prior_job
                            or prior_job[:3] != (packet["task_id"], "glm", "complete")
                            or not prior_job[3]
                            or kind != entry["kind"] or call != canonical(entry["call"])
                            or not 0 <= used <= quota <= entry["quota"]
                            or len(results) != used
                        ):
                            raise WorkflowError("identity continuation cumulative terminal provenance differs")
                        attempts.extend(results)
                account = next(iter(packet["scenes"].values()))["account"]
                if (
                    limit != (entry["quota"],)
                    or sum(row[2] for row in identity_grants) != limit[0]
                    or len(attempts) != entry["quota"]
                    or any(
                        (
                            row["state"] != "succeeded" or json.loads(row["response"]).get("AccountId") != account
                        ) if entry["kind"] == "identity" else (
                            row["state"] != "succeeded" and not known_original_resolved_read(gate, row)
                        )
                        for row in attempts
                    )
                ):
                    raise WorkflowError("identity continuation needs exhausted successful original account identity")
    for path in (root / "creation-authorizations").glob("*.json"):
        approval = read_json(path)
        evidence = contained(root, approval.get("authorization_evidence", "missing"))
        previous = read_json(evidence).get("carry_forward_dimensions", {}) if evidence.is_file() else {}
        for prior in previous.values():
            for previous_key in ("identity_continuation", "read_continuation"):
                continuation = prior.get(previous_key, {})
                if continuation.get("request_id") == request["request_id"] and (
                    previous_key != key or continuation != request
                    or approval.get("execution_sha256") != execution_digest(packet)
                ):
                    raise WorkflowError("read continuation ID already bound to a different exact operation")


def ros_snat_parent_binding(packet: dict[str, Any]) -> dict[str, str] | None:
    """One explicit ROS-source retest, not general cross-product read authority.

    The independently reviewed additional operation binds the ROS identity and
    both precise VPC parents. Fresh product responses must still prove the join;
    this preparation binding never creates a resource reference or a verdict.
    """
    request = packet.get("additional_source_request", {})
    binding = request.get("ros_snat_parent_binding") if isinstance(request, dict) else None
    if binding is None:
        return None
    required = {"resource_type", "stack_id", "logical_resource_id", "identifier", "snat_table_id",
                "nat_gateway_id", "region_id"}
    if (
        not isinstance(binding, dict) or set(binding) != required
        or any(not isinstance(value, str) or not value or value.startswith("$") for value in binding.values())
        or request.get("dimension") != "ROS" or binding["resource_type"] != "ALIYUN::ECS::SNatEntry"
        or not re.fullmatch(r"snat-[A-Za-z0-9]+", binding["identifier"])
        or not re.fullmatch(r"stb-[A-Za-z0-9]+", binding["snat_table_id"])
        or not re.fullmatch(r"ngw-[A-Za-z0-9]+", binding["nat_gateway_id"])
        or not re.fullmatch(r"cn-[a-z0-9-]+", binding["region_id"])
        or set(packet.get("execution_grants", {})) != {"ROS"}
        or set(packet.get("requests", {})) != {"ROS"}
        or not packet.get("scenes") or packet.get("budget_cents") != 0
        or any(
            not scene.endswith(("-iframe", "-open")) or "-ROS-" not in scene
            or value.get("expected_type") != binding["resource_type"]
            or value.get("identifier") != binding["identifier"] or value.get("region") != binding["region_id"]
            for scene, value in packet["scenes"].items()
        )
    ):
        raise WorkflowError("ROS SNAT parent reads need one exact legacy ROS identity and bounded operation")
    region = binding["region_id"]
    expected = [
        ("identity", {"product": "sts", "action": "GetCallerIdentity", "version": "2015-04-01",
                      "region_id": region, "params": {}}),
        ("read", {"product": "ros", "action": "GetStackResource", "version": "2019-09-10",
                  "region_id": region, "params": {"RegionId": region, "StackId": binding["stack_id"],
                      "LogicalResourceId": binding["logical_resource_id"], "ShowResourceAttributes": True}}),
        ("read", {"product": "vpc", "action": "DescribeSnatTableEntries", "version": "2016-04-28",
                  "region_id": region, "params": {"RegionId": region, "SnatTableId": binding["snat_table_id"],
                      "SnatEntryId": binding["identifier"], "PageNumber": 1, "PageSize": 10}}),
        ("read", {"product": "vpc", "action": "DescribeNatGateways", "version": "2016-04-28",
                  "region_id": region, "params": {"RegionId": region,
                      "NatGatewayId": binding["nat_gateway_id"], "PageNumber": 1, "PageSize": 10}}),
    ]
    entries = packet["execution_grants"]["ROS"]
    if len(entries) != len(expected) or any(
        entry.get("kind") != kind or entry.get("quota") != 1 or entry.get("call") != call
        or (kind == "read" and entry.get("read_policy") != {"phase": "execute"})
        for entry, (kind, call) in zip(entries, expected)
    ):
        raise WorkflowError("ROS SNAT parent operation permits only one identity, ROS resource and two exact reads")
    return binding


def validate_execution(
    packet: dict[str, Any], needed: set[str], *, require_read_policy: bool = False,
    readonly_dimensions: set[str] | None = None,
    native_nas_query_sample: bool = False,
) -> None:
    grants = packet.get("execution_grants", {})
    readonly = {"QUERY"} | (readonly_dimensions or set())
    if not isinstance(grants, dict) or set(grants) != needed:
        raise WorkflowError("exact execution grants required for every missing dimension")
    try:
        if native_nas_query_sample:
            from scripts.web.e2e.resource_link_workflow.execution_gate import validate_nas_native_plan

            # Internal capability from immutable OPS admission, never a packet
            # switch. This exception cannot turn ordinary QUERY into a writer.
            if (needed != {"QUERY"} or not validate_nas_native_plan(grants["QUERY"])
                    or not isinstance(packet.get("cleanup_requests", {}).get("QUERY"), str)
                    or not packet["cleanup_requests"]["QUERY"].strip() or not packet.get("cleanup_recipe")):
                raise GateDeniedError()
            readonly.discard("QUERY")
        for dimension, entries in grants.items():
            validate_grants(entries, require_read_policy=require_read_policy)
            creates = [entry for entry in entries if entry["kind"] == "create"]
            if (dimension in readonly and any(entry["kind"] not in {"identity", "read"} for entry in entries)
                    or dimension not in readonly and not creates):
                raise GateDeniedError()
            if dimension in {"ROS", "TF"} and dimension in readonly:
                if dimension == "ROS" and ros_snat_parent_binding(packet) is not None:
                    continue
                reads = [entry["call"] for entry in entries if entry["kind"] == "read"]
                if any(entry.get("read_policy", {}).get("phase", "execute") != "execute"
                       for entry in entries if entry["kind"] == "read") or not reads or any(
                    call["product"].lower() != "ros"
                    or call["action"] not in {"GetStack", "GetStackResource", "ListStackResources"}
                    or not isinstance(call.get("params", {}).get("StackId"), str)
                    or not call["params"]["StackId"].strip()
                    or call["params"]["StackId"].startswith("$")
                    for call in reads
                ):
                    raise GateDeniedError()
                if dimension == "TF" and not any(call["action"] == "GetStack" for call in reads):
                    # Actual signed GetStack.StackType is checked again before navigation.
                    raise GateDeniedError()
    except (GateDeniedError, KeyError, TypeError, ValueError) as error:
        raise WorkflowError("invalid exact API capabilities or response-bound cleanup") from error
    if len({binding["account"] for binding in packet["scenes"].values()}) != 1:
        raise WorkflowError("one verified account per resource task required")


def validate_authorization(root: Path, resource_id: str, packet: dict[str, Any]) -> None:
    validate_stack_timeouts(packet)
    path = authorization_path(root, packet)
    approval = read_json(path) if path.exists() else {}
    if not approval.get("authorized") or resource_id not in approval.get("resource_ids", []):
        raise WorkflowError("creation needs explicit OPS authorization for this resource ID")
    version = approval.get("schema_version")
    if version not in {2, 3} or approval.get("identity_mode") != "existing_account":
        raise WorkflowError("review exact execution plan; old IAM-only attestation is not execution authorization")
    if version == 3:
        valid_lifetime = approval.get("lifetime") == "task" and approval.get("expires_at") is None
    else:
        valid_lifetime = approval.get("expires_at", 0) > time.time()
    if approval.get("execution_sha256") != execution_digest(packet) or not valid_lifetime:
        raise WorkflowError("OPS exact execution authorization changed or expired")
    account = next(iter(packet["scenes"].values()))["account"]
    if account != approval.get("account") or packet["budget_cents"] > approval.get("max_budget_cents", -1):
        raise WorkflowError("account or budget outside OPS authorization")
    evidence = contained(root, approval.get("authorization_evidence", "missing-authorization"))
    if not evidence.is_file() or digest(evidence) != approval.get("authorization_sha256"):
        raise WorkflowError("original user authorization/OPS operation review evidence changed")
    from .selection import validate_review

    proof = read_json(evidence)
    validate_review(root, packet, proof)
    if "hourly_cost" in packet:
        from .hourly_budget import approved_cost

        with closing(sqlite3.connect(f"{(root / 'state.sqlite3').as_uri()}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (packet["task_id"],)).fetchone()
            approved_cost(root, connection, task, packet)
    if "execution_selection" in packet or _continued_initial_query(packet):
        from .additional_source import validate_paid_operation_review

        proposal = contained(root, approval.get("proposal_evidence", "missing-selected-proposal"))
        if (
            not proposal.is_file() or digest(proposal) != proof.get("proposal_sha256")
            or approval.get("proposal_sha256") != proof.get("proposal_sha256")
            or read_json(proposal) != packet
        ):
            raise WorkflowError("continued/selection packet must be the exact independently reviewed full proposal")
        validate_paid_operation_review(root, packet, proof)
        if _continued_initial_query(packet) and not proof.get("carry_forward_dimensions", {}).get("QUERY", {}).get(
            "identity_continuation"
        ):
            raise WorkflowError("continued initial QUERY needs its exact independently reviewed identity continuation")
        authorities = carry_forward_authorities(root, packet, proof, review_admission=False)
        for dimension in proof["carry_forward_dimensions"]:
            if approval.get("dimension_authorities", {}).get(dimension) != authorities[dimension]:
                raise WorkflowError("selection authorization must retain its original cumulative authority")
        if "preparation_selection" in packet and approval.get("dimension_authorities") != authorities:
            raise WorkflowError("initial selection authorization must retain its sole reviewed operation authority")
    if "additional_source_request" in packet:
        proof = read_json(evidence)
        request = packet["additional_source_request"]
        if (
            approval.get("additional_source_request") != request
            or proof.get("kind") != "ops_additional_source_review"
            or proof.get("approved") is not True
            or proof.get("additional_source_approved") != request
        ):
            raise WorkflowError("explicit additional source OPS approval changed")
    if "additional_source_requests" in packet:
        proof = read_json(evidence)
        requests = packet["additional_source_requests"]
        if (
            approval.get("additional_source_requests") != requests
            or proof.get("kind") != "ops_additional_sources_review"
            or proof.get("approved") is not True
            or proof.get("additional_sources_approved") != requests
        ):
            raise WorkflowError("explicit additional operations OPS approval changed")
    writes = any(entry.get("kind") in {"create", "delete"}
                 for entries in packet["execution_grants"].values() for entry in entries)
    if writes and not packet.get("cleanup_recipe"):
        raise WorkflowError("creation requires reviewed cleanup recipe")
