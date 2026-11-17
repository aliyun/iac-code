"""Offline OPS deletion provenance: no cloud, local credentials, or quota writes."""

import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.additional_source import (
    _validate_external_group_cleanup,
    reviewed_external_cleanup,
)
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write


@pytest.fixture
def external(tmp_path):
    root, task, scope, authority = tmp_path, "plan:R0076", "a" * 32 + ":ROS", "original:ROS"

    def save(name, value):
        path = "inputs/" + name + ".json"
        json_write(root / path, value)
        return {"path": path, "sha256": digest(root / path)}

    stack = {
        "id": "fake-stack",
        "account": "fake-account",
        "region": "cn-hangzhou",
        "product": "ros",
        "action": "CreateStack",
    }
    group = {
        "id": "fake-group",
        "account": "fake-account",
        "region": "cn-hangzhou",
        "resource_type": "ALIYUN::ResourceManager::ResourceGroup",
        "stack_id": "fake-stack",
    }
    template = {
        "Resources": {
            "Group": {
                "Type": group["resource_type"],
                "Properties": {"Name": "fake-name", "DisplayName": "fake-display"},
            }
        }
    }
    call = {
        "product": "ros",
        "action": "CreateStack",
        "version": "2019-09-10",
        "region_id": "cn-hangzhou",
        "params": {"StackName": "fake-stack-name", "TemplateBody": json.dumps(template)},
    }
    delete = {
        "product": "ros",
        "action": "DeleteStack",
        "version": "2019-09-10",
        "region_id": "cn-hangzhou",
        "params": {"RegionId": "cn-hangzhou", "RetainAllResources": False, "StackId": "$created_id"},
    }
    packet = {
        "task_id": task,
        "scenes": {"scene": {"account": "fake-account"}},
        "execution_grants": {
            "ROS": [
                {"id": "create", "kind": "create", "quota": 1, "call": call},
                {
                    "id": "delete",
                    "kind": "delete",
                    "quota": 1,
                    "call": delete,
                    "parent": "create",
                    "target_param": "StackId",
                },
            ]
        },
    }
    response = {"RequestId": "fake-create", "StackId": "fake-stack"}
    creation = save(
        "creation",
        {
            "attempt_id": "attempt",
            "job_id": "a" * 32,
            "dimension": "ROS",
            "state": "succeeded",
            "session_id": "fake-full-session",
            "resource": stack,
            "response": response,
        },
    )
    params = {"RegionId": "cn-hangzhou", "StackId": "fake-stack"}
    exact = delete | {"params": delete["params"] | {"StackId": "fake-stack"}}
    review = save(
        "review",
        {
            "kind": "ops_original_owned_stack_cleanup_review",
            "approved": True,
            "task_id": task,
            "scope": scope,
            "account": "fake-account",
            "region": "cn-hangzhou",
            "original_create_attempt": "attempt",
            "exact_delete_call": exact,
            "max_delete_calls": 1,
            "automatic_retries": 0,
            "cloud_create_calls": 0,
            "original_owned": [{"id": "root-owner"}, {"id": "group-owner"}],
        },
    )
    metadata = {
        "Id": "fake-group",
        "AccountId": "fake-account",
        "Status": "OK",
        "Name": "fake-name",
        "DisplayName": "fake-display",
    }
    descriptions = [
        ("sts", "GetCallerIdentity", {}, {"AccountId": "fake-account"}),
        (
            "ros",
            "GetStack",
            params,
            {"StackId": "fake-stack", "StackName": "fake-stack-name", "RegionId": "cn-hangzhou"},
        ),
        (
            "ros",
            "ListStackResources",
            params,
            {
                "Resources": [
                    {
                        "StackId": "fake-stack",
                        "LogicalResourceId": "Group",
                        "PhysicalResourceId": "fake-group",
                        "ResourceType": group["resource_type"],
                    }
                ]
            },
        ),
        ("resourcemanager", "GetResourceGroup", {"ResourceGroupId": "fake-group"}, {"ResourceGroup": metadata}),
        (
            "resourcemanager",
            "ListResources",
            {"ResourceGroupId": "fake-group", "PageNumber": 1, "PageSize": 10},
            {"TotalCount": 0, "Resources": {"Resource": []}},
        ),
        ("ros", "DeleteStack", exact["params"], {}),
    ]
    calls = []
    for index, (product, action, parameters, body) in enumerate(descriptions, 1):
        intent = {
            "product": product,
            "action": action,
            "params": parameters,
            "started": index * 10,
            "version": {"sts": "2015-04-01", "ros": "2019-09-10", "resourcemanager": "2020-03-31"}[product],
            "endpoint": product + ".aliyuncs.com",
            "profile": "test-guima",
            "max_attempts": 1,
        }
        body = body | {"RequestId": "fake-request-" + str(index)}
        raw = save(
            "raw" + str(index),
            {
                "body": body,
                "statusCode": 200,
                "finished": index * 10 + 1,
                "headers": {"x-acs-request-id": body["RequestId"]},
            },
        )
        binding = save("intent" + str(index), intent)
        calls.append(
            intent
            | {
                "source": raw["path"],
                "source_sha256": raw["sha256"],
                "intent": binding["path"],
                "intent_sha256": binding["sha256"],
            }
        )
    result = save(
        "result",
        {
            "task_id": task,
            "review_evidence": review["path"],
            "review_sha256": review["sha256"],
            "delete_submitted": True,
            "original_model_wire_unchanged": True,
            "pass_added": False,
            "calls": calls,
            "delete_success_request_id": "fake-request-6",
        },
    )
    post = save(
        "post",
        {
            "task_id": task,
            "original_scope": scope,
            "account": "fake-account",
            "cloud_writes": 0,
            "profile": "test-guima",
            "original_delete_evidence": result["path"],
            "original_delete_sha256": result["sha256"],
            "calls": [
                {
                    "product": "sts",
                    "action": "GetCallerIdentity",
                    "status_code": 200,
                    "response": {"AccountId": "fake-account"},
                },
                {
                    "product": "ros",
                    "action": "GetStack",
                    "params": params,
                    "status_code": 200,
                    "started": 70,
                    "response": {"StackId": "fake-stack", "Status": "DELETE_COMPLETE", "RequestId": "fake-terminal"},
                },
                {
                    "product": "resourcemanager",
                    "action": "ListResourceGroups",
                    "version": "2020-03-31",
                    "status_code": 200,
                    "params": {"ResourceGroupId": "fake-group", "PageNumber": 1, "PageSize": 10},
                    "response": {
                        "TotalCount": 1,
                        "RequestId": "fake-pending",
                        "ResourceGroups": {"ResourceGroup": [metadata | {"Status": "PendingDelete"}]},
                    },
                },
            ],
        },
    )
    human = save(
        "human",
        {
            "kind": "human_pending_delete_authorization",
            "approved": True,
            "task_id": task,
            "ownership_id": "group-owner",
            "resource_id": "fake-group",
            "original_user_message": "fake approval",
        },
    )
    absence = save(
        "absence",
        {
            "task_id": task,
            "ownership_id": "root-owner",
            "resource": stack,
            "absent": True,
            "failed": False,
            "original_creation": creation,
            "source_evidence": post["path"],
            "source_sha256": post["sha256"],
        },
    )
    pending = save(
        "pending",
        {
            "kind": "ops_user_accepted_pending_delete",
            "task_id": task,
            "scope": scope,
            "ownership_id": "group-owner",
            "resource": group,
            "absent": False,
            "failed": False,
            "cleanup_disposition": "user_accepted_pending_delete",
            "human_authorization": human["path"],
            "human_sha256": human["sha256"],
            "source_evidence": post["path"],
            "source_sha256": post["sha256"],
        },
    )
    proof = {
        "kind": "ops_external_ros_group_cleanup_provenance",
        "approved": True,
        "task_id": task,
        "scope": scope,
        "authority": authority,
        "pass_added": False,
        "model_wire_unchanged": True,
        "original_creation": creation,
        "delete_result": result,
        "stack_absence": absence,
        "group_pending_delete": pending,
    }
    request = {"prior_scope": scope, "external_cleanup": save("proof", proof)}
    with sqlite3.connect(root / "state.sqlite3") as state:
        state.executescript(
            "CREATE TABLE owned(id,task_id,dimension,state,resource,evidence);"
            "CREATE TABLE events(kind,data);CREATE TABLE jobs(id,task_id,role,purpose,state,turn_id);"
        )
        state.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?)", ("a" * 32, task, "glm", "dispatch", "complete", "turn"))
        for owner, resource, binding in [("root-owner", stack, absence), ("group-owner", group, pending)]:
            state.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (owner, task, "ROS", "deleted", json.dumps(resource), json.dumps(binding)),
            )
        state.execute(
            "INSERT INTO events VALUES(?,?)",
            (
                "ops_user_accepted_pending_delete",
                json.dumps(
                    {
                        "task_id": task,
                        "ownership_id": "group-owner",
                        "actual_absence_verified": False,
                        "evidence": pending,
                    }
                ),
            ),
        )
    with sqlite3.connect(root / "execution-gate.sqlite3") as gate:
        gate.executescript(
            "CREATE TABLE attempts(id,scope,grant_id,state,session,response,created,credential);"
            "CREATE TABLE identities(scope,session,credential,account);"
        )
        gate.execute(
            "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
            ("attempt", scope, "create", "succeeded", "fake-full-session", json.dumps(response), 1, "fake-credential"),
        )
        gate.execute(
            "INSERT INTO identities VALUES(?,?,?,?)", (scope, "fake-full-session", "fake-credential", "fake-account")
        )
    return root, packet, request, authority, proof, save


@pytest.fixture
def reviewed_external(external, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import creation

    root, packet, request, authority, proof, save = external
    predecessor = save("original-packet", packet)
    request.update(dimension="ROS", predecessor_proposal=predecessor["path"], predecessor_sha256=predecessor["sha256"])
    review = save(
        "operation-approval",
        {"kind": "ops_additional_source_review", "approved": True, "additional_source_approved": request},
    )
    approval = {
        "authorized": True,
        "task_id": packet["task_id"],
        "identity_mode": "existing_account",
        "authorization_evidence": review["path"],
        "authorization_sha256": review["sha256"],
        "additional_source_request": request,
    }
    json_write(root / "creation-authorizations/approved.json", approval)
    with sqlite3.connect(root / "state.sqlite3") as state:
        state.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
            ("review", packet["task_id"], "sol", "review", "running", "review-turn"),
        )
    with sqlite3.connect(root / "execution-gate.sqlite3") as gate:
        gate.executescript(
            "CREATE TABLE grants(scope,id,authority,quota,used,kind,call);"
            "CREATE TABLE authority_limits(authority,id,used);"
        )
        for entry in packet["execution_grants"]["ROS"]:
            used = 1 if entry["kind"] == "create" else 0
            gate.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
                (request["prior_scope"], entry["id"], authority, 1, used, entry["kind"], json.dumps(entry["call"])),
            )
            gate.execute("INSERT INTO authority_limits VALUES(?,?,?)", (authority, entry["id"], used))
    # The predecessor's ordinary authorization has separate contract tests;
    # this fixture focuses on rechecking the complete real-shape cleanup chain.
    monkeypatch.setattr(creation, "validate_authorization", lambda *_: None)
    monkeypatch.setattr(creation, "execution_authority", lambda *_: authority)
    return root, packet, request, authority, proof, save, approval


def test_review_rechecks_original_external_cleanup_without_model_read_or_journal_write(reviewed_external):
    root, packet, request, _, _, _, _ = reviewed_external
    before = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    assert reviewed_external_cleanup(root, packet["task_id"], request["prior_scope"], "review") is True
    assert reviewed_external_cleanup(root, packet["task_id"], "b" * 32 + ":ROS", "review") is False
    assert reviewed_external_cleanup(root, packet["task_id"], "a" * 32 + ":TF", "review") is False
    assert all((root / name).read_bytes() == value for name, value in before.items())


@pytest.mark.parametrize(
    "damage",
    [
        "unapproved",
        "review_sha",
        "request",
        "predecessor_sha",
        "model_delete",
        "active_glm",
        "other_active",
        "wrong_review",
        "owner_present",
        "proof_sha",
    ],
)
def test_review_external_cleanup_never_excuses_unverified_or_current_scope(reviewed_external, damage):
    root, packet, request, _, _, _, approval = reviewed_external
    if damage in {"unapproved", "review_sha", "request", "predecessor_sha", "proof_sha"}:
        if damage == "unapproved":
            approval["authorized"] = False
        elif damage == "review_sha":
            approval["authorization_sha256"] = "0" * 64
        else:
            review = json.loads((root / approval["authorization_evidence"]).read_text(encoding="utf-8"))
            updated = approval["additional_source_request"]
            if damage == "request":
                updated["dimension"] = "TF"
            elif damage == "predecessor_sha":
                updated["predecessor_sha256"] = "0" * 64
            else:
                updated["external_cleanup"]["sha256"] = "0" * 64
            review["additional_source_approved"] = updated
            json_write(root / approval["authorization_evidence"], review)
            approval["authorization_sha256"] = digest(root / approval["authorization_evidence"])
        json_write(root / "creation-authorizations/approved.json", approval)
    elif damage == "model_delete":
        with sqlite3.connect(root / "execution-gate.sqlite3") as gate:
            gate.execute("UPDATE authority_limits SET used=1 WHERE id='delete'")
    else:
        with sqlite3.connect(root / "state.sqlite3") as state:
            if damage == "active_glm":
                state.execute("UPDATE jobs SET state='running' WHERE role='glm'")
            elif damage == "other_active":
                state.execute(
                    "INSERT INTO jobs VALUES('other',?,'glm','dispatch','running','turn')", (packet["task_id"],)
                )
            elif damage == "wrong_review":
                state.execute("UPDATE jobs SET purpose='repair' WHERE id='review'")
            else:
                state.execute("UPDATE owned SET state='present'")
    with pytest.raises(WorkflowError):
        reviewed_external_cleanup(root, packet["task_id"], request["prior_scope"], "review")


def test_external_cleanup_is_readonly_not_a_model_delete_or_pass(external):
    root, packet, request, authority, _, _ = external
    before = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    with sqlite3.connect(f"file:{root}/execution-gate.sqlite3?mode=ro", uri=True) as gate:
        _validate_external_group_cleanup(root, packet, request, authority, gate)
    assert all((root / name).read_bytes() == value for name, value in before.items())


@pytest.mark.parametrize(
    "damage",
    [
        "authority",
        "scope",
        "task",
        "pass",
        "wire_changed",
        "sha",
        "source_changed",
        "unsigned",
        "active_turn",
        "owner_present",
        "wrong_owner_proof",
        "no_native_event",
        "wrong_create_id",
        "wrong_session",
        "wrong_template",
    ],
)
def test_external_cleanup_rejects_drift_or_unverified_provenance(external, damage):
    root, packet, request, authority, proof, save = external
    fields = {
        "authority": ("authority", "other"),
        "scope": ("scope", "other"),
        "task": ("task_id", "other"),
        "pass": ("pass_added", True),
        "wire_changed": ("model_wire_unchanged", False),
    }
    if damage in fields:
        field, value = fields[damage]
        proof[field] = value
        request["external_cleanup"] = save("changed-proof", proof)
    elif damage == "sha":
        request["external_cleanup"]["sha256"] = "0" * 64
    elif damage == "source_changed":
        json_write(root / "inputs/raw6.json", {"fake": True})
    elif damage in {"unsigned", "wrong_create_id", "wrong_session"}:
        with sqlite3.connect(root / "execution-gate.sqlite3") as gate:
            gate.execute(
                {
                    "unsigned": "DELETE FROM identities",
                    "wrong_create_id": "UPDATE attempts SET grant_id='other'",
                    "wrong_session": "UPDATE attempts SET session='other'",
                }[damage]
            )
    elif damage == "wrong_template":
        packet["execution_grants"]["ROS"][0]["call"]["params"]["TemplateBody"] = '{"Resources":{}}'
    else:
        with sqlite3.connect(root / "state.sqlite3") as state:
            state.execute(
                {
                    "active_turn": "UPDATE jobs SET state='running'",
                    "owner_present": "UPDATE owned SET state='present'",
                    "wrong_owner_proof": "UPDATE owned SET evidence='{}'",
                    "no_native_event": "DELETE FROM events",
                }[damage]
            )
    with sqlite3.connect(f"file:{root}/execution-gate.sqlite3?mode=ro", uri=True) as gate, pytest.raises(WorkflowError):
        _validate_external_group_cleanup(root, packet, request, authority, gate)


@pytest.mark.parametrize(
    "damage",
    [
        "nonempty",
        "wrong_child",
        "wrong_account",
        "wrong_delete",
        "bad_request_id",
        "failed_delete",
        "delete_before_create",
    ],
)
def test_external_cleanup_rechecks_raw_semantics_not_just_proof_hashes(external, damage):
    root, packet, request, authority, proof, save = external
    result = json.loads((root / proof["delete_result"]["path"]).read_text(encoding="utf-8"))
    index = {"nonempty": 5, "wrong_child": 3, "wrong_account": 1}.get(damage, 6)
    raw = json.loads((root / f"inputs/raw{index}.json").read_text(encoding="utf-8"))
    match = "original sole empty group"
    if damage == "nonempty":
        raw["body"]["TotalCount"] = 1
        raw["body"]["Resources"]["Resource"] = [{"Id": "existing-business-resource"}]
    elif damage == "wrong_child":
        raw["body"]["Resources"][0]["PhysicalResourceId"] = "other-group"
    elif damage == "wrong_account":
        raw["body"]["AccountId"] = "other-account"
    elif damage == "wrong_delete":
        result["calls"][5]["params"]["StackId"] = "other-stack"
        match = "original SDK response/intent/time"
    elif damage == "bad_request_id":
        raw["headers"]["x-acs-request-id"] = "different-request"
        match = "original SDK response/intent/time"
    elif damage == "failed_delete":
        raw["statusCode"] = 403
        match = "original SDK response/intent/time"
    else:
        result["calls"][5]["started"] = 0
        match = "original SDK response/intent/time"
    binding = save("damaged-raw", raw)
    result["calls"][index - 1]["source"] = binding["path"]
    result["calls"][index - 1]["source_sha256"] = binding["sha256"]
    proof["delete_result"] = save("damaged-result", result)
    request["external_cleanup"] = save("damaged-proof", proof)
    with sqlite3.connect(f"file:{root}/execution-gate.sqlite3?mode=ro", uri=True) as gate:
        with pytest.raises(WorkflowError, match=match):
            _validate_external_group_cleanup(root, packet, request, authority, gate)
