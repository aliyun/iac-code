"""Only independently reconciled definite rejections stop blocking future acceptance."""

import copy
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.store import Store


@pytest.fixture
def rejection(tmp_path):
    store = Store.initialize(tmp_path / "state")
    task, job, region, account = "fixture:R0017", "a" * 32, "cn-hangzhou", "fake-account"
    with store.transaction() as state:
        state.execute(
            "INSERT INTO tasks(id,run_id,resource_id,spec,state) VALUES(?,?,?,?,?)",
            (task, "fixture", "R0017", "{}", "repair"),
        )
        state.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job, task, "glm", "dispatch", "complete", "fake-thread", "fake-turn", "fake-token", "confirmed", 1),
        )
    template = {
        "Resources": {
            "Prefix": {
                "Type": "ALIYUN::ECS::PrefixList",
                "Properties": {"PrefixListName": "fixture-ros", "AddressFamily": "IPv4", "MaxEntries": 1},
            }
        }
    }
    rows = [
        {
            "id": "identity",
            "scope": job + ":ROS",
            "grant_id": "identity",
            "state": "succeeded",
            "response": json.dumps({"AccountId": account}),
            "created": 1,
            "kind": "identity",
            "call": json.dumps({"product": "sts", "action": "GetCallerIdentity", "params": {}}),
        },
        {
            "id": "rejected",
            "scope": job + ":ROS",
            "grant_id": "create",
            "state": "unknown",
            "response": json.dumps(
                {
                    "Code": "StackValidationFailed",
                    "statusCode": 400,
                    "RequestId": "fake-request",
                    "Message": 'AddressFamily: "IPv4" is not an allowed value [Ipv4, Ipv6]',
                }
            ),
            "created": 2,
            "kind": "create",
            "call": json.dumps(
                {
                    "product": "ros",
                    "action": "CreateStack",
                    "region_id": region,
                    "params": {"RegionId": region, "StackName": "fixture-ros", "TemplateBody": json.dumps(template)},
                }
            ),
        },
        {
            "id": "absence",
            "scope": job + ":ROS",
            "grant_id": "name-inventory",
            "state": "succeeded",
            "response": json.dumps({"RequestId": "fake-read", "PrefixLists": {"PrefixList": []}}),
            "created": 3,
            "kind": "read",
            "call": json.dumps(
                {
                    "product": "ecs",
                    "action": "DescribePrefixLists",
                    "params": {"RegionId": region, "PrefixListName": "fixture-ros", "MaxResults": 10},
                }
            ),
        },
    ]
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
        gate.executescript(
            "CREATE TABLE grants(scope,id,kind,call,used,authority);"
            "CREATE TABLE attempts(id,scope,grant_id,state,response,created,session,credential);"
        )
        for row in rows:
            gate.execute(
                "INSERT INTO grants VALUES(?,?,?,?,1,'fake-original-authority')",
                (row["scope"], row["grant_id"], row["kind"], row["call"]),
            )
            gate.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                tuple(row[key] for key in ["id", "scope", "grant_id", "state", "response", "created"])
                + ("ws~fake-complete", "fake-credential"),
            )
    calls = []
    for label, product, action, params, response in [
        ("identity", "sts", "GetCallerIdentity", {}, {"AccountId": account, "RequestId": "fake-fresh"}),
        (
            "stacks",
            "ros",
            "ListStacks",
            {"RegionId": region, "StackName.1": "fixture-ros", "PageSize": 10},
            {"RequestId": "fake-list", "TotalCount": 0, "Stacks": []},
        ),
    ]:
        value = {
            "product": product,
            "action": action,
            "profile": "test-guima",
            "region": region,
            "parameters": params,
            "response": response,
            "exit_code": 0,
            "cloud_write_performed": False,
        }
        path = "inputs/" + label + ".json"
        json_write(store.root / path, value)
        calls.append({"path": path, "sha256": digest(store.root / path), "value": value})
    audit = {
        "kind": "ops_independent_rejected_create_absence",
        "task_id": task,
        "original_job": job,
        "actual_account": account,
        "region": region,
        "original_wire": rows,
        "calls": calls,
        "confirmed_no_residual": True,
        "no_journal_mutation": True,
    }
    json_write(store.root / "inputs/rejection.json", audit)
    return store, task, audit


def test_reconciliation_preserves_unknown_and_used_counts_and_is_idempotent(rejection):
    store, task, _ = rejection
    result = store.reconcile_rejected_creation(task, "inputs/rejection.json")
    assert result == {"reconciled_attempts": ["rejected"], "wire_unchanged": True, "new_pass": 0}
    assert store.reconcile_rejected_creation(task, "inputs/rejection.json")["already_recorded"] is True
    with store.transaction() as state:
        assert store.reconciled_creations(state, task) == {"rejected"}
        assert state.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0
        assert state.execute("SELECT count(*) FROM owned").fetchone()[0] == 0
        assert state.execute("SELECT count(*) FROM events WHERE kind='ops_creation_reconciled'").fetchone()[0] == 1
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
        assert gate.execute("SELECT state FROM attempts WHERE id='rejected'").fetchone() == ("unknown",)
        assert gate.execute("SELECT used FROM grants WHERE id='create'").fetchone() == (1,)


@pytest.mark.parametrize(
    "damage",
    [
        "not_absent",
        "account",
        "stack_name",
        "stack_present",
        "read_error",
        "raw_sha",
        "raw_value",
        "has_stack_id",
        "http500",
        "unknown_code",
        "unknown_message",
        "child_present",
        "next_token",
        "wrong_child_name",
        "wire_changed",
        "different_credential",
        "live_job",
        "owned",
        "input_changed_after_binding",
    ],
)
def test_unknown_or_unproved_creation_never_gets_reconciled(rejection, damage):
    store, task, original = rejection
    audit = copy.deepcopy(original)
    if damage == "not_absent":
        audit["confirmed_no_residual"] = False
    elif damage == "account":
        audit["actual_account"] = "wrong"
    elif damage in {"stack_name", "stack_present", "read_error", "raw_sha", "raw_value"}:
        record = audit["calls"][1]
        if damage == "stack_name":
            record["value"]["parameters"]["StackName.1"] = "unrelated"
        elif damage == "stack_present":
            record["value"]["response"]["TotalCount"] = 1
        elif damage == "read_error":
            record["value"]["exit_code"] = 1
        elif damage == "raw_sha":
            record["sha256"] = "wrong"
        elif damage == "raw_value":
            record["value"]["profile"] = "untrusted"
        if damage not in {"raw_sha", "raw_value"}:
            json_write(store.root / record["path"], record["value"])
            record["sha256"] = digest(store.root / record["path"])
    elif damage in {"has_stack_id", "http500", "unknown_code", "unknown_message"}:
        response = json.loads(audit["original_wire"][1]["response"])
        key, value = {
            "has_stack_id": ("StackId", "possible-stack"),
            "http500": ("statusCode", 500),
            "unknown_code": ("Code", "InternalError"),
            "unknown_message": ("Message", "Unknown write"),
        }[damage]
        response[key] = value
        audit["original_wire"][1]["response"] = json.dumps(response)
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
            gate.execute("UPDATE attempts SET response=? WHERE id='rejected'", (json.dumps(response),))
    elif damage in {"child_present", "next_token", "wrong_child_name"}:
        row = audit["original_wire"][2]
        response, call = json.loads(row["response"]), json.loads(row["call"])
        if damage == "child_present":
            response["PrefixLists"]["PrefixList"] = [{"PrefixListId": "existing"}]
        elif damage == "next_token":
            response["NextToken"] = "more"
        else:
            call["params"]["PrefixListName"] = "unrelated"
        row["response"], row["call"] = json.dumps(response), json.dumps(call)
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
            gate.execute("UPDATE attempts SET response=? WHERE id='absence'", (row["response"],))
            gate.execute("UPDATE grants SET call=? WHERE id='name-inventory'", (row["call"],))
    elif damage in {"wire_changed", "different_credential"}:
        column, value = ("response", "{}") if damage == "wire_changed" else ("credential", "different")
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
            gate.execute(f"UPDATE attempts SET {column}=? WHERE id='rejected'", (value,))
    elif damage in {"live_job", "owned"}:
        with store.transaction() as state:
            if damage == "live_job":
                state.execute("UPDATE jobs SET state='running'")
            else:
                state.execute("INSERT INTO owned VALUES('owner',?,'ROS','{}','created','{}')", (task,))
    elif damage == "input_changed_after_binding":
        store.reconcile_rejected_creation(task, "inputs/rejection.json")
        audit["changed"] = True
    json_write(store.root / "inputs/rejection.json", audit)
    with pytest.raises(WorkflowError):
        if damage == "input_changed_after_binding":
            with store.transaction() as state:
                store.reconciled_creations(state, task)
        else:
            store.reconcile_rejected_creation(task, "inputs/rejection.json")
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
        assert gate.execute("SELECT used FROM grants WHERE id='create'").fetchone()[0] == 1


def test_cli_accepts_only_explicit_ops_entrypoint():
    from scripts.web.e2e.resource_link_workflow.__main__ import parser

    args = parser().parse_args(["reconcile-rejected-creation", "fixture:R0017", "--evidence", "inputs/proof.json"])
    assert args.evidence == "inputs/proof.json"


@pytest.fixture
def role_rejection(rejection):
    store, task, audit = rejection
    job, region, account = audit["original_job"], audit["region"], audit["actual_account"]
    role_name, stack_name = "fixture-role", "fixture-role-stack"
    template = {
        "Resources": {
            "Role": {
                "Type": "ALIYUN::RAM::Role",
                "Properties": {
                    "RoleName": role_name,
                    "AssumeRolePolicyDocument": {
                        "Statement": [{"Action": ["sts:AssumeRole"]}],
                    },
                },
            }
        }
    }
    negative = {
        "Code": "EntityNotExist.Role",
        "statusCode": 404,
        "HostId": "ram.aliyuncs.com",
        "Message": "The role not exists:" + role_name,
        "RequestId": "fake-role-read",
    }
    stack_body = {"RequestId": "fake-stack-read", "Stacks": [], "TotalCount": 0, "PageNumber": 1, "PageSize": 10}
    entries = [
        (
            "identity",
            "identity",
            "succeeded",
            {
                "product": "sts",
                "action": "GetCallerIdentity",
                "version": "2015-04-01",
                "region_id": region,
                "params": {},
            },
            {"AccountId": account, "RequestId": "fake-original-identity"},
        ),
        (
            "preflight-role-name",
            "read",
            "unknown",
            {
                "product": "ram",
                "action": "GetRole",
                "version": "2015-05-01",
                "region_id": region,
                "params": {"RoleName": role_name},
            },
            {"status": 404, "headers": {"x-acs-request-id": negative["RequestId"]}, "body": negative},
        ),
        (
            "preflight-stack-name",
            "read",
            "succeeded",
            {
                "product": "ros",
                "action": "ListStacks",
                "version": "2019-09-10",
                "region_id": region,
                "params": {"RegionId": region, "StackName": [stack_name], "PageNumber": 1, "PageSize": 10},
            },
            stack_body,
        ),
        (
            "create",
            "create",
            "unknown",
            {
                "product": "ros",
                "action": "CreateStack",
                "version": "2019-09-10",
                "region_id": region,
                "params": {"RegionId": region, "StackName": stack_name, "TemplateBody": json.dumps(template)},
            },
            {
                "Code": "StackValidationFailed",
                "statusCode": 400,
                "HostId": "ros.aliyuncs.com",
                "RequestId": "fake-rejected-create",
                "Message": (
                    "Property error: : Resources.Role.Properties.AssumeRolePolicyDocument.Statement[1].Action: "
                    ": Resource [Role]: Invalid Property Action, Value \"['sts:AssumeRole']\" is not a string"
                ),
            },
        ),
    ]
    rows = []
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
        gate.execute("DELETE FROM attempts")
        gate.execute("DELETE FROM grants")
        for position, (grant_id, kind, state, call, response) in enumerate(entries, 1):
            row = {
                "id": "rejected" if kind == "create" else grant_id,
                "scope": job + ":ROS",
                "grant_id": grant_id,
                "state": state,
                "response": json.dumps(response),
                "created": position,
                "session": "ws~fake-complete",
                "credential": "fake-credential",
                "kind": kind,
                "call": json.dumps(call),
                "authority": "fake-original-authority",
            }
            rows.append(row)
            gate.execute(
                "INSERT INTO grants VALUES(?,?,?,?,1,?)",
                tuple(
                    row[k]
                    for k in (
                        "scope",
                        "grant_id",
                        "kind",
                        "call",
                        "authority",
                    )
                ),
            )
            gate.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                tuple(
                    row[k]
                    for k in (
                        "id",
                        "scope",
                        "grant_id",
                        "state",
                        "response",
                        "created",
                        "session",
                        "credential",
                    )
                ),
            )
    audit["original_wire"] = rows
    audit["calls"][1]["value"]["parameters"] = {
        "RegionId": region,
        "StackName.1": stack_name,
        "PageNumber": 1,
        "PageSize": 10,
    }
    audit["calls"][1]["value"]["response"] = stack_body
    audit["calls"].append(
        {
            "path": "inputs/role.json",
            "value": {
                "product": "ram",
                "action": "GetRole",
                "parameters": {"RoleName": role_name},
                "profile": "test-guima",
                "region": region,
                "cloud_write_performed": False,
                "error_data": negative,
                "exception_type": "ClientException",
            },
        }
    )
    for item in audit["calls"]:
        json_write(store.root / item["path"], item["value"])
        item["sha256"] = digest(store.root / item["path"])
    json_write(store.root / "inputs/rejection.json", audit)
    return store, task, audit


def test_exact_role_schema_rejection_keeps_all_wire_rows_and_no_pass(role_rejection):
    store, task, _ = role_rejection
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
        before = [gate.execute("SELECT * FROM " + table).fetchall() for table in ("attempts", "grants")]
    assert store.reconcile_rejected_creation(task, "inputs/rejection.json")["reconciled_attempts"] == ["rejected"]
    assert store.reconcile_rejected_creation(task, "inputs/rejection.json")["already_recorded"] is True
    with store.transaction() as db:
        assert store.reconciled_creations(db, task) == {"rejected"}
        assert db.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM owned").fetchone()[0] == 0
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
        assert [gate.execute("SELECT * FROM " + table).fetchall() for table in ("attempts", "grants")] == before


@pytest.mark.parametrize(
    "damage",
    [
        "create500",
        "create_request",
        "create_host",
        "create_message",
        "stack_id",
        "template_type",
        "action_string",
        "extra_resource",
        "extra_statement",
        "original_identity_account",
        "original_identity_request",
        "precheck_status",
        "precheck_header",
        "precheck_name",
        "precheck_request",
        "precheck_version",
        "precheck_present",
        "precheck_stack_name",
        "precheck_stack_present",
        "precheck_stack_page",
        "precheck_stack_request",
        "fresh_account",
        "fresh_identity_request",
        "fresh_role_status",
        "fresh_role_name",
        "fresh_role_host",
        "fresh_role_request",
        "fresh_role_present",
        "fresh_stack_name",
        "fresh_stack_extra_name",
        "fresh_stack_present",
        "fresh_stack_page",
        "fresh_stack_request",
        "fresh_stack_token",
        "missing_call",
        "raw_sha",
        "raw_value",
        "profile",
        "region",
        "cloud_write",
        "missing_binding",
        "credential",
        "authority",
        "empty_session",
        "original_wire_changed",
        "live_job",
        "owner",
        "malformed_body",
    ],
)
def test_role_rejection_does_not_generalize_unknown_errors_or_reset_quotas(role_rejection, damage):
    store, task, original = role_rejection
    audit = copy.deepcopy(original)
    rows, calls = audit["original_wire"], audit["calls"]
    row_index = None
    call_index = None
    if damage in {"create500", "create_request", "create_host", "create_message", "stack_id"}:
        row_index = 3
        response = json.loads(rows[3]["response"])
        key, value = {
            "create500": ("statusCode", 500),
            "create_request": ("RequestId", ""),
            "create_host": ("HostId", "other"),
            "create_message": ("Message", "unrelated failure"),
            "stack_id": ("StackId", "possible-stack"),
        }[damage]
        response[key] = value
        rows[3]["response"] = json.dumps(response)
    elif damage in {"template_type", "action_string", "extra_resource", "extra_statement"}:
        row_index = 3
        call = json.loads(rows[3]["call"])
        template = json.loads(call["params"]["TemplateBody"])
        resource = template["Resources"]["Role"]
        if damage == "template_type":
            resource["Type"] = "ALIYUN::RAM::User"
        elif damage == "action_string":
            resource["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Action"] = "sts:AssumeRole"
        elif damage == "extra_resource":
            template["Resources"]["Other"] = {"Type": "ALIYUN::RAM::Role"}
        else:
            resource["Properties"]["AssumeRolePolicyDocument"]["Statement"].append({"Action": ["sts:AssumeRole"]})
        call["params"]["TemplateBody"] = json.dumps(template)
        rows[3]["call"] = json.dumps(call)
    elif damage.startswith("original_identity"):
        row_index = 0
        response = json.loads(rows[0]["response"])
        response["AccountId" if damage.endswith("account") else "RequestId"] = (
            "wrong" if damage.endswith("account") else ""
        )
        rows[0]["response"] = json.dumps(response)
    elif damage.startswith("precheck_stack"):
        row_index = 2
        call, response = json.loads(rows[2]["call"]), json.loads(rows[2]["response"])
        if damage.endswith("name"):
            call["params"]["StackName"] = ["unrelated"]
        elif damage.endswith("present"):
            response["Stacks"] = [{"StackId": "existing"}]
        elif damage.endswith("page"):
            response["PageNumber"] = 2
        else:
            response["RequestId"] = ""
        rows[2]["call"], rows[2]["response"] = json.dumps(call), json.dumps(response)
    elif damage.startswith("precheck") or damage == "malformed_body":
        row_index = 1
        call, response = json.loads(rows[1]["call"]), json.loads(rows[1]["response"])
        if damage == "precheck_status":
            response["status"] = 500
        elif damage == "precheck_header":
            response["headers"]["x-acs-request-id"] = "wrong"
        elif damage == "precheck_name":
            call["params"]["RoleName"] = "unrelated"
        elif damage == "precheck_request":
            response["body"]["RequestId"] = ""
        elif damage == "precheck_version":
            call["version"] = "other"
        elif damage == "malformed_body":
            response["body"] = None
        else:
            response["body"] = {"Role": {"RoleName": "fixture-role"}}
        rows[1]["call"], rows[1]["response"] = json.dumps(call), json.dumps(response)
    elif damage.startswith("fresh_identity") or damage == "fresh_account":
        call_index = 0
        calls[0]["value"]["response"]["AccountId" if damage == "fresh_account" else "RequestId"] = (
            "wrong" if damage == "fresh_account" else ""
        )
    elif damage.startswith("fresh_role"):
        call_index = 2
        value = calls[2]["value"]
        if damage.endswith("name"):
            value["parameters"]["RoleName"] = "unrelated"
        elif damage.endswith("present"):
            value["success_response"] = {"Role": {"RoleName": "fixture-role"}}
        else:
            key, replacement = {
                "fresh_role_status": ("statusCode", 500),
                "fresh_role_host": ("HostId", "other"),
                "fresh_role_request": ("RequestId", ""),
            }[damage]
            value["error_data"][key] = replacement
    elif damage.startswith("fresh_stack"):
        call_index = 1
        value = calls[1]["value"]
        if damage.endswith("extra_name"):
            value["parameters"]["StackName.2"] = "other"
        elif damage.endswith("name"):
            value["parameters"]["StackName.1"] = "other"
        else:
            key, replacement = {
                "fresh_stack_present": ("TotalCount", 1),
                "fresh_stack_page": ("PageNumber", 2),
                "fresh_stack_request": ("RequestId", ""),
                "fresh_stack_token": ("NextToken", "more"),
            }[damage]
            value["response"][key] = replacement
    elif damage in {"profile", "region", "cloud_write"}:
        call_index = 2
        key, value = {
            "profile": ("profile", "other"),
            "region": ("region", "other"),
            "cloud_write": ("cloud_write_performed", True),
        }[damage]
        calls[2]["value"][key] = value
    elif damage in {"credential", "authority", "empty_session"}:
        row_index = 3
        rows[3]["session" if damage == "empty_session" else damage] = "" if damage == "empty_session" else "other"
    elif damage == "missing_binding":
        del rows[3]["credential"]
    elif damage == "missing_call":
        calls.pop()
    elif damage == "raw_sha":
        calls[2]["sha256"] = "wrong"
    elif damage == "raw_value":
        calls[2]["value"]["parameters"]["RoleName"] = "other"
    elif damage == "original_wire_changed":
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
            gate.execute("UPDATE attempts SET response='{}' WHERE id='rejected'")
    elif damage in {"live_job", "owner"}:
        with store.transaction() as db:
            if damage == "live_job":
                db.execute("UPDATE jobs SET state='running'")
            else:
                db.execute("INSERT INTO owned VALUES('owner',?,'ROS','{}','created','{}')", (task,))
    if row_index is not None:
        row = rows[row_index]
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
            gate.execute(
                "UPDATE attempts SET state=?,response=?,session=?,credential=? WHERE id=?",
                tuple(row[k] for k in ("state", "response", "session", "credential", "id")),
            )
            gate.execute(
                "UPDATE grants SET call=?,authority=? WHERE id=?",
                tuple(
                    row[k]
                    for k in (
                        "call",
                        "authority",
                        "grant_id",
                    )
                ),
            )
    if call_index is not None:
        item = calls[call_index]
        json_write(store.root / item["path"], item["value"])
        item["sha256"] = digest(store.root / item["path"])
    json_write(store.root / "inputs/rejection.json", audit)
    with pytest.raises(WorkflowError):
        store.reconcile_rejected_creation(task, "inputs/rejection.json")
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as gate:
        assert gate.execute("SELECT used FROM grants WHERE id='create'").fetchone() == (1,)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='ops_creation_reconciled'").fetchone()[0] == 0
