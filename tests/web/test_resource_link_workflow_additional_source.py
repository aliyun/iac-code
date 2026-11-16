"""One explicitly reviewed additional source never resets original wire quotas."""

import copy
import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from scripts.web.e2e.resource_link_workflow.additional_source import (
    dispatch_reservation_increment,
    validate_additional_source,
)
from scripts.web.e2e.resource_link_workflow.creation import (
    authorization_path,
    execution_digest,
    requires_execution_review,
    reviewed_authorities,
    validate_authorization,
)
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write


@pytest.fixture
def initial_query(tmp_path):
    """Real-shaped initial QUERY has Sol approval, not retrospective creation permission."""
    job, prepare = "a" * 32, "b" * 32
    old = {
        "task_id": "plan:R0019",
        "budget_cents": 0,
        "profile": "test-guima",
        "requests": {"QUERY": "Read the first page of snapshots"},
        "scenes": {"R0019-QUERY-1-iframe": {"account": "fake"}},
        "execution_grants": {
            "QUERY": [
                {
                    "id": "identity",
                    "kind": "identity",
                    "quota": 1,
                    "call": {
                        "product": "sts",
                        "action": "GetCallerIdentity",
                        "version": "2015-04-01",
                        "region_id": "cn-hangzhou",
                        "params": {},
                    },
                },
                {
                    "id": "snapshots",
                    "kind": "read",
                    "quota": 1,
                    "read_policy": {"phase": "execute"},
                    "call": {
                        "product": "ecs",
                        "action": "DescribeSnapshots",
                        "version": "2014-05-26",
                        "region_id": "cn-hangzhou",
                        "params": {"RegionId": "cn-hangzhou", "PageNumber": 1, "PageSize": 10},
                    },
                },
            ]
        },
    }
    json_write(tmp_path / "inputs/old.json", old)
    json_write(tmp_path / "inputs/remediation.json", {"offline": "actual repair fixture"})
    json_write(tmp_path / f"packets/{prepare}/packet.json", old)
    with closing(sqlite3.connect(tmp_path / "state.sqlite3")) as db, db:
        db.executescript(
            "CREATE TABLE jobs(id,task_id,role,state,turn_id,created,purpose);CREATE TABLE owned(task_id,state);"
            "CREATE TABLE events(id INTEGER PRIMARY KEY,kind,data,created);"
        )
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
            (job, old["task_id"], "glm", "complete", "glm-turn", 30, "execute"),
        )
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
            (prepare, old["task_id"], "sol", "complete", "sol-turn", 10, "prepare"),
        )
        db.execute(
            "INSERT INTO events(kind,data,created) VALUES(?,?,?)",
            (
                "packet_approved",
                json.dumps(
                    {"task_id": old["task_id"], "packet_sha": digest(tmp_path / f"packets/{prepare}/packet.json")}
                ),
                20,
            ),
        )
    with closing(sqlite3.connect(tmp_path / "execution-gate.sqlite3")) as db, db:
        db.executescript(
            "CREATE TABLE grants(scope,id,authority,quota,used,kind,call);"
            "CREATE TABLE attempts(scope,grant_id,state,response);CREATE TABLE authority_limits(authority,id,used);"
            "CREATE TABLE identities(scope,account);"
        )
        scope, authority = job + ":QUERY", execution_digest(old) + ":QUERY"
        db.execute("INSERT INTO identities VALUES(?,?)", (scope, "fake"))
        for entry in old["execution_grants"]["QUERY"]:
            db.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
                (
                    scope,
                    entry["id"],
                    authority,
                    1,
                    1,
                    entry["kind"],
                    canonical(entry["call"]),
                ),
            )
            db.execute("INSERT INTO authority_limits VALUES(?,?,?)", (authority, entry["id"], 1))
            db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (scope, entry["id"], "succeeded", "{}"))
    new = copy.deepcopy(old)
    new["additional_source_requests"] = [
        {
            "request_id": "query-after-loader-repair-001",
            "dimension": "QUERY",
            "prior_scope": job + ":QUERY",
            "predecessor_proposal": "inputs/old.json",
            "predecessor_sha256": digest(tmp_path / "inputs/old.json"),
            "remediation_evidence": "inputs/remediation.json",
            "remediation_sha256": digest(tmp_path / "inputs/remediation.json"),
        }
    ]
    proof = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "additional_sources_approved": copy.deepcopy(new["additional_source_requests"]),
        "carry_forward_dimensions": {},
    }
    return tmp_path, old, new, proof


def test_initial_query_supplement_uses_real_sol_approval_without_inventing_old_permission(initial_query):
    root, old, new, proof = initial_query
    original = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    assert not requires_execution_review(old)
    assert requires_execution_review(new)
    validate_additional_source(root, new, proof)
    assert not authorization_path(root, old).exists()
    assert original == {name: (root / name).read_bytes() for name in original}
    with pytest.raises(WorkflowError, match="explicit OPS authorization"):
        validate_authorization(root, "R0019", new)
    json_write(root / "inputs/ops-review.json", proof)
    json_write(
        authorization_path(root, new),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0019"],
            "account": "fake",
            "max_budget_cents": 0,
            "execution_sha256": execution_digest(new),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/ops-review.json",
            "authorization_sha256": digest(root / "inputs/ops-review.json"),
            "additional_source_requests": new["additional_source_requests"],
        },
    )
    validate_authorization(root, "R0019", new)  # No fabricated cleanup recipe for a read-only operation.
    assert dispatch_reservation_increment(root, new) == 0
    assert not authorization_path(root, old).exists()
    assert original == {name: (root / name).read_bytes() for name in original}


@pytest.fixture
def initial_query_identity_continuation(initial_query):
    root, old, _, _ = initial_query
    with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
        db.execute("UPDATE grants SET used=0 WHERE kind='read'")
        db.execute("UPDATE authority_limits SET used=0 WHERE id='snapshots'")
        db.execute("DELETE FROM attempts WHERE grant_id='snapshots'")
        db.execute("UPDATE attempts SET response=?", (canonical({"AccountId": "fake"}),))
    new = copy.deepcopy(old)
    new["execution_grants"]["QUERY"][0]["quota"] = 2
    new["requests"]["QUERY"] = "Copy every approved parameter including params.RegionId"
    request = {
        "request_id": "initial-query-identity-repair-001",
        "prior_scope": "a" * 32 + ":QUERY",
        "remediation_evidence": "inputs/remediation.json",
        "remediation_sha256": digest(root / "inputs/remediation.json"),
    }
    new["query_identity_continuation"] = copy.deepcopy(request)
    json_write(root / "inputs/new.json", new)
    proof = {
        "kind": "ops_identity_continuation_review",
        "approved": True,
        "proposal_sha256": digest(root / "inputs/new.json"),
        "proposal_execution_sha256": execution_digest(new),
        "carry_forward_dimensions": {
            "QUERY": {
                "proposal": "inputs/old.json",
                "proposal_sha256": digest(root / "inputs/old.json"),
                "execution_sha256": execution_digest(old),
                "identity_continuation": request,
            }
        },
    }
    return root, old, new, proof


def test_initial_query_identity_continuation_keeps_unused_read_and_original_authority(
    initial_query_identity_continuation,
):
    root, old, new, proof = initial_query_identity_continuation
    original = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    assert not requires_execution_review(old)
    assert requires_execution_review(new)
    authority = execution_digest(old) + ":QUERY"
    assert reviewed_authorities(root, new, proof) == {"QUERY": authority}
    assert not authorization_path(root, old).exists()
    with pytest.raises(WorkflowError, match="explicit OPS authorization"):
        validate_authorization(root, "R0019", new)
    json_write(root / "inputs/ops-identity-review.json", proof)
    json_write(
        authorization_path(root, new),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0019"],
            "account": "fake",
            "max_budget_cents": 0,
            "execution_sha256": execution_digest(new),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/ops-identity-review.json",
            "authorization_sha256": digest(root / "inputs/ops-identity-review.json"),
            "proposal_evidence": "inputs/new.json",
            "proposal_sha256": digest(root / "inputs/new.json"),
            "dimension_authorities": {"QUERY": authority},
        },
    )
    validate_authorization(root, "R0019", new)
    assert original == {name: (root / name).read_bytes() for name in original}
    # Live successor consumption must not rerun exhausted-predecessor admission.
    with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
        db.execute("UPDATE authority_limits SET used=2 WHERE id='identity'")
    validate_authorization(root, "R0019", new)
    drifted = copy.deepcopy(new)
    drifted["requests"]["QUERY"] = "A different unreviewed request"
    with pytest.raises(WorkflowError, match="exact independently reviewed full proposal"):
        validate_authorization(root, "R0019", drifted)


@pytest.mark.parametrize(
    "damage",
    [
        "no_event",
        "prepare_active",
        "glm_active",
        "unknown_identity",
        "foreign_identity",
        "identity_not_exhausted",
        "read_quota_increased",
        "read_params_changed",
        "invalid_old_authorization",
        "unreviewed",
        "wrong_execution_sha",
        "wrong_scope",
        "remediation_changed",
        "outstanding",
        "marker_changed",
    ],
)
def test_initial_query_identity_continuation_refuses_unproven_or_expanded_permission(
    initial_query_identity_continuation,
    damage,
):
    root, old, new, proof = initial_query_identity_continuation
    if damage in {"no_event", "prepare_active", "glm_active", "outstanding"}:
        with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
            db.execute(
                {
                    "no_event": "DELETE FROM events",
                    "prepare_active": "UPDATE jobs SET state='running' WHERE role='sol'",
                    "glm_active": "UPDATE jobs SET state='running' WHERE role='glm'",
                    "outstanding": "INSERT INTO owned VALUES('plan:R0019','present')",
                }[damage]
            )
    elif damage in {"unknown_identity", "foreign_identity", "identity_not_exhausted"}:
        with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
            if damage == "unknown_identity":
                db.execute("UPDATE attempts SET state='unknown'")
            elif damage == "foreign_identity":
                db.execute("UPDATE attempts SET response=?", (canonical({"AccountId": "other"}),))
            else:
                db.execute("UPDATE authority_limits SET used=0 WHERE id='identity'")
    elif damage == "read_quota_increased":
        new["execution_grants"]["QUERY"][1]["quota"] = 2
    elif damage == "read_params_changed":
        new["execution_grants"]["QUERY"][1]["call"]["params"]["PageSize"] = 100
    elif damage == "invalid_old_authorization":
        json_write(authorization_path(root, old), {"authorized": False})
    elif damage == "unreviewed":
        proof["approved"] = False
    elif damage == "wrong_execution_sha":
        proof["proposal_execution_sha256"] = "0" * 64
    elif damage == "wrong_scope":
        proof["carry_forward_dimensions"]["QUERY"]["identity_continuation"]["prior_scope"] = "c" * 32 + ":QUERY"
    elif damage == "marker_changed":
        new["query_identity_continuation"]["request_id"] = "unreviewed-different-request"
    else:
        json_write(root / "inputs/remediation.json", {"changed": True})
    with pytest.raises(WorkflowError):
        reviewed_authorities(root, new, proof)


@pytest.fixture
def sae_custom_query(initial_query):
    root, old, _, _ = initial_query
    old["execution_grants"]["QUERY"][1] = {
        "id": "read-namespaces",
        "kind": "read",
        "quota": 1,
        "read_policy": {"phase": "execute"},
        "call": {
            "product": "sae",
            "action": "DescribeNamespaceList",
            "version": "2019-05-06",
            "region_id": "cn-hangzhou",
            "params": {"RegionId": "cn-hangzhou"},
        },
    }
    old["scenes"]["R0019-QUERY-1-iframe"].update(
        expected_type="ALIYUN::SAE::Namespace",
        region="cn-hangzhou",
    )
    json_write(root / "inputs/old.json", old)
    packet_path = root / ("packets/" + "b" * 32 + "/packet.json")
    json_write(packet_path, old)
    with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
        db.execute(
            "UPDATE events SET data=?",
            (
                json.dumps(
                    {
                        "task_id": old["task_id"],
                        "packet_sha": digest(packet_path),
                    }
                ),
            ),
        )
    with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
        db.execute("DELETE FROM grants")
        db.execute("DELETE FROM attempts")
        db.execute("DELETE FROM authority_limits")
        for entry in old["execution_grants"]["QUERY"]:
            authority = execution_digest(old) + ":QUERY"
            db.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
                (
                    "a" * 32 + ":QUERY",
                    entry["id"],
                    authority,
                    1,
                    1,
                    entry["kind"],
                    canonical(entry["call"]),
                ),
            )
            db.execute("INSERT INTO authority_limits VALUES(?,?,?)", (authority, entry["id"], 1))
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?)",
                (
                    "a" * 32 + ":QUERY",
                    entry["id"],
                    "succeeded",
                    json.dumps({"AccountId": "fake"}) if entry["kind"] == "identity" else "{}",
                ),
            )
    new = copy.deepcopy(old)
    new["execution_grants"]["QUERY"][1]["call"]["params"]["ContainCustom"] = True
    request = {
        "request_id": "reviewed-sae-custom-namespace-once",
        "dimension": "QUERY",
        "prior_scope": "a" * 32 + ":QUERY",
        "predecessor_proposal": "inputs/old.json",
        "predecessor_sha256": digest(root / "inputs/old.json"),
        "remediation_evidence": "inputs/remediation.json",
        "remediation_sha256": digest(root / "inputs/remediation.json"),
    }
    new["additional_source_requests"] = [request]
    json_write(root / "inputs/official.json", {"kind": "ops_official_custom_identity_review"})
    proof = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "additional_sources_approved": [copy.deepcopy(request)],
        "carry_forward_dimensions": {},
        "query_parameter_reviews": {
            request["request_id"]: {
                "kind": "ops_sae_include_custom_query_review",
                "approved": True,
                "grant_id": "read-namespaces",
                "original_params": {"RegionId": "cn-hangzhou"},
                "approved_params": {"RegionId": "cn-hangzhou", "ContainCustom": True},
                "official_evidence": "inputs/official.json",
                "official_sha256": digest(root / "inputs/official.json"),
            }
        },
    }
    return root, old, new, proof


def test_reviewed_sae_custom_read_retains_original_provenance_and_journal(sae_custom_query):
    root, old, new, proof = sae_custom_query
    before = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    validate_additional_source(root, new, proof)
    assert before == {name: (root / name).read_bytes() for name in before}
    assert old["execution_grants"]["QUERY"][1]["call"]["params"] == {"RegionId": "cn-hangzhou"}
    assert new["execution_grants"]["QUERY"][1]["quota"] == 1


@pytest.fixture
def sae_application_query(sae_custom_query):
    root, old, _, _ = sae_custom_query
    previous, selected, group = (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        ("00000000-0000-0000-0000-000000000003"),
    )
    read = old["execution_grants"]["QUERY"][1]
    read["call"].update(
        action="ListApplications",
        params={
            "FieldType": "appIds",
            "FieldValue": previous,
            "CurrentPage": 1,
            "PageSize": 10,
        },
    )
    for scene in old["scenes"].values():
        scene.update(expected_type="ALIYUN::SAE::Application", identifier=previous)
    json_write(root / "inputs/old.json", old)
    packet_path = root / ("packets/" + "b" * 32 + "/packet.json")
    json_write(packet_path, old)
    with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
        db.execute(
            "UPDATE events SET data=?", (json.dumps({"task_id": old["task_id"], "packet_sha": digest(packet_path)}),)
        )
    with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
        db.execute("DELETE FROM grants")
        db.execute("DELETE FROM authority_limits")
        for entry in old["execution_grants"]["QUERY"]:
            authority = execution_digest(old) + ":QUERY"
            db.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
                (
                    "a" * 32 + ":QUERY",
                    entry["id"],
                    authority,
                    1,
                    1,
                    entry["kind"],
                    canonical(entry["call"]),
                ),
            )
            db.execute("INSERT INTO authority_limits VALUES(?,?,?)", (authority, entry["id"], 1))
    new = copy.deepcopy(old)
    new["execution_grants"]["QUERY"][1]["call"]["params"]["FieldValue"] = selected
    calls = [
        copy.deepcopy(old["execution_grants"]["QUERY"][0]["call"]),
        {
            **read["call"],
            "params": {"CurrentPage": 1, "PageSize": 10},
        },
        {
            **read["call"],
            "action": "DescribeApplicationGroups",
            "params": {
                "AppId": selected,
                "CurrentPage": 1,
                "PageSize": 10,
            },
        },
    ]
    bodies = [
        {"AccountId": "fake", "RequestId": "identity"},
        {
            "Success": True,
            "Code": 200,
            "RequestId": "list",
            "CurrentPage": 1,
            "PageSize": 10,
            "Data": {
                "CurrentPage": 1,
                "PageSize": 10,
                "Applications": [
                    {
                        "AppId": selected,
                        "AppName": "offline-app",
                        "AppDescription": "offline-desc",
                        "RegionId": "cn-hangzhou",
                    }
                ],
            },
        },
        {
            "Success": True,
            "Code": 200,
            "RequestId": "group",
            "Data": [
                {
                    "GroupId": group,
                    "GroupType": 0,
                    "GroupName": "_DEFAULT_GROUP",
                }
            ],
        },
    ]
    events = []
    for index, (call, body) in enumerate(zip(calls, bodies)):
        path = f"inputs/original-{index}.json"
        json_write(root / path, body)
        events.append(
            {
                "call": call,
                "path": path,
                "sha256": digest(root / path),
                "error": False,
                "request_id": body["RequestId"],
                "started_at": index * 2,
                "completed_at": index * 2 + 1,
            }
        )
    json_write(
        root / "inputs/diagnosis.json",
        {
            "kind": "ops_sae_existing_application_group_diagnosis",
            "task_id": old["task_id"],
            "events": events,
        },
    )
    selection = {
        "grant_id": read["id"],
        "changed_param": "FieldValue",
        "original_value": previous,
        "proposed_value": selected,
        "source_evidence": events[1]["path"],
        "source_sha256": events[1]["sha256"],
        "source_pointer": "/Data/Applications/0",
        "group_evidence": events[2]["path"],
        "group_sha256": events[2]["sha256"],
        "same_account": "fake",
        "same_region": "cn-hangzhou",
        "all_other_call_fields_and_quotas_unchanged": True,
    }
    for scene in new["scenes"].values():
        scene.update(
            identifier=selected,
            source_evidence=events[1]["path"],
            source_sha256=events[1]["sha256"],
            source_pointer=selection["source_pointer"],
            render_text=["offline-app", "offline-desc"],
        )
    request = {
        "request_id": "reviewed-existing-sae-application-once",
        "dimension": "QUERY",
        "prior_scope": "a" * 32 + ":QUERY",
        "predecessor_proposal": "inputs/old.json",
        "predecessor_sha256": digest(root / "inputs/old.json"),
        "remediation_evidence": "inputs/remediation.json",
        "remediation_sha256": digest(root / "inputs/remediation.json"),
        "query_sample_reselection": selection,
    }
    new["additional_source_requests"] = [request]
    json_write(
        root / "inputs/official.json",
        {
            "kind": "ops_official_sae_existing_application_review",
            "sources": [
                {
                    "path": "inputs/remediation.json",
                    "sha256": digest(root / "inputs/remediation.json"),
                }
            ],
        },
    )
    review = {
        "kind": "ops_sae_existing_application_sample_review",
        "approved": True,
        "sample_selection_approved": copy.deepcopy(selection),
        "grant_id": read["id"],
        "original_params": copy.deepcopy(read["call"]["params"]),
        "approved_params": copy.deepcopy(new["execution_grants"]["QUERY"][1]["call"]["params"]),
        "diagnostic_evidence": "inputs/diagnosis.json",
        "diagnostic_sha256": digest(root / "inputs/diagnosis.json"),
        "default_group_id": group,
        "official_evidence": "inputs/official.json",
        "official_sha256": digest(root / "inputs/official.json"),
    }
    proof = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "additional_sources_approved": [copy.deepcopy(request)],
        "carry_forward_dimensions": {},
        "query_parameter_reviews": {request["request_id"]: review},
    }
    return root, old, new, proof


def test_existing_sae_sample_reselection_keeps_original_authority_and_wire_readonly(sae_application_query):
    root, old, new, proof = sae_application_query
    original = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    validate_additional_source(root, new, proof)
    assert original == {name: (root / name).read_bytes() for name in original}
    assert (
        old["execution_grants"]["QUERY"][1]["call"]["params"]["FieldValue"]
        != new["execution_grants"]["QUERY"][1]["call"]["params"]["FieldValue"]
    )


@pytest.mark.parametrize(
    "damage",
    [
        "unapproved",
        "missing-review",
        "selection",
        "before",
        "after",
        "diagnostic-sha",
        "official-sha",
        "group",
        "quota",
        "bool-quota",
        "page",
        "bool-page",
        "identity",
        "region",
        "action",
        "version",
        "read-policy",
        "scene-account",
        "scene-region",
        "scene-type",
        "scene-id",
        "scene-pointer",
        "scene-markers",
        "scene-source",
        "unknown",
        "unused",
        "wire-call",
        "limit",
        "raw-sha",
        "extra-review",
    ],
)
def test_sae_reselection_rejects_unreviewed_or_wider_operations(sae_application_query, damage):
    root, _, new, proof = sae_application_query
    request = new["additional_source_requests"][0]
    review = proof["query_parameter_reviews"][request["request_id"]]
    read = new["execution_grants"]["QUERY"][1]
    scene = next(iter(new["scenes"].values()))
    if damage == "unapproved":
        review["approved"] = False
    elif damage == "missing-review":
        proof.pop("query_parameter_reviews")
    elif damage in {"selection", "before", "after", "diagnostic-sha", "official-sha", "group"}:
        review[
            {
                "selection": "sample_selection_approved",
                "before": "original_params",
                "after": "approved_params",
                "diagnostic-sha": "diagnostic_sha256",
                "official-sha": "official_sha256",
                "group": "default_group_id",
            }[damage]
        ] = "other"
    elif damage in {"quota", "bool-quota"}:
        read["quota"] = True if damage == "bool-quota" else 2
    elif damage in {"page", "bool-page"}:
        read["call"]["params"]["CurrentPage"] = True if damage == "bool-page" else 2
    elif damage == "identity":
        new["execution_grants"]["QUERY"][0]["quota"] = 2
    elif damage in {"region", "action", "version"}:
        read["call"]["region_id" if damage == "region" else damage] = "other"
    elif damage == "read-policy":
        read["read_policy"] = {"phase": "cleanup"}
    elif damage.startswith("scene-"):
        scene[
            {
                "scene-account": "account",
                "scene-region": "region",
                "scene-type": "expected_type",
                "scene-id": "identifier",
                "scene-pointer": "source_pointer",
                "scene-markers": "render_text",
                "scene-source": "source_evidence",
            }[damage]
        ] = "other"
    elif damage == "raw-sha":
        json_write(root / "inputs/original-1.json", {"changed": True})
    elif damage == "extra-review":
        proof["query_parameter_reviews"]["unbound"] = copy.deepcopy(review)
    else:
        with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
            db.execute(
                {
                    "unknown": "UPDATE attempts SET state='unknown'",
                    "unused": "UPDATE grants SET used=0",
                    "wire-call": "UPDATE grants SET call='{}' WHERE kind='read'",
                    "limit": "UPDATE authority_limits SET used=0",
                }[damage]
            )
    original = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)
    assert original == {name: (root / name).read_bytes() for name in original}


@pytest.mark.parametrize(
    "damage",
    [
        "signed-account",
        "request-id",
        "foreign-group-call",
        "foreign-list-region",
        "unordered",
        "error",
        "duplicate-call",
        "duplicate-app",
        "row-region",
        "empty-name",
        "empty-description",
        "null-list",
        "bool-page",
        "list-failed",
        "null-group",
        "missing-default",
        "duplicate-default",
        "bool-group-type",
        "empty-group-id",
        "group-failed",
    ],
)
def test_sae_reselection_revalidates_semantics_even_if_all_candidate_shas_are_updated(sae_application_query, damage):
    root, _, new, proof = sae_application_query
    request = new["additional_source_requests"][0]
    review = proof["query_parameter_reviews"][request["request_id"]]
    diagnosis = json.loads((root / "inputs/diagnosis.json").read_text(encoding="utf-8"))
    bodies = [json.loads((root / f"inputs/original-{index}.json").read_text(encoding="utf-8")) for index in range(3)]
    if damage == "signed-account":
        bodies[0]["AccountId"] = "foreign"
    elif damage == "request-id":
        bodies[1]["RequestId"] = "foreign"
    elif damage == "foreign-group-call":
        diagnosis["events"][2]["call"]["params"]["AppId"] = request["query_sample_reselection"]["original_value"]
    elif damage == "foreign-list-region":
        diagnosis["events"][1]["call"]["region_id"] = "cn-beijing"
    elif damage == "unordered":
        diagnosis["events"][2]["started_at"] = 0
    elif damage == "error":
        diagnosis["events"][2]["error"] = True
    elif damage == "duplicate-call":
        diagnosis["events"].append(copy.deepcopy(diagnosis["events"][2]))
    elif damage == "duplicate-app":
        bodies[1]["Data"]["Applications"] *= 2
    elif damage in {"row-region", "empty-name", "empty-description"}:
        bodies[1]["Data"]["Applications"][0][
            {
                "row-region": "RegionId",
                "empty-name": "AppName",
                "empty-description": "AppDescription",
            }[damage]
        ] = "cn-beijing" if damage == "row-region" else ""
    elif damage == "null-list":
        bodies[1]["Data"] = None
    elif damage == "bool-page":
        bodies[1]["CurrentPage"] = True
    elif damage == "list-failed":
        bodies[1]["Success"] = False
    elif damage == "null-group":
        bodies[2]["Data"] = None
    elif damage == "missing-default":
        bodies[2]["Data"][0]["GroupName"] = "other"
    elif damage == "duplicate-default":
        bodies[2]["Data"] *= 2
    elif damage == "bool-group-type":
        bodies[2]["Data"][0]["GroupType"] = False
    elif damage == "empty-group-id":
        bodies[2]["Data"][0]["GroupId"] = ""
    else:
        bodies[2]["Code"] = 403
    for index, body in enumerate(bodies):
        path = root / f"inputs/original-{index}.json"
        json_write(path, body)
        diagnosis["events"][index]["sha256"] = digest(path)
    json_write(root / "inputs/diagnosis.json", diagnosis)
    selection = request["query_sample_reselection"]
    selection["source_sha256"] = digest(root / "inputs/original-1.json")
    selection["group_sha256"] = digest(root / "inputs/original-2.json")
    for scene in new["scenes"].values():
        scene["source_sha256"] = selection["source_sha256"]
    review["sample_selection_approved"] = copy.deepcopy(selection)
    review["diagnostic_sha256"] = digest(root / "inputs/diagnosis.json")
    proof["additional_sources_approved"] = copy.deepcopy(new["additional_source_requests"])
    original = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)
    assert original == {name: (root / name).read_bytes() for name in original}


@pytest.mark.parametrize(
    "damage",
    [
        "missing_review",
        "unapproved",
        "wrong_kind",
        "wrong_id",
        "wrong_before",
        "wrong_after",
        "official_sha",
        "official_kind",
        "foreign_region",
        "wrong_product",
        "wrong_action",
        "wrong_version",
        "extra_param",
        "string_true",
        "integer_true",
        "read_quota",
        "identity_quota",
        "identity_changed",
        "wrong_type",
        "extra_review",
        "unknown_predecessor",
        "unused_predecessor",
        "wire_changed",
        "limit_changed",
        "signer_changed",
    ],
)
def test_sae_custom_read_exception_refuses_unreviewed_or_wider_calls(sae_custom_query, damage):
    root, _, new, proof = sae_custom_query
    request = new["additional_source_requests"][0]
    review = proof["query_parameter_reviews"][request["request_id"]]
    entry = new["execution_grants"]["QUERY"][1]
    if damage == "missing_review":
        del proof["query_parameter_reviews"]
    elif damage in {"unapproved", "wrong_kind", "wrong_id", "wrong_before", "wrong_after", "official_sha"}:
        key, value = {
            "unapproved": ("approved", False),
            "wrong_kind": ("kind", "generic"),
            "wrong_id": ("grant_id", "other"),
            "wrong_before": ("original_params", {}),
            "wrong_after": ("approved_params", {"ContainCustom": True}),
            "official_sha": ("official_sha256", "0" * 64),
        }[damage]
        review[key] = value
    elif damage == "official_kind":
        json_write(root / "inputs/official.json", {"kind": "other"})
        review["official_sha256"] = digest(root / "inputs/official.json")
    elif damage == "foreign_region":
        entry["call"]["region_id"] = "cn-beijing"
    elif damage.startswith("wrong_") and damage != "wrong_type":
        entry["call"][damage.removeprefix("wrong_")] = "other"
    elif damage == "extra_param":
        entry["call"]["params"]["HybridCloudExclude"] = True
    elif damage == "string_true":
        entry["call"]["params"]["ContainCustom"] = "true"
    elif damage == "integer_true":
        entry["call"]["params"]["ContainCustom"] = 1
    elif damage == "read_quota":
        entry["quota"] = 2
    elif damage == "identity_quota":
        new["execution_grants"]["QUERY"][0]["quota"] = 2
    elif damage == "identity_changed":
        new["execution_grants"]["QUERY"][0]["call"]["params"] = {"other": "value"}
    elif damage == "wrong_type":
        next(iter(new["scenes"].values()))["expected_type"] = "ALIYUN::ECS::Instance"
    elif damage == "extra_review":
        proof["query_parameter_reviews"]["unbound-operation"] = copy.deepcopy(review)
    else:
        with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
            statement = {
                "unknown_predecessor": "UPDATE attempts SET state='unknown'",
                "unused_predecessor": "UPDATE grants SET used=0",
                "wire_changed": "UPDATE grants SET call='{}' WHERE kind='read'",
                "limit_changed": "UPDATE authority_limits SET used=0",
                "signer_changed": "UPDATE attempts SET response='{}' WHERE grant_id='identity'",
            }[damage]
            db.execute(statement)
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)


@pytest.mark.parametrize(
    "damage",
    [
        "no_event",
        "wrong_event_task",
        "last_approval_changed",
        "packet_drift",
        "packet_changed_format",
        "prepare_active",
        "prepare_wrong_role",
        "prepare_no_turn",
        "prepare_after_approval",
        "event_after_glm",
        "glm_active",
        "glm_no_turn",
        "unknown_wire",
        "unused_wire",
        "foreign_identity",
        "no_identity",
        "wire_call_changed",
        "wire_kind_changed",
        "outstanding",
        "existing_invalid_authorization",
        "unreviewed",
        "changed_successor_calls",
        "foreign_successor",
        "predecessor_sha",
        "remediation_sha",
        "malformed_event",
    ],
)
def test_initial_query_supplement_rejects_unproven_or_drifted_predecessor(initial_query, damage):
    root, old, new, proof = initial_query
    if damage in {
        "no_event",
        "wrong_event_task",
        "last_approval_changed",
        "prepare_active",
        "prepare_wrong_role",
        "prepare_no_turn",
        "prepare_after_approval",
        "event_after_glm",
        "glm_active",
        "glm_no_turn",
        "outstanding",
        "malformed_event",
    }:
        with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
            statements = {
                "no_event": "DELETE FROM events",
                "wrong_event_task": "UPDATE events SET data='{}'",
                "prepare_active": "UPDATE jobs SET state='running' WHERE role='sol'",
                "prepare_wrong_role": "UPDATE jobs SET role='glm' WHERE role='sol'",
                "prepare_no_turn": "UPDATE jobs SET turn_id=NULL WHERE role='sol'",
                "prepare_after_approval": "UPDATE jobs SET created=25 WHERE role='sol'",
                "event_after_glm": "UPDATE events SET created=40",
                "glm_active": "UPDATE jobs SET state='running' WHERE role='glm'",
                "glm_no_turn": "UPDATE jobs SET turn_id=NULL WHERE role='glm'",
                "outstanding": "INSERT INTO owned VALUES('plan:R0019','present')",
                "malformed_event": "UPDATE events SET data='[]'",
            }
            if damage == "last_approval_changed":
                db.execute(
                    "INSERT INTO events(kind,data,created) VALUES(?,?,?)",
                    (
                        "packet_approved",
                        json.dumps({"task_id": old["task_id"], "packet_sha": "0" * 64}),
                        21,
                    ),
                )
            else:
                db.execute(statements[damage])
    elif damage.startswith("packet_"):
        path = root / ("packets/" + "b" * 32 + "/packet.json")
        value = copy.deepcopy(old)
        if damage == "packet_drift":
            value["requests"]["QUERY"] = "different request"
        path.write_text(json.dumps(value), encoding="utf-8")
    elif damage in {
        "unknown_wire",
        "unused_wire",
        "foreign_identity",
        "no_identity",
        "wire_call_changed",
        "wire_kind_changed",
    }:
        with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
            db.execute(
                {
                    "unknown_wire": "UPDATE attempts SET state='unknown'",
                    "unused_wire": "UPDATE grants SET used=0",
                    "foreign_identity": "UPDATE identities SET account='foreign'",
                    "no_identity": "DELETE FROM identities",
                    "wire_call_changed": "UPDATE grants SET call='{}' WHERE kind='read'",
                    "wire_kind_changed": "UPDATE grants SET kind='create' WHERE kind='read'",
                }[damage]
            )
    elif damage == "existing_invalid_authorization":
        json_write(authorization_path(root, old), {"authorized": False})
    elif damage == "unreviewed":
        proof["approved"] = False
    elif damage == "changed_successor_calls":
        new["execution_grants"]["QUERY"][1]["call"]["params"]["PageSize"] = 100
    elif damage == "foreign_successor":
        new["scenes"]["R0019-QUERY-1-iframe"]["account"] = "foreign"
    else:
        new["additional_source_requests"][0][damage + "256"] = "0" * 64
        proof["additional_sources_approved"] = copy.deepcopy(new["additional_source_requests"])
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)


@pytest.mark.parametrize(
    "key",
    [
        "additional_source_request",
        "additional_source_requests",
        "execution_selection",
        "query_identity_continuation",
    ],
)
def test_even_empty_supplement_marker_requires_ops_review(initial_query, key):
    _, old, _, _ = initial_query
    old[key] = []
    assert requires_execution_review(old)


@pytest.fixture
def source(tmp_path):
    old = {
        "task_id": "plan:R0015",
        "budget_cents": 0,
        "scenes": {"scene": {"account": "fake"}},
        "cleanup_recipe": {"ROS": "original"},
        "execution_grants": {
            "ROS": [{"id": "create", "kind": "create", "quota": 1}, {"id": "delete", "kind": "delete", "quota": 1}]
        },
    }
    json_write(tmp_path / "inputs/old.json", old)
    json_write(tmp_path / "inputs/remediation.json", {"real_evidence": "offline fixture"})
    json_write(tmp_path / "inputs/review.json", {"approved": True})
    json_write(
        authorization_path(tmp_path, old),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0015"],
            "account": "fake",
            "max_budget_cents": 0,
            "execution_sha256": execution_digest(old),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/review.json",
            "authorization_sha256": digest(tmp_path / "inputs/review.json"),
        },
    )
    job = "a" * 32
    with closing(sqlite3.connect(tmp_path / "state.sqlite3")) as db, db:
        db.executescript("CREATE TABLE jobs(id,task_id,role,state,turn_id);CREATE TABLE owned(task_id,state);")
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job, "plan:R0015", "glm", "complete", "original-turn"))
        db.execute("INSERT INTO owned VALUES(?,?)", ("plan:R0015", "deleted"))
    with closing(sqlite3.connect(tmp_path / "execution-gate.sqlite3")) as db, db:
        db.executescript(
            "CREATE TABLE grants(scope,id,authority,quota,used,kind,call);"
            "CREATE TABLE attempts(scope,grant_id,state,response);CREATE TABLE authority_limits(authority,id,used);"
        )
        for grant in ("create", "delete"):
            authority = execution_digest(old) + ":ROS"
            db.execute(
                "INSERT INTO grants(scope,id,authority,quota,used) VALUES(?,?,?,?,?)",
                (job + ":ROS", grant, authority, 1, 1),
            )
            db.execute("INSERT INTO authority_limits VALUES(?,?,?)", (authority, grant, 1))
            db.execute("INSERT INTO attempts(scope,grant_id,state) VALUES(?,?,?)", (job + ":ROS", grant, "succeeded"))
    new = copy.deepcopy(old)
    new["additional_source_request"] = {
        "request_id": "source-after-remediation-001",
        "dimension": "ROS",
        "prior_scope": job + ":ROS",
        "predecessor_proposal": "inputs/old.json",
        "predecessor_sha256": digest(tmp_path / "inputs/old.json"),
        "remediation_evidence": "inputs/remediation.json",
        "remediation_sha256": digest(tmp_path / "inputs/remediation.json"),
    }
    proof = {
        "kind": "ops_additional_source_review",
        "approved": True,
        "additional_source_approved": copy.deepcopy(new["additional_source_request"]),
    }
    return tmp_path, old, new, proof


def test_explicit_additional_source_is_readonly_and_preserves_predecessor(source):
    root, old, new, proof = source
    original = (root / "execution-gate.sqlite3").read_bytes()
    validate_additional_source(root, new, proof)
    assert (root / "execution-gate.sqlite3").read_bytes() == original
    assert execution_digest(old) != execution_digest(new)
    assert authorization_path(root, old) != authorization_path(root, new)


@pytest.fixture
def paid_source(source):
    root, old, new, proof = source
    old["budget_cents"] = new["budget_cents"] = 100
    json_write(root / "inputs/old.json", old)
    request = new["additional_source_request"]
    request["predecessor_sha256"] = digest(root / "inputs/old.json")
    proof["additional_source_approved"] = copy.deepcopy(request)
    json_write(
        authorization_path(root, old),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0015"],
            "account": "fake",
            "max_budget_cents": 100,
            "execution_sha256": execution_digest(old),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/review.json",
            "authorization_sha256": digest(root / "inputs/review.json"),
        },
    )
    with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
        db.execute("UPDATE grants SET authority=?", (execution_digest(old) + ":ROS",))
        db.execute("UPDATE authority_limits SET authority=?", (execution_digest(old) + ":ROS",))
    with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
        db.executescript(
            "CREATE TABLE tasks(id,run_id);CREATE TABLE runs(id,budget);"
            "CREATE TABLE budget_reservations(task_id,amount);"
        )
        db.execute("INSERT INTO tasks VALUES('plan:R0015','plan')")
        db.execute("INSERT INTO runs VALUES('plan',100)")
        db.execute("INSERT INTO budget_reservations VALUES('plan:R0015',100)")
    json_write(
        root / "inputs/human.json",
        {
            "kind": "human_fee_authorization",
            "approved": True,
            "task_id": "plan:R0015",
            "plan_id": "plan",
            "max_budget_cents": 100,
            "verbatim_user_message": "offline fake user approves paid empty resource",
        },
    )
    cost = {
        "kind": "ops_cumulative_fee_review",
        "approved": True,
        "task_id": "plan:R0015",
        "account": "fake",
        "budget_cents": 100,
        "execution_sha256": execution_digest(new),
        "additional_operations": [request],
        "human_authorization": "inputs/human.json",
        "human_sha256": digest(root / "inputs/human.json"),
        "includes_previous_operations": True,
        "estimate_basis": "offline fake cumulative fee estimate",
        "limitations": "Not a measured bill or cloud-enforced cap; includes previous and remaining operations",
        "official_sources": [{"path": "inputs/remediation.json", "sha256": digest(root / "inputs/remediation.json")}],
    }
    json_write(root / "inputs/cost.json", cost)
    proof["paid_operation_review"] = {
        "human_authorization": "inputs/human.json",
        "human_sha256": digest(root / "inputs/human.json"),
        "cost_evidence": "inputs/cost.json",
        "cost_sha256": digest(root / "inputs/cost.json"),
    }
    proof["budget_reservation_reuse"] = {
        "task_id": new["task_id"],
        "reserved_cents": 100,
        "cost_evidence": "inputs/cost.json",
        "cost_sha256": digest(root / "inputs/cost.json"),
    }
    return root, old, new, proof


@pytest.mark.parametrize("reuse", [True, False])
def test_paid_additional_operation_preserves_truthful_budget_and_wire(paid_source, reuse):
    root, _, new, proof = paid_source
    if not reuse:
        proof.pop("budget_reservation_reuse")
    before = (root / "execution-gate.sqlite3").read_bytes()
    validate_additional_source(root, new, proof)
    json_write(root / "inputs/paid-review.json", proof)
    json_write(
        authorization_path(root, new),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0015"],
            "account": "fake",
            "max_budget_cents": 100,
            "execution_sha256": execution_digest(new),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/paid-review.json",
            "authorization_sha256": digest(root / "inputs/paid-review.json"),
            "additional_source_request": new["additional_source_request"],
        },
    )
    assert dispatch_reservation_increment(root, new) == (0 if reuse else 100)
    assert new["budget_cents"] == 100
    assert (root / "execution-gate.sqlite3").read_bytes() == before
    with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
        assert db.execute("SELECT amount FROM budget_reservations").fetchall() == [(100,)]


@pytest.mark.parametrize(
    "damage",
    [
        "missing_review",
        "human_changed",
        "cost_changed",
        "prior_cost_excluded",
        "wrong_account",
        "wrong_operations",
        "human_denied",
        "human_low_cap",
        "official_changed",
        "wrong_task",
        "missing_reservation",
        "reserve_changed",
        "run_low_cap",
        "zero_paid",
        "boolean_budget",
    ],
)
def test_paid_additional_operation_rejects_missing_or_drifted_fee_scope(paid_source, damage):
    root, _, new, proof = paid_source
    if damage == "missing_review":
        proof.pop("paid_operation_review")
    elif damage in {"human_changed", "cost_changed", "official_changed"}:
        path = {"human_changed": "human.json", "cost_changed": "cost.json", "official_changed": "remediation.json"}[
            damage
        ]
        json_write(root / "inputs" / path, {"changed": True})
    elif damage in {"prior_cost_excluded", "wrong_account", "wrong_operations"}:
        cost = json.loads((root / "inputs/cost.json").read_text(encoding="utf-8"))
        cost[
            {
                "prior_cost_excluded": "includes_previous_operations",
                "wrong_account": "account",
                "wrong_operations": "additional_operations",
            }[damage]
        ] = {
            "prior_cost_excluded": False,
            "wrong_account": "other",
            "wrong_operations": [],
        }[damage]
        json_write(root / "inputs/cost.json", cost)
        sha = digest(root / "inputs/cost.json")
        proof["paid_operation_review"]["cost_sha256"] = proof["budget_reservation_reuse"]["cost_sha256"] = sha
    elif damage in {"human_denied", "human_low_cap"}:
        human = json.loads((root / "inputs/human.json").read_text(encoding="utf-8"))
        key = "approved" if damage == "human_denied" else "max_budget_cents"
        human[key] = False if damage == "human_denied" else 99
        json_write(root / "inputs/human.json", human)
        proof["paid_operation_review"]["human_sha256"] = digest(root / "inputs/human.json")
    elif damage == "wrong_task":
        proof["budget_reservation_reuse"]["task_id"] = "plan:other"
    elif damage in {"missing_reservation", "reserve_changed", "run_low_cap"}:
        with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
            db.execute(
                {
                    "missing_reservation": "DELETE FROM budget_reservations",
                    "reserve_changed": "UPDATE budget_reservations SET amount=99",
                    "run_low_cap": "UPDATE runs SET budget=99",
                }[damage]
            )
    else:
        new["budget_cents"] = False if damage == "boolean_budget" else 0
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)


@pytest.mark.parametrize("damage", [None, "unknown_write", "missing_identity", "other_403", "outstanding"])
def test_additional_source_preserves_known_execute_signature_rejection_after_verified_cleanup(
    source,
    monkeypatch,
    damage,
):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    root, old, new, proof = source
    job = "a" * 32
    scope = job + ":ROS"
    entries = [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 1,
            "call": {
                "product": "sts",
                "action": "GetCallerIdentity",
                "region_id": "cn-hangzhou",
                "params": {},
            },
        },
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": {
                "product": "ros",
                "action": "CreateStack",
                "region_id": "cn-hangzhou",
                "params": {"StackName": "offline"},
            },
        },
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "parent": "create",
            "target_param": "StackId",
            "response_path": ["StackId"],
            "call": {
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "$created_id"},
            },
        },
        {
            "id": "detail",
            "kind": "read",
            "quota": 1,
            "read_policy": {"phase": "execute"},
            "call": {
                "product": "oss",
                "action": "GetBucketInfo",
                "region_id": "cn-hangzhou",
                "version": "2019-05-17",
                "params": {"bucket": "offline-bucket"},
            },
        },
    ]
    old["execution_grants"]["ROS"] = copy.deepcopy(entries)
    new["execution_grants"]["ROS"] = copy.deepcopy(entries)
    json_write(root / "inputs/old.json", old)
    new["additional_source_request"]["predecessor_sha256"] = digest(root / "inputs/old.json")
    proof["additional_source_approved"] = copy.deepcopy(new["additional_source_request"])
    json_write(
        authorization_path(root, old),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0015"],
            "account": "fake",
            "max_budget_cents": 0,
            "execution_sha256": execution_digest(old),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/review.json",
            "authorization_sha256": digest(root / "inputs/review.json"),
        },
    )
    path = root / "full-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    gate.install(
        path, "0", scope, "ws~original", "fake", time.time() + 1000, entries, authority=execution_digest(old) + ":ROS"
    )
    signer = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    gate.finish(gate.reserve(entries[0]["call"], "ws~original", signer, read_only=True), {"AccountId": "fake"}, True)
    gate.finish(
        gate.reserve(entries[1]["call"], "ws~original", signer, read_only=False),
        {"StackId": "stack-offline"},
        True,
    )
    gate.finish(
        gate.reserve(entries[3]["call"], "ws~original", signer, read_only=True),
        {
            "Code": "SignatureDoesNotMatch",
            "EC": "0002-00000201",
            "RequestId": "offline-request",
            "HostId": "offline-bucket.oss-cn-hangzhou.aliyuncs.com",
        },
        False,
        status=403,
        headers={"x-oss-request-id": "offline-request"},
    )
    gate.cleanup(path, "0", scope, "ws~original")
    deletion = copy.deepcopy(entries[2]["call"])
    deletion["params"]["StackId"] = "stack-offline"
    gate.finish(gate.reserve(deletion, "ws~original", signer, read_only=False), {"RequestId": "deleted"}, True)
    with closing(sqlite3.connect(path)) as db, db:
        if damage == "unknown_write":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='create'")
        elif damage == "missing_identity":
            db.execute("DELETE FROM identities")
        elif damage == "other_403":
            value = json.loads(db.execute("SELECT response FROM attempts WHERE grant_id='detail'").fetchone()[0])
            value["body"]["Code"] = "AccessDenied"
            db.execute("UPDATE attempts SET response=? WHERE grant_id='detail'", (json.dumps(value),))
    if damage == "outstanding":
        with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
            db.execute("UPDATE owned SET state='present'")
    path.replace(root / "execution-gate.sqlite3")
    before = (root / "execution-gate.sqlite3").read_bytes()
    if damage is None:
        validate_additional_source(root, new, proof)
    else:
        with pytest.raises(WorkflowError):
            validate_additional_source(root, new, proof)
    assert (root / "execution-gate.sqlite3").read_bytes() == before


@pytest.mark.parametrize("damage", [None, "write", "different_error", "no_request", "unfiltered", "orphan"])
def test_additional_source_only_recognizes_exact_negative_nas_read_without_rewriting(source, damage):
    root, _, new, proof = source
    scope = new["additional_source_request"]["prior_scope"]
    call = {
        "product": "nas",
        "action": "DescribeAccessGroups",
        "params": {"AccessGroupName": "fixture-group", "FileSystemType": "standard"},
    }
    response = {"Code": "InvalidAccessGroup.NotFound", "statusCode": 404, "RequestId": "fake-request"}
    if damage == "different_error":
        response["Code"] = "Forbidden"
    elif damage == "no_request":
        response["RequestId"] = ""
    elif damage == "unfiltered":
        del call["params"]["AccessGroupName"]
    with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
        if damage != "orphan":
            db.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
                (scope, "absence", "original", 2, 1, "delete" if damage == "write" else "read", json.dumps(call)),
            )
        db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (scope, "absence", "unknown", json.dumps(response)))
    original = (root / "execution-gate.sqlite3").read_bytes()
    if damage is None:
        validate_additional_source(root, new, proof)
    else:
        with pytest.raises(WorkflowError, match="unknown wire"):
            validate_additional_source(root, new, proof)
    assert (root / "execution-gate.sqlite3").read_bytes() == original


@pytest.mark.parametrize(
    "case",
    [
        "unreviewed",
        "changed_request",
        "sha",
        "calls",
        "account",
        "paid",
        "outstanding",
        "active",
        "unknown",
        "unused",
        "failed",
        "duplicate_id",
    ],
)
def test_additional_source_requires_exact_review_cleanup_and_consumed_success(source, case):
    root, old, new, proof = source
    if case == "unreviewed":
        proof["approved"] = False
    elif case == "changed_request":
        new["additional_source_request"]["request_id"] += "-changed"
    elif case == "sha":
        new["additional_source_request"]["predecessor_sha256"] = "0" * 64
        proof["additional_source_approved"] = copy.deepcopy(new["additional_source_request"])
    elif case == "calls":
        new["execution_grants"]["ROS"][0]["quota"] = 2
    elif case == "account":
        new["scenes"]["scene"]["account"] = "other"
    elif case == "paid":
        new["budget_cents"] = 1
    elif case in {"outstanding", "active"}:
        with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
            db.execute(
                "UPDATE owned SET state='present'" if case == "outstanding" else "UPDATE jobs SET state='running'"
            )
    elif case in {"unknown", "unused", "failed"}:
        with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
            db.execute(
                {
                    "unknown": "UPDATE attempts SET state='unknown'",
                    "unused": "UPDATE authority_limits SET used=0",
                    "failed": "UPDATE attempts SET state='failed'",
                }[case]
            )
    else:
        previous = copy.deepcopy(new["additional_source_request"])
        previous["remediation_sha256"] = "0" * 64
        json_write(root / "creation-authorizations/duplicate.json", {"additional_source_request": previous})
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)


@pytest.fixture
def mixed_source(source):
    root, old, new, proof = source
    carry = copy.deepcopy(old)
    carry["execution_grants"] = {
        "CREATE": copy.deepcopy(old["execution_grants"]["ROS"]),
        "QUERY": [{"id": "query", "kind": "read", "quota": 2, "call": {"action": "ExactQuery"}}],
    }
    carry["scenes"] = {f"old-{index}": {"account": "fake"} for index in range(8)}
    json_write(root / "inputs/carry.json", carry)
    approval = {
        "schema_version": 3,
        "authorized": True,
        "identity_mode": "existing_account",
        "resource_ids": ["R0015"],
        "account": "fake",
        "max_budget_cents": 0,
        "execution_sha256": execution_digest(carry),
        "lifetime": "task",
        "expires_at": None,
        "authorization_evidence": "inputs/review.json",
        "authorization_sha256": digest(root / "inputs/review.json"),
        "dimension_authorities": {"CREATE": "original-authority:CREATE", "QUERY": "original-authority:QUERY"},
    }
    json_write(authorization_path(root, carry), approval)
    new["execution_grants"].update(copy.deepcopy(carry["execution_grants"]))
    new["scenes"] = {f"new-{index}": {"account": "fake"} for index in range(6)}
    proof["carry_forward_dimensions"] = {
        dimension: {
            "proposal": "inputs/carry.json",
            "proposal_sha256": digest(root / "inputs/carry.json"),
            "execution_sha256": execution_digest(carry),
        }
        for dimension in ("CREATE", "QUERY")
    }
    return root, old, new, proof, carry


def test_mixed_additional_sample_retains_original_authorities_and_scene_count_can_change(mixed_source):
    root, old, new, proof, carry = mixed_source
    state_before = (root / "state.sqlite3").read_bytes()
    gate_before = (root / "execution-gate.sqlite3").read_bytes()
    validate_additional_source(root, new, proof)
    bindings = reviewed_authorities(root, new, proof)
    assert bindings == {
        "ROS": execution_digest(new) + ":ROS",
        "CREATE": "original-authority:CREATE",
        "QUERY": "original-authority:QUERY",
    }
    assert bindings["ROS"] != execution_digest(old) + ":ROS"
    assert (root / "state.sqlite3").read_bytes() == state_before
    assert (root / "execution-gate.sqlite3").read_bytes() == gate_before


@pytest.mark.parametrize(
    "case",
    [
        "missing_carry",
        "extra_carry",
        "supplement_carried",
        "quota",
        "calls",
        "proposal_sha",
        "execution_sha",
        "authorization",
        "carry_account",
        "mixed_account",
        "empty_account",
    ],
)
def test_mixed_source_never_grants_unreviewed_or_reset_extra_dimensions(mixed_source, case):
    root, old, new, proof, carry = mixed_source
    if case == "missing_carry":
        del proof["carry_forward_dimensions"]["QUERY"]
    elif case == "extra_carry":
        proof["carry_forward_dimensions"]["TF"] = copy.deepcopy(proof["carry_forward_dimensions"]["QUERY"])
    elif case == "supplement_carried":
        proof["carry_forward_dimensions"]["ROS"] = copy.deepcopy(proof["carry_forward_dimensions"]["QUERY"])
    elif case == "quota":
        new["execution_grants"]["CREATE"][0]["quota"] = 2
    elif case == "calls":
        new["execution_grants"]["QUERY"][0]["call"]["action"] = "WiderQuery"
    elif case in {"proposal_sha", "execution_sha"}:
        proof["carry_forward_dimensions"]["QUERY"][case + "256"] = "0" * 64
    elif case == "authorization":
        authorization_path(root, carry).unlink()
    elif case == "carry_account":
        carry["scenes"]["old-0"]["account"] = "other"
        json_write(root / "inputs/carry.json", carry)
        for binding in proof["carry_forward_dimensions"].values():
            binding["proposal_sha256"] = digest(root / "inputs/carry.json")
    elif case == "mixed_account":
        new["scenes"]["new-0"]["account"] = "other"
    else:
        new["scenes"] = {}
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)


@pytest.fixture
def multiple_source(source):
    root, old, _, _ = source
    old["execution_grants"]["CREATE"] = copy.deepcopy(old["execution_grants"]["ROS"])
    old["execution_grants"]["QUERY"] = [
        {"id": "identity", "kind": "identity", "quota": 1, "call": {"action": "GetCallerIdentity"}},
        {"id": "query", "kind": "read", "quota": 2, "call": {"action": "ExactRead"}},
    ]
    json_write(root / "inputs/old.json", old)
    approval = {
        "schema_version": 3,
        "authorized": True,
        "identity_mode": "existing_account",
        "resource_ids": ["R0015"],
        "account": "fake",
        "max_budget_cents": 0,
        "execution_sha256": execution_digest(old),
        "lifetime": "task",
        "expires_at": None,
        "authorization_evidence": "inputs/review.json",
        "authorization_sha256": digest(root / "inputs/review.json"),
    }
    json_write(authorization_path(root, old), approval)
    job = "a" * 32
    with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
        db.execute("UPDATE grants SET authority=? WHERE scope=?", (execution_digest(old) + ":ROS", job + ":ROS"))
        db.execute("UPDATE authority_limits SET authority=?", (execution_digest(old) + ":ROS",))
        for dimension in ("CREATE", "QUERY"):
            for entry in old["execution_grants"][dimension]:
                authority = execution_digest(old) + ":" + dimension
                db.execute(
                    "INSERT INTO grants(scope,id,authority,quota,used) VALUES(?,?,?,?,?)",
                    (job + ":" + dimension, entry["id"], authority, entry["quota"], 1),
                )
                db.execute("INSERT INTO authority_limits VALUES(?,?,?)", (authority, entry["id"], 1))
                db.execute(
                    "INSERT INTO attempts(scope,grant_id,state) VALUES(?,?,?)",
                    (job + ":" + dimension, entry["id"], "succeeded"),
                )
    new = copy.deepcopy(old)
    new["additional_source_requests"] = [
        {
            "request_id": "explicit-new-operation-" + dimension,
            "dimension": dimension,
            "prior_scope": job + ":" + dimension,
            "predecessor_proposal": "inputs/old.json",
            "predecessor_sha256": digest(root / "inputs/old.json"),
            "remediation_evidence": "inputs/remediation.json",
            "remediation_sha256": digest(root / "inputs/remediation.json"),
        }
        for dimension in ("ROS", "CREATE", "QUERY")
    ]
    proof = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "additional_sources_approved": copy.deepcopy(new["additional_source_requests"]),
    }
    return root, old, new, proof


def test_multiple_explicit_operations_preserve_journals_and_have_stable_individual_authorities(multiple_source):
    root, old, new, proof = multiple_source
    before = (root / "execution-gate.sqlite3").read_bytes()
    first = reviewed_authorities(root, new, proof)
    assert set(first) == {"ROS", "CREATE", "QUERY"}
    assert all(first[d] != execution_digest(old) + ":" + d for d in first)
    new["additional_source_requests"].reverse()
    proof["additional_sources_approved"].reverse()
    assert reviewed_authorities(root, new, proof) == first
    assert (root / "execution-gate.sqlite3").read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    ["unreviewed", "omit", "duplicate", "legacy", "query_write", "changed_read", "unknown", "unused", "foreign"],
)
def test_multiple_operations_never_implicitly_refill_unaudited_dimensions(multiple_source, damage):
    root, old, new, proof = multiple_source
    if damage == "unreviewed":
        proof["approved"] = False
    elif damage == "omit":
        new["additional_source_requests"].pop()
        proof["additional_sources_approved"] = copy.deepcopy(new["additional_source_requests"])
    elif damage == "duplicate":
        new["additional_source_requests"].append(copy.deepcopy(new["additional_source_requests"][0]))
        proof["additional_sources_approved"] = copy.deepcopy(new["additional_source_requests"])
    elif damage == "legacy":
        new["additional_source_request"] = copy.deepcopy(new["additional_source_requests"][0])
    elif damage == "query_write":
        new["execution_grants"]["QUERY"][0]["kind"] = "create"
    elif damage == "changed_read":
        new["execution_grants"]["QUERY"][1]["quota"] = 20
    elif damage in {"unknown", "unused"}:
        with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
            db.execute(
                "UPDATE attempts SET state='unknown' WHERE scope LIKE '%:QUERY'"
                if damage == "unknown"
                else "UPDATE grants SET used=0 WHERE scope LIKE '%:QUERY'"
            )
    else:
        new["scenes"]["scene"]["account"] = "foreign"
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)


@pytest.fixture
def reduced_query(source):
    root, old, _, _ = source
    old["execution_grants"]["QUERY"] = [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 2,
            "call": {"product": "sts", "action": "GetCallerIdentity", "params": {}},
        },
        {
            "id": "query",
            "kind": "read",
            "quota": 1,
            "call": {"product": "oss", "action": "ListBuckets", "params": {"prefix": "fake", "max-keys": 1}},
        },
    ]
    json_write(root / "inputs/old.json", old)
    approval = {
        "schema_version": 3,
        "authorized": True,
        "identity_mode": "existing_account",
        "resource_ids": ["R0015"],
        "account": "fake",
        "max_budget_cents": 0,
        "execution_sha256": execution_digest(old),
        "lifetime": "task",
        "expires_at": None,
        "authorization_evidence": "inputs/review.json",
        "authorization_sha256": digest(root / "inputs/review.json"),
    }
    json_write(authorization_path(root, old), approval)
    with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", ("b" * 32, old["task_id"], "glm", "complete", "first-turn"))
    authority = execution_digest(old) + ":QUERY"
    with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
        for entry in old["execution_grants"]["QUERY"]:
            db.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
                (
                    "a" * 32 + ":QUERY",
                    entry["id"],
                    authority,
                    entry["quota"],
                    1,
                    entry["kind"],
                    canonical(entry["call"]),
                ),
            )
            db.execute("INSERT INTO authority_limits VALUES(?,?,?)", (authority, entry["id"], entry["quota"]))
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?)",
                (
                    "a" * 32 + ":QUERY",
                    entry["id"],
                    "succeeded",
                    json.dumps({"AccountId": "fake"}),
                ),
            )
        identity = old["execution_grants"]["QUERY"][0]
        db.execute(
            "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
            (
                "b" * 32 + ":QUERY",
                "identity",
                authority,
                1,
                1,
                "identity",
                canonical(identity["call"]),
            ),
        )
        db.execute(
            "INSERT INTO attempts VALUES(?,?,?,?)",
            (
                "b" * 32 + ":QUERY",
                "identity",
                "succeeded",
                json.dumps({"AccountId": "fake"}),
            ),
        )
    new = copy.deepcopy(old)
    del new["execution_grants"]["ROS"]
    new["execution_grants"]["QUERY"][0]["quota"] = 1
    new["additional_source_requests"] = [
        {
            "request_id": "explicit-query-after-continued-identity",
            "dimension": "QUERY",
            "prior_scope": "a" * 32 + ":QUERY",
            "predecessor_proposal": "inputs/old.json",
            "predecessor_sha256": digest(root / "inputs/old.json"),
            "remediation_evidence": "inputs/remediation.json",
            "remediation_sha256": digest(root / "inputs/remediation.json"),
            "query_identity_quota_reduction": {"grant_id": "identity", "previous_quota": 2, "new_quota": 1},
        }
    ]
    proof = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "additional_sources_approved": copy.deepcopy(new["additional_source_requests"]),
    }
    return root, old, new, proof


def test_explicit_query_identity_reduction_preserves_all_original_quotas_and_attempts(reduced_query):
    root, old, new, proof = reduced_query
    before = (root / "execution-gate.sqlite3").read_bytes()
    authorities = reviewed_authorities(root, new, proof)
    assert authorities["QUERY"] != execution_digest(old) + ":QUERY"
    assert new["execution_grants"]["QUERY"][0]["quota"] == 1
    assert old["execution_grants"]["QUERY"][0]["quota"] == 2
    assert (root / "execution-gate.sqlite3").read_bytes() == before
    assert digest(root / "inputs/old.json") == new["additional_source_requests"][0]["predecessor_sha256"]


@pytest.mark.parametrize(
    "damage",
    [
        "no_review",
        "no_reduction",
        "wrong_id",
        "wrong_previous",
        "wrong_new",
        "bool_new",
        "extra_field",
        "write_dimension",
        "changed_call",
        "changed_read_quota",
        "new_identity_two",
        "wrong_product",
        "unconsumed_limit",
        "bad_sum",
        "wrong_local_quota",
        "wrong_local_call",
        "wrong_read_call",
        "unknown",
        "foreign_account",
        "invalid_response",
        "foreign_task",
        "active_turn",
        "missing_turn",
        "missing_attempt",
        "wrong_dimension",
        "orphan_attempt",
    ],
)
def test_query_identity_reduction_is_exact_readonly_review_not_quota_reset(reduced_query, damage):
    root, old, new, proof = reduced_query
    request = new["additional_source_requests"][0]
    reduction = request["query_identity_quota_reduction"]
    if damage == "no_review":
        proof["approved"] = False
    elif damage == "no_reduction":
        del request["query_identity_quota_reduction"]
    elif damage in {"wrong_id", "wrong_previous", "wrong_new", "bool_new", "extra_field"}:
        key, value = {
            "wrong_id": ("grant_id", "query"),
            "wrong_previous": ("previous_quota", 3),
            "wrong_new": ("new_quota", 2),
            "bool_new": ("new_quota", True),
            "extra_field": ("unreviewed", True),
        }[damage]
        reduction[key] = value
    elif damage == "write_dimension":
        request["dimension"] = "ROS"
    elif damage == "changed_call":
        new["execution_grants"]["QUERY"][1]["call"]["params"]["prefix"] = "other"
    elif damage == "changed_read_quota":
        new["execution_grants"]["QUERY"][1]["quota"] = 2
    elif damage == "new_identity_two":
        new["execution_grants"]["QUERY"][0]["quota"] = 2
    elif damage == "wrong_product":
        new["execution_grants"]["QUERY"][0]["call"]["product"] = "other"
    elif damage in {"foreign_task", "active_turn", "missing_turn"}:
        field, value = {
            "foreign_task": ("task_id", "foreign:R0015"),
            "active_turn": ("state", "running"),
            "missing_turn": ("turn_id", None),
        }[damage]
        with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
            db.execute(f"UPDATE jobs SET {field}=? WHERE id=?", (value, "b" * 32))
    else:
        with closing(sqlite3.connect(root / "execution-gate.sqlite3")) as db, db:
            scope = "b" * 32 + ":QUERY"
            if damage == "unconsumed_limit":
                db.execute("UPDATE authority_limits SET used=1 WHERE id='identity'")
            elif damage == "bad_sum":
                db.execute("UPDATE grants SET used=0 WHERE scope=?", (scope,))
            elif damage == "wrong_local_quota":
                db.execute("UPDATE grants SET quota=3 WHERE scope=?", (scope,))
            elif damage in {"wrong_local_call", "wrong_read_call"}:
                db.execute(
                    "UPDATE grants SET call='{}' WHERE scope=? AND id=?",
                    (
                        scope if damage == "wrong_local_call" else "a" * 32 + ":QUERY",
                        "identity" if damage == "wrong_local_call" else "query",
                    ),
                )
            elif damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown' WHERE scope=?", (scope,))
            elif damage in {"foreign_account", "invalid_response"}:
                db.execute(
                    "UPDATE attempts SET response=? WHERE scope=?",
                    (
                        json.dumps({"AccountId": "other"}) if damage == "foreign_account" else "invalid",
                        scope,
                    ),
                )
            elif damage == "missing_attempt":
                db.execute("DELETE FROM attempts WHERE scope=?", (scope,))
            elif damage == "wrong_dimension":
                db.execute("UPDATE grants SET scope=? WHERE scope=?", ("b" * 32 + ":CREATE", scope))
            elif damage == "orphan_attempt":
                db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (scope, "missing", "unknown", "{}"))
    if damage != "no_review":
        proof["additional_sources_approved"] = copy.deepcopy(new["additional_source_requests"])
    before = (root / "execution-gate.sqlite3").read_bytes()
    with pytest.raises(WorkflowError):
        validate_additional_source(root, new, proof)
    assert (root / "execution-gate.sqlite3").read_bytes() == before
