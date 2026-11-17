"""Supplement one source without replenishing unchanged dimensions' quotas."""

import json
import sqlite3
from copy import deepcopy

import pytest

from scripts.web.e2e.resource_link_workflow.creation import (
    authorization_path,
    execution_authority,
    execution_digest,
    reviewed_authorities,
)
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write


@pytest.fixture
def approved(tmp_path):
    old = {
        "task_id": "plan:R0001",
        "budget_cents": 0,
        "scenes": {"s": {"account": "fake-account"}},
        "cleanup_recipe": {"TF": "original owned only"},
        "execution_grants": {
            "ROS": [{"id": "create", "quota": 1}],
            "TF": [{"id": "create", "quota": 1}],
            "QUERY": [{"id": "read", "quota": 1}],
        },
    }
    evidence = tmp_path / "inputs/review.json"
    json_write(evidence, {"approved": True})
    path = tmp_path / "inputs/old.json"
    json_write(path, old)
    json_write(
        authorization_path(tmp_path, old),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0001"],
            "account": "fake-account",
            "max_budget_cents": 0,
            "execution_sha256": execution_digest(old),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/review.json",
            "authorization_sha256": digest(evidence),
        },
    )
    new = deepcopy(old)
    new["execution_grants"]["ROS"][0]["new_source"] = "independently-reviewed"
    proof = {
        "carry_forward_dimensions": {
            dimension: {
                "proposal": "inputs/old.json",
                "proposal_sha256": digest(path),
                "execution_sha256": execution_digest(old),
            }
            for dimension in ("TF", "QUERY")
        }
    }
    return tmp_path, old, new, proof


@pytest.mark.parametrize("dimension", ["TF", "QUERY"])
def test_unchanged_dimension_uses_original_cumulative_key(approved, dimension):
    root, old, new, proof = approved
    bindings = reviewed_authorities(root, new, proof)
    assert bindings[dimension] == f"{execution_digest(old)}:{dimension}"
    assert bindings["ROS"] == f"{execution_digest(new)}:ROS"
    json_write(authorization_path(root, new), {"dimension_authorities": bindings})
    assert execution_authority(root, new, dimension) == bindings[dimension]
    assert execution_authority(root, new, "ROS") == bindings["ROS"]


@pytest.mark.parametrize("change", ["quota", "template", "task", "account", "sha"])
def test_changed_original_entitlement_rejected(approved, change):
    root, old, new, proof = approved
    if change == "quota":
        new["execution_grants"]["TF"][0]["quota"] = 2
    elif change == "template":
        new["execution_grants"]["TF"][0]["call"] = {"template": "different"}
    elif change == "task":
        new["task_id"] = "plan:R0002"
    elif change == "account":
        new["scenes"]["s"]["account"] = "other-account"
    else:
        proof["carry_forward_dimensions"]["TF"]["proposal_sha256"] = "0" * 64
    with pytest.raises(WorkflowError):
        reviewed_authorities(root, new, proof)


def test_existing_authorization_keeps_old_default_key(approved):
    root, old, _, _ = approved
    assert execution_authority(root, old, "TF") == f"{execution_digest(old)}:TF"


@pytest.mark.parametrize("dimension", ["TF", "QUERY"])
def test_install_does_not_copy_or_reset_consumed_authority(approved, dimension):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize, install

    root, old, new, proof = approved
    authority = reviewed_authorities(root, new, proof)[dimension]
    gate_path = root / "gate.sqlite3"
    initialize(gate_path)
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
        }
    ]
    install(gate_path, "0", f"original:{dimension}", "ws~old", "fake-account", None, entries, authority=authority)
    with sqlite3.connect(gate_path) as db:
        db.execute("UPDATE authority_limits SET used=1 WHERE authority=?", (authority,))
    install(gate_path, "0", f"next:{dimension}", "ws~next", "fake-account", None, entries, authority=authority)
    with sqlite3.connect(gate_path) as db:
        assert db.execute("SELECT authority,used FROM authority_limits").fetchall() == [(authority, 1)]
        assert db.execute("SELECT authority FROM grants WHERE scope=?", (f"next:{dimension}",)).fetchone()[0] == (
            f"{execution_digest(old)}:{dimension}"
        )


def test_readonly_query_without_creation_authorization_keeps_default_key(approved):
    root, _, new, _ = approved
    assert not authorization_path(root, new).exists()
    assert execution_authority(root, new, "QUERY") == f"{execution_digest(new)}:QUERY"


@pytest.fixture
def identity_continuation(tmp_path):
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, initialize, install

    old = {
        "task_id": "plan:R0018",
        "budget_cents": 0,
        "scenes": {"s": {"account": "fake"}},
        "cleanup_recipe": {"CREATE": "original response-bound deletion"},
        "execution_grants": {
            "CREATE": [
                {
                    "id": "identity",
                    "kind": "identity",
                    "quota": 1,
                    "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
                },
                {
                    "id": "read",
                    "kind": "read",
                    "quota": 2,
                    "call": {
                        "product": "ecs",
                        "action": "DescribeSecurityGroups",
                        "region_id": "cn-hangzhou",
                        "params": {"SecurityGroupName": "fixture-group", "PageSize": 10},
                    },
                },
                {
                    "id": "create",
                    "kind": "create",
                    "quota": 1,
                    "call": {
                        "product": "ecs",
                        "action": "CreateSecurityGroup",
                        "region_id": "cn-hangzhou",
                        "params": {"SecurityGroupName": "fixture-group"},
                    },
                },
                {
                    "id": "delete",
                    "kind": "delete",
                    "quota": 1,
                    "parent": "create",
                    "response_path": ["SecurityGroupId"],
                    "target_param": "SecurityGroupId",
                    "call": {
                        "product": "ecs",
                        "action": "DeleteSecurityGroup",
                        "region_id": "cn-hangzhou",
                        "params": {"SecurityGroupId": "$created_id"},
                    },
                },
            ]
        },
    }
    json_write(tmp_path / "inputs/old.json", old)
    json_write(tmp_path / "inputs/original-review.json", {"approved": True})
    json_write(tmp_path / "inputs/repair.json", {"diagnosis": "offline identity-only fixture"})
    json_write(
        authorization_path(tmp_path, old),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0018"],
            "account": "fake",
            "max_budget_cents": 0,
            "execution_sha256": execution_digest(old),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/original-review.json",
            "authorization_sha256": digest(tmp_path / "inputs/original-review.json"),
        },
    )
    scope = "a" * 32 + ":CREATE"
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        db.executescript("CREATE TABLE jobs(id,task_id,role,state,turn_id);CREATE TABLE owned(task_id,state);")
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", ("a" * 32, old["task_id"], "glm", "complete", "turn"))
    initialize(tmp_path / "execution-gate.sqlite3")
    authority = execution_authority(tmp_path, old, "CREATE")
    install(
        tmp_path / "execution-gate.sqlite3",
        "0",
        scope,
        "ws~old",
        "fake",
        None,
        old["execution_grants"]["CREATE"],
        authority=authority,
    )
    with sqlite3.connect(tmp_path / "execution-gate.sqlite3") as db:
        db.execute("UPDATE grants SET used=1 WHERE id='identity'")
        db.execute("UPDATE authority_limits SET used=1 WHERE id='identity'")
        db.execute(
            "INSERT INTO attempts(id,scope,grant_id,state,response) VALUES(?,?,?,?,?)",
            ("original", scope, "identity", "succeeded", canonical({"AccountId": "fake"})),
        )
    new = deepcopy(old)
    new["execution_grants"]["CREATE"][0]["quota"] = 2
    proof = {
        "kind": "ops_identity_continuation_review",
        "approved": True,
        "proposal_execution_sha256": execution_digest(new),
        "carry_forward_dimensions": {
            "CREATE": {
                "proposal": "inputs/old.json",
                "proposal_sha256": digest(tmp_path / "inputs/old.json"),
                "execution_sha256": execution_digest(old),
                "identity_continuation": {
                    "request_id": "identity-repair-001",
                    "prior_scope": scope,
                    "remediation_evidence": "inputs/repair.json",
                    "remediation_sha256": digest(tmp_path / "inputs/repair.json"),
                },
            }
        },
    }
    return tmp_path, old, new, proof


@pytest.fixture
def split_identity_continuation(identity_continuation):
    """Two successful identities on the same authority, one in each terminal scope."""
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, install

    root, old, new, proof = identity_continuation
    authority = execution_authority(root, old, "CREATE")
    old["execution_grants"]["CREATE"][0]["quota"] = 2
    json_write(root / "inputs/old.json", old)
    approval = {
        "schema_version": 3,
        "authorized": True,
        "identity_mode": "existing_account",
        "resource_ids": ["R0018"],
        "account": "fake",
        "max_budget_cents": 0,
        "execution_sha256": execution_digest(old),
        "lifetime": "task",
        "expires_at": None,
        "dimension_authorities": {"CREATE": authority},
        "authorization_evidence": "inputs/original-review.json",
        "authorization_sha256": digest(root / "inputs/original-review.json"),
    }
    json_write(authorization_path(root, old), approval)
    install(
        root / "execution-gate.sqlite3",
        "1",
        "b" * 32 + ":CREATE",
        "ws~second",
        "fake",
        None,
        old["execution_grants"]["CREATE"],
        authority=authority,
    )
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", ("b" * 32, old["task_id"], "glm", "complete", "turn2"))
    with sqlite3.connect(root / "execution-gate.sqlite3") as db:
        db.execute("UPDATE grants SET used=1 WHERE scope=? AND id='identity'", ("b" * 32 + ":CREATE",))
        db.execute("UPDATE authority_limits SET used=2 WHERE authority=? AND id='identity'", (authority,))
        db.execute(
            "INSERT INTO attempts(id,scope,grant_id,state,response) VALUES(?,?,?,?,?)",
            ("second", "b" * 32 + ":CREATE", "identity", "succeeded", canonical({"AccountId": "fake"})),
        )
    new = deepcopy(old)
    new["execution_grants"]["CREATE"][0]["quota"] = 3
    proof["proposal_execution_sha256"] = execution_digest(new)
    source = proof["carry_forward_dimensions"]["CREATE"]
    source["proposal_sha256"] = digest(root / "inputs/old.json")
    source["execution_sha256"] = execution_digest(old)
    source["identity_continuation"]["prior_scope"] = "b" * 32 + ":CREATE"
    return root, old, new, proof, authority


def test_identity_continuation_checks_cumulative_terminal_scopes_without_reset(split_identity_continuation):
    root, old, new, proof, authority = split_identity_continuation
    before = (root / "execution-gate.sqlite3").read_bytes()
    assert reviewed_authorities(root, new, proof)["CREATE"] == authority
    assert (root / "execution-gate.sqlite3").read_bytes() == before


@pytest.mark.parametrize("damage", ["active", "other-task", "wrong-call", "unknown", "wrong-account", "count", "quota"])
def test_identity_continuation_rejects_damaged_previous_cumulative_scope(split_identity_continuation, damage):
    root, old, new, proof, authority = split_identity_continuation
    if damage in {"active", "other-task"}:
        with sqlite3.connect(root / "state.sqlite3") as db:
            if damage == "active":
                db.execute("UPDATE jobs SET state='running' WHERE id=?", ("a" * 32,))
            else:
                db.execute("UPDATE jobs SET task_id='another:R0018' WHERE id=?", ("a" * 32,))
    else:
        with sqlite3.connect(root / "execution-gate.sqlite3") as db:
            if damage == "wrong-call":
                db.execute("UPDATE grants SET call='{}' WHERE scope=? AND id='identity'", ("a" * 32 + ":CREATE",))
            elif damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown' WHERE id='original'")
            elif damage == "wrong-account":
                db.execute("UPDATE attempts SET response=? WHERE id='original'", ('{"AccountId":"other"}',))
            elif damage == "count":
                db.execute("UPDATE grants SET used=0 WHERE scope=? AND id='identity'", ("a" * 32 + ":CREATE",))
            else:
                db.execute("UPDATE grants SET quota=4 WHERE scope=? AND id='identity'", ("a" * 32 + ":CREATE",))
    with pytest.raises(WorkflowError):
        reviewed_authorities(root, new, proof)


def test_identity_review_preserves_original_write_and_identity_counts(identity_continuation):
    from scripts.web.e2e.resource_link_workflow.execution_gate import install

    root, old, new, proof = identity_continuation
    original = (root / "execution-gate.sqlite3").read_bytes()
    authority = reviewed_authorities(root, new, proof)["CREATE"]
    assert authority == execution_authority(root, old, "CREATE")
    assert (root / "execution-gate.sqlite3").read_bytes() == original
    install(
        root / "execution-gate.sqlite3",
        "0",
        "b" * 32 + ":CREATE",
        "ws~new",
        "fake",
        None,
        new["execution_grants"]["CREATE"],
        authority=authority,
    )
    with sqlite3.connect(root / "execution-gate.sqlite3") as db:
        assert db.execute("SELECT id,used FROM authority_limits ORDER BY id").fetchall() == [
            ("create", 0),
            ("delete", 0),
            ("identity", 1),
            ("read", 0),
        ]
        assert db.execute(
            "SELECT quota FROM grants WHERE scope=? AND id='identity'", ("b" * 32 + ":CREATE",)
        ).fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM identities").fetchone() == (0,)


@pytest.fixture
def read_continuation(identity_continuation):
    """A new signed session needs the same bounded precheck, not more writes."""
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

    root, old, new, proof = identity_continuation
    source = proof["carry_forward_dimensions"]["CREATE"]
    request = source.pop("identity_continuation")
    request["grant_ids"] = ["identity", "read"]
    source["read_continuation"] = request
    proof.update(kind="ops_read_continuation_review", read_continuation_approved=deepcopy(request))
    new["execution_grants"]["CREATE"][1]["quota"] = 3
    proof["proposal_execution_sha256"] = execution_digest(new)
    with sqlite3.connect(root / "execution-gate.sqlite3") as db:
        db.execute("UPDATE grants SET used=2 WHERE id='read'")
        db.execute("UPDATE authority_limits SET used=2 WHERE id='read'")
        for number in range(2):
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,state,response) VALUES(?,?,?,?,?)",
                (f"read-{number}", request["prior_scope"], "read", "succeeded", canonical({"TotalCount": 0})),
            )
    return root, old, new, proof


def test_explicit_read_continuation_preserves_all_original_calls_counts_and_writes(read_continuation):
    from scripts.web.e2e.resource_link_workflow.execution_gate import install

    root, old, new, proof = read_continuation
    before = (root / "execution-gate.sqlite3").read_bytes()
    authority = reviewed_authorities(root, new, proof)["CREATE"]
    assert authority == execution_authority(root, old, "CREATE")
    assert (root / "execution-gate.sqlite3").read_bytes() == before
    install(
        root / "execution-gate.sqlite3",
        "0",
        "b" * 32 + ":CREATE",
        "ws~new",
        "fake",
        None,
        new["execution_grants"]["CREATE"],
        authority=authority,
    )
    with sqlite3.connect(root / "execution-gate.sqlite3") as db:
        assert db.execute("SELECT id,used FROM authority_limits ORDER BY id").fetchall() == [
            ("create", 0),
            ("delete", 0),
            ("identity", 1),
            ("read", 2),
        ]
        assert db.execute(
            "SELECT id,quota FROM grants WHERE scope=? ORDER BY id", ("b" * 32 + ":CREATE",)
        ).fetchall() == [
            ("create", 1),
            ("delete", 1),
            ("identity", 2),
            ("read", 3),
        ]
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (3,)


@pytest.fixture(params=["identity_continuation", "read_continuation"])
def installed_continuation(request):
    """A native-reviewed successor, not a new OPS admission after consuming calls."""
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, install

    root, old, new, proof = request.getfixturevalue(request.param)
    new["execution_selection"] = {"dimensions": ["CREATE"], "deferred_scenes": {}}
    proof.update(
        task_id=new["task_id"],
        execution_selection_approved=deepcopy(new["execution_selection"]),
        proposal_execution_sha256=execution_digest(new),
    )
    json_write(root / "inputs/continued.json", new)
    proof["proposal_sha256"] = digest(root / "inputs/continued.json")
    authority = reviewed_authorities(root, new, proof)["CREATE"]
    json_write(root / "inputs/continued-review.json", proof)
    approval = {
        "schema_version": 3,
        "authorized": True,
        "identity_mode": "existing_account",
        "resource_ids": ["R0018"],
        "task_id": new["task_id"],
        "account": "fake",
        "max_budget_cents": 0,
        "execution_sha256": execution_digest(new),
        "lifetime": "task",
        "expires_at": None,
        "authorization_evidence": "inputs/continued-review.json",
        "authorization_sha256": digest(root / "inputs/continued-review.json"),
        "proposal_evidence": "inputs/continued.json",
        "proposal_sha256": digest(root / "inputs/continued.json"),
        "dimension_authorities": {"CREATE": authority},
    }
    json_write(authorization_path(root, new), approval)
    scope = "b" * 32 + ":CREATE"
    install(
        root / "execution-gate.sqlite3",
        "0",
        scope,
        "ws~successor",
        "fake",
        None,
        new["execution_grants"]["CREATE"],
        authority=authority,
    )
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", ("b" * 32, new["task_id"], "glm", "running", "next-turn"))
        db.execute("INSERT INTO owned VALUES(?,?)", (new["task_id"], "present"))
    with sqlite3.connect(root / "execution-gate.sqlite3") as db:
        for identifier, response in [("identity", {"AccountId": "fake"}), ("create", {"SecurityGroupId": "offline"})]:
            db.execute("UPDATE grants SET used=1 WHERE scope=? AND id=?", (scope, identifier))
            db.execute("UPDATE authority_limits SET used=used+1 WHERE authority=? AND id=?", (authority, identifier))
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,state,response) VALUES(?,?,?,?,?)",
                ("next-" + identifier, scope, identifier, "succeeded", canonical(response)),
            )
    return root, old, new, proof, approval


def test_frozen_continuation_authorization_survives_live_successor_and_owned_cleanup(installed_continuation):
    from scripts.web.e2e.resource_link_workflow.creation import validate_authorization

    root, _, packet, proof, _ = installed_continuation
    before = (root / "execution-gate.sqlite3").read_bytes()
    validate_authorization(root, "R0018", packet)
    # A second operation review must STILL refuse the now-consumed allowance.
    with pytest.raises(WorkflowError):
        reviewed_authorities(root, packet, proof)
    assert (root / "execution-gate.sqlite3").read_bytes() == before


@pytest.mark.parametrize("damage", ["review", "original", "remedy", "proposal", "authority", "account", "quota"])
def test_frozen_continuation_runtime_rejects_immutable_binding_changes(installed_continuation, damage):
    from scripts.web.e2e.resource_link_workflow.creation import validate_authorization

    root, _, packet, proof, approval = installed_continuation
    if damage in {"review", "original", "remedy", "proposal"}:
        path = {
            "review": "continued-review.json",
            "original": "old.json",
            "remedy": "repair.json",
            "proposal": "continued.json",
        }[damage]
        json_write(root / "inputs" / path, {"tampered": True})
    elif damage == "authority":
        approval["dimension_authorities"]["CREATE"] = "wrong:CREATE"
        json_write(authorization_path(root, packet), approval)
    elif damage == "account":
        packet["scenes"]["s"]["account"] = "other"
    else:
        packet["execution_grants"]["CREATE"][2]["quota"] = 2
    with pytest.raises(WorkflowError):
        validate_authorization(root, "R0018", packet)


@pytest.mark.parametrize(
    "damage",
    [
        "unselected",
        "duplicate",
        "missing-identity",
        "write-selected",
        "unknown-selected",
        "unknown-unselected",
        "used-write",
        "not-exhausted",
        "count",
        "quota",
        "params",
        "request-binding",
    ],
)
def test_read_continuation_rejects_inexact_or_unknown_operations(read_continuation, damage):
    root, old, new, proof = read_continuation
    request = proof["carry_forward_dimensions"]["CREATE"]["read_continuation"]
    if damage == "unselected":
        request["grant_ids"] = ["identity", "not-original"]
    elif damage == "duplicate":
        request["grant_ids"].append("read")
    elif damage == "missing-identity":
        request["grant_ids"] = ["read"]
    elif damage == "write-selected":
        request["grant_ids"].append("create")
    elif damage == "quota":
        new["execution_grants"]["CREATE"][1]["quota"] = 4
    elif damage == "params":
        new["execution_grants"]["CREATE"][1]["call"]["params"]["PageSize"] = 100
    elif damage == "request-binding":
        proof["read_continuation_approved"] = {}
    else:
        with sqlite3.connect(root / "execution-gate.sqlite3") as db:
            if damage == "unknown-selected":
                db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='read'")
            elif damage == "unknown-unselected":
                db.execute(
                    "INSERT INTO attempts(id,scope,grant_id,state,response) VALUES(?,?,?,?,?)",
                    ("orphan", request["prior_scope"], "missing", "unknown", "{}"),
                )
            elif damage == "used-write":
                db.execute("UPDATE grants SET used=1 WHERE id='create'")
                db.execute("UPDATE authority_limits SET used=1 WHERE id='create'")
            elif damage == "not-exhausted":
                db.execute("UPDATE grants SET used=1 WHERE id='read'")
                db.execute("UPDATE authority_limits SET used=1 WHERE id='read'")
                db.execute("DELETE FROM attempts WHERE id='read-1'")
            else:
                db.execute("DELETE FROM attempts WHERE id='read-1'")
    if damage != "request-binding":
        proof["read_continuation_approved"] = deepcopy(request)
    proof["proposal_execution_sha256"] = execution_digest(new)
    with pytest.raises(WorkflowError):
        reviewed_authorities(root, new, proof)


def _rebind_continuation_old(root, old, proof):
    """Offline fixture only: retain original authority across reviewed input SHA."""
    approval = json.loads(
        authorization_path(root, json.loads((root / "inputs/old.json").read_text(encoding="utf-8"))).read_text(
            encoding="utf-8"
        )
    )
    authority = approval.get("dimension_authorities", {}).get("CREATE", approval["execution_sha256"] + ":CREATE")
    json_write(root / "inputs/old.json", old)
    approval.update(
        execution_sha256=execution_digest(old),
        max_budget_cents=old["budget_cents"],
        dimension_authorities={"CREATE": authority},
    )
    json_write(authorization_path(root, old), approval)
    source = proof["carry_forward_dimensions"]["CREATE"]
    source.update(proposal_sha256=digest(root / "inputs/old.json"), execution_sha256=execution_digest(old))


@pytest.mark.parametrize("negative", [False, True])
def test_read_continuation_exact_oss_negative_preserves_original_unknown(read_continuation, negative):
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

    root, old, new, proof = read_continuation
    call = {
        "product": "oss",
        "action": "GetBucketInfo",
        "version": "2019-05-17",
        "region_id": "cn-hangzhou",
        "params": {"bucket": "fixture-bucket"},
    }
    old["execution_grants"]["CREATE"][1]["call"] = call
    new["execution_grants"]["CREATE"][1]["call"] = deepcopy(call)
    _rebind_continuation_old(root, old, proof)
    proof["proposal_execution_sha256"] = execution_digest(new)
    response = {
        "status": 404 if negative else 500,
        "headers": {"x-oss-request-id": "req-fixture"},
        "body": {"Code": "NoSuchBucket", "BucketName": "fixture-bucket", "RequestId": "req-fixture"},
    }
    with sqlite3.connect(root / "execution-gate.sqlite3") as db:
        db.execute("UPDATE grants SET call=? WHERE id='read'", (canonical(call),))
        db.execute("UPDATE attempts SET state='unknown',response=? WHERE id='read-0'", (canonical(response),))
    before = (root / "execution-gate.sqlite3").read_bytes()
    if negative:
        assert reviewed_authorities(root, new, proof)["CREATE"] == execution_authority(root, old, "CREATE")
    else:
        with pytest.raises(WorkflowError):
            reviewed_authorities(root, new, proof)
    assert (root / "execution-gate.sqlite3").read_bytes() == before


@pytest.mark.parametrize("damage", [None, "missing-fee", "wrong-execution", "human-cap", "official-sha"])
def test_paid_read_continuation_needs_exact_existing_human_and_cumulative_review(read_continuation, damage):
    root, old, new, proof = read_continuation
    old["budget_cents"] = new["budget_cents"] = 100
    _rebind_continuation_old(root, old, proof)
    proof["proposal_execution_sha256"] = execution_digest(new)
    json_write(
        root / "inputs/human.json",
        {
            "kind": "human_fee_authorization",
            "approved": True,
            "task_id": old["task_id"],
            "plan_id": "plan",
            "verbatim_user_message": "fixture paid permission",
            "max_budget_cents": 99 if damage == "human-cap" else 100,
        },
    )
    json_write(root / "inputs/official.json", {"fixture": "bounded paid API and empty object storage cost"})
    json_write(
        root / "inputs/cost.json",
        {
            "kind": "ops_cumulative_fee_review",
            "approved": True,
            "task_id": old["task_id"],
            "account": "fake",
            "execution_sha256": execution_digest(new),
            "additional_operations": [],
            "human_authorization": "inputs/human.json",
            "human_sha256": digest(root / "inputs/human.json"),
            "budget_cents": 100,
            "includes_previous_operations": True,
            "estimate_basis": "fixture exact cumulative estimate",
            "limitations": "not actual billing or a cloud cap",
            "official_sources": [
                {
                    "path": "inputs/official.json",
                    "sha256": "0" * 64 if damage == "official-sha" else digest(root / "inputs/official.json"),
                }
            ],
        },
    )
    proof["paid_operation_review"] = {
        "human_authorization": "inputs/human.json",
        "human_sha256": digest(root / "inputs/human.json"),
        "cost_evidence": "inputs/cost.json",
        "cost_sha256": digest(root / "inputs/cost.json"),
    }
    if damage == "missing-fee":
        proof.pop("paid_operation_review")
    elif damage == "wrong-execution":
        cost = json.loads((root / "inputs/cost.json").read_text(encoding="utf-8"))
        cost["execution_sha256"] = "0" * 64
        json_write(root / "inputs/cost.json", cost)
        proof["paid_operation_review"]["cost_sha256"] = digest(root / "inputs/cost.json")
    if damage:
        with pytest.raises(WorkflowError):
            reviewed_authorities(root, new, proof)
    else:
        before = (root / "execution-gate.sqlite3").read_bytes()
        assert reviewed_authorities(root, new, proof)["CREATE"] == execution_authority(root, old, "CREATE")
        assert (root / "execution-gate.sqlite3").read_bytes() == before


def test_composed_read_review_binds_each_dimension_separately(read_continuation):
    root, old, new, direct = read_continuation
    request = direct["carry_forward_dimensions"]["CREATE"]["read_continuation"]
    proof = {
        "kind": "ops_read_continuations_review",
        "approved": True,
        "proposal_sha256": "a" * 64,
        "carry_forward_dimensions": direct["carry_forward_dimensions"],
        "read_continuation_reviews": {
            "CREATE": {
                **direct,
                "dimension": "CREATE",
                "proposal_sha256": "a" * 64,
                "read_continuation_approved": request,
            }
        },
    }
    assert reviewed_authorities(root, new, proof)["CREATE"] == execution_authority(root, old, "CREATE")
    proof["read_continuation_reviews"]["CREATE"]["proposal_sha256"] = "b" * 64
    with pytest.raises(WorkflowError):
        reviewed_authorities(root, new, proof)


@pytest.mark.parametrize(
    "damage",
    [
        "write-quota",
        "read-quota",
        "write-name",
        "identity-call",
        "identity-plus-two",
        "missing-review",
        "wrong-execution",
        "remediation",
        "scope",
        "account",
        "active",
        "outstanding",
        "unknown",
        "used-write",
        "not-exhausted",
        "wrong-signed-account",
        "used-after-review",
        "reused-id",
    ],
)
def test_identity_continuation_rejects_expansion_or_uncertainty(identity_continuation, damage):
    root, old, new, proof = identity_continuation
    entries = new["execution_grants"]["CREATE"]
    source = proof["carry_forward_dimensions"]["CREATE"]
    request = source["identity_continuation"]
    if damage == "write-quota":
        entries[2]["quota"] = 2
    elif damage == "read-quota":
        entries[1]["quota"] = 3
    elif damage == "write-name":
        entries[2]["call"]["params"]["SecurityGroupName"] = "other"
    elif damage == "identity-call":
        entries[0]["call"]["region_id"] = "cn-shanghai"
    elif damage == "identity-plus-two":
        entries[0]["quota"] = 3
    elif damage == "missing-review":
        proof["approved"] = False
    elif damage == "wrong-execution":
        proof["proposal_execution_sha256"] = "0" * 64
    elif damage == "remediation":
        request["remediation_sha256"] = "0" * 64
    elif damage == "scope":
        request["prior_scope"] = "b" * 32 + ":CREATE"
    elif damage == "account":
        new["scenes"]["s"]["account"] = "other"
    elif damage == "active":
        with sqlite3.connect(root / "state.sqlite3") as db:
            db.execute("UPDATE jobs SET state='running'")
    elif damage == "outstanding":
        with sqlite3.connect(root / "state.sqlite3") as db:
            db.execute("INSERT INTO owned VALUES('plan:R0018','present')")
    elif damage == "reused-id":
        changed = deepcopy(proof)
        changed["carry_forward_dimensions"]["CREATE"]["identity_continuation"]["remediation_sha256"] = "0" * 64
        json_write(root / "inputs/previous.json", changed)
        json_write(
            root / "creation-authorizations/previous.json",
            {"authorization_evidence": "inputs/previous.json", "execution_sha256": execution_digest(new)},
        )
    else:
        with sqlite3.connect(root / "execution-gate.sqlite3") as db:
            if damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown'")
            elif damage == "used-write":
                db.execute("UPDATE grants SET used=1 WHERE id='create'")
                db.execute("UPDATE authority_limits SET used=1 WHERE id='create'")
            elif damage == "not-exhausted":
                db.execute("UPDATE authority_limits SET used=0 WHERE id='identity'")
            elif damage == "wrong-signed-account":
                db.execute("UPDATE attempts SET response=?", (json.dumps({"AccountId": "other"}),))
            elif damage == "used-after-review":
                db.execute("UPDATE authority_limits SET used=2 WHERE id='identity'")
    if damage != "wrong-execution":
        proof["proposal_execution_sha256"] = execution_digest(new)
    with pytest.raises(WorkflowError):
        reviewed_authorities(root, new, proof)


@pytest.fixture
def composed_identity_review(identity_continuation):
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, install

    root, old, new, proof = identity_continuation
    old["execution_grants"]["ROS"] = deepcopy(old["execution_grants"]["CREATE"])
    old["cleanup_recipe"]["ROS"] = "original response-bound deletion"
    json_write(root / "inputs/old.json", old)
    json_write(
        authorization_path(root, old),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "resource_ids": ["R0018"],
            "account": "fake",
            "max_budget_cents": 0,
            "execution_sha256": execution_digest(old),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/original-review.json",
            "authorization_sha256": digest(root / "inputs/original-review.json"),
        },
    )
    # Fixture records real test journal shapes: consumed ROS writes and an
    # exhausted successful identity but unused CREATE writes/read.
    with sqlite3.connect(root / "execution-gate.sqlite3") as db:
        db.execute("UPDATE grants SET authority=?", (execution_digest(old) + ":CREATE",))
        db.execute("UPDATE authority_limits SET authority=?", (execution_digest(old) + ":CREATE",))
    install(
        root / "execution-gate.sqlite3",
        "1",
        "a" * 32 + ":ROS",
        "ws~ros",
        "fake",
        None,
        old["execution_grants"]["ROS"],
        authority=execution_digest(old) + ":ROS",
    )
    with sqlite3.connect(root / "execution-gate.sqlite3") as db:
        db.execute("UPDATE grants SET used=1 WHERE scope=? AND kind IN ('create','delete')", ("a" * 32 + ":ROS",))
        db.execute(
            "UPDATE authority_limits SET used=1 WHERE authority=? AND id IN ('create','delete')",
            (execution_digest(old) + ":ROS",),
        )
        for grant in ("create", "delete"):
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,state,response) VALUES(?,?,?,?,?)",
                ("ros-" + grant, "a" * 32 + ":ROS", grant, "succeeded", canonical({"RequestId": grant})),
            )
    new["execution_grants"]["ROS"] = deepcopy(old["execution_grants"]["ROS"])
    new["additional_source_requests"] = [
        {
            "request_id": "mixed-source-ROS-001",
            "dimension": "ROS",
            "prior_scope": "a" * 32 + ":ROS",
            "predecessor_proposal": "inputs/old.json",
            "predecessor_sha256": digest(root / "inputs/old.json"),
            "remediation_evidence": "inputs/repair.json",
            "remediation_sha256": digest(root / "inputs/repair.json"),
        }
    ]
    source = proof["carry_forward_dimensions"]["CREATE"]
    source["proposal_sha256"] = digest(root / "inputs/old.json")
    source["execution_sha256"] = execution_digest(old)
    json_write(root / "inputs/final.json", new)
    final_sha = digest(root / "inputs/final.json")
    composed = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "proposal_sha256": final_sha,
        "additional_sources_approved": deepcopy(new["additional_source_requests"]),
        "carry_forward_dimensions": deepcopy(proof["carry_forward_dimensions"]),
        "identity_continuation_reviews": {
            "CREATE": {
                "kind": "ops_identity_continuation_review",
                "approved": True,
                "dimension": "CREATE",
                "proposal_sha256": final_sha,
                "proposal_execution_sha256": execution_digest(new),
                "identity_continuation_approved": deepcopy(source["identity_continuation"]),
            }
        },
    }
    return root, old, new, composed


def test_distinct_source_and_identity_reviews_compose_without_reset(composed_identity_review):
    root, old, new, proof = composed_identity_review
    wire = (root / "execution-gate.sqlite3").read_bytes()
    state = (root / "state.sqlite3").read_bytes()
    bindings = reviewed_authorities(root, new, proof)
    assert bindings["CREATE"] == execution_authority(root, old, "CREATE")
    assert bindings["ROS"] != execution_authority(root, old, "ROS")
    assert new["execution_grants"]["CREATE"][0]["quota"] == 2
    assert new["execution_grants"]["CREATE"][1:] == old["execution_grants"]["CREATE"][1:]
    assert (root / "execution-gate.sqlite3").read_bytes() == wire
    assert (root / "state.sqlite3").read_bytes() == state


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "extra",
        "kind",
        "unapproved",
        "dimension",
        "request",
        "proposal",
        "execution",
        "read-quota",
        "write-quota",
        "used-write",
        "unknown",
    ],
)
def test_composed_identity_review_never_implicitly_renews(composed_identity_review, damage):
    root, _, new, proof = composed_identity_review
    identity = proof["identity_continuation_reviews"]["CREATE"]
    if damage == "missing":
        proof.pop("identity_continuation_reviews")
    elif damage == "extra":
        proof["identity_continuation_reviews"]["QUERY"] = deepcopy(identity)
    elif damage == "kind":
        identity["kind"] = "ops_additional_sources_review"
    elif damage == "unapproved":
        identity["approved"] = False
    elif damage == "dimension":
        identity["dimension"] = "QUERY"
    elif damage == "request":
        identity["identity_continuation_approved"]["request_id"] = "other-request"
    elif damage == "proposal":
        identity["proposal_sha256"] = "0" * 64
    elif damage == "execution":
        identity["proposal_execution_sha256"] = "0" * 64
    elif damage in {"read-quota", "write-quota"}:
        new["execution_grants"]["CREATE"][1 if damage == "read-quota" else 2]["quota"] += 1
        identity["proposal_execution_sha256"] = execution_digest(new)
    else:
        with sqlite3.connect(root / "execution-gate.sqlite3") as db:
            if damage == "used-write":
                db.execute("UPDATE authority_limits SET used=1 WHERE authority LIKE '%:CREATE' AND id='create'")
                db.execute("UPDATE grants SET used=1 WHERE scope LIKE '%:CREATE' AND id='create'")
            else:
                db.execute("UPDATE attempts SET state='unknown' WHERE scope LIKE '%:CREATE'")
    wire = (root / "execution-gate.sqlite3").read_bytes()
    with pytest.raises(WorkflowError):
        reviewed_authorities(root, new, proof)
    assert (root / "execution-gate.sqlite3").read_bytes() == wire
