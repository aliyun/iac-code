import json
import sqlite3
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.phases import (
    assert_navigation_before_cleanup,
    assert_stack_source_ready,
    execution_request,
    known_absence_read,
    readonly_continuation,
)


def test_execution_request_retains_reviewed_text_and_separates_cleanup():
    approved = "请创建一次最小元数据，采样后立即清理。"
    value = execution_request(approved)
    assert value.count(approved) == 1
    assert value.startswith("当前是执行及引用生成阶段，不是清理阶段")
    assert value.endswith("才执行已审批的响应绑定删除。")
    assert "不得在本次回答里删除或清理" in value


def test_navigation_remains_available_before_cleanup(tmp_path: Path):
    assert_navigation_before_cleanup(tmp_path, "ROS")
    (tmp_path / "TF-cleanup-intent.json").write_text("{}", encoding="utf-8")
    assert_navigation_before_cleanup(tmp_path, "ROS")


@pytest.mark.parametrize("dimension", ["ROS", "TF", "CREATE", "QUERY"])
def test_cleanup_intent_prevents_only_later_navigation(tmp_path: Path, dimension: str):
    marker = tmp_path / f"{dimension}-cleanup-intent.json"
    marker.write_text('{"state":"may_have_submitted"}', encoding="utf-8")
    with pytest.raises(WorkflowError, match="navigation must precede explicit cleanup"):
        assert_navigation_before_cleanup(tmp_path, dimension)
    assert marker.read_text(encoding="utf-8") == '{"state":"may_have_submitted"}'


@pytest.fixture
def wire(tmp_path):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize

    path = tmp_path / "wire.sqlite3"
    initialize(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO active VALUES(?,?,?,?,?,?)", ("0", "job:TF", "ws~full", "fake", None, "execute")
        )
        for ident, kind, action, used in [
            ("create", "create", "CreateStack", 1),
            ("delete", "delete", "DeleteStack", 0),
            ("status", "read", "GetStack", 1),
        ]:
            quota = 3 if kind == "read" else 1
            connection.execute(
                "INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,?,?,?,?,?)",
                ("job:TF", ident, kind, json.dumps({"action": action}), quota, used, "original:TF"),
            )
            connection.execute("INSERT INTO authority_limits VALUES(?,?,?)", ("original:TF", ident, used))
        for ident, grant, response, created in [
            ("c", "create", {"StackId": "stack"}, 1),
            ("s", "status", {"StackId": "stack", "Status": "CREATE_IN_PROGRESS"}, 2),
        ]:
            connection.execute(
                "INSERT INTO attempts(id,scope,grant_id,state,response,created) VALUES(?,?,?,?,?,?)",
                (ident, "job:TF", grant, "succeeded", json.dumps(response), created),
            )
    return path


def test_pending_stack_cannot_freeze_source_but_continuation_preserves_every_counter(wire):
    before = wire.read_bytes()
    with pytest.raises(WorkflowError, match="continue-read"):
        assert_stack_source_ready(wire, "job:TF", cleanup=False)
    request = readonly_continuation(wire, "0", "job:TF", "ws~full", "source")
    assert "禁止再次创建、删除" in request
    assert wire.read_bytes() == before
    with sqlite3.connect(wire) as connection:
        connection.execute(
            "UPDATE attempts SET response=? WHERE id='s'",
            (json.dumps({"StackId": "stack", "Status": "CREATE_COMPLETE"}),),
        )
    assert_stack_source_ready(wire, "job:TF", cleanup=False)


@pytest.mark.parametrize(
    "damage", ["session", "scope", "phase", "create_unused", "unknown", "reads_exhausted", "expired"]
)
def test_read_continuation_refuses_new_writes_unknown_or_wrong_binding(wire, damage):
    with sqlite3.connect(wire) as connection:
        if damage in {"session", "scope", "phase"}:
            connection.execute(f"UPDATE active SET {damage}='wrong'")
        elif damage == "create_unused":
            connection.execute("UPDATE authority_limits SET used=0 WHERE id='create'")
        elif damage == "unknown":
            connection.execute("UPDATE attempts SET state='unknown' WHERE id='s'")
        elif damage == "expired":
            connection.execute("UPDATE active SET expires=1")
        else:
            connection.execute("UPDATE authority_limits SET used=3 WHERE id='status'")
    with pytest.raises(WorkflowError):
        readonly_continuation(wire, "0", "job:TF", "ws~full", "source")


def test_cleanup_read_continuation_requires_already_successful_delete_and_fresh_status(wire):
    with sqlite3.connect(wire) as connection:
        connection.execute("UPDATE active SET phase='cleanup'")
    with pytest.raises(WorkflowError):
        readonly_continuation(wire, "0", "job:TF", "ws~full", "cleanup")
    with sqlite3.connect(wire) as connection:
        connection.execute("UPDATE grants SET used=1 WHERE id='delete'")
        connection.execute("UPDATE authority_limits SET used=1 WHERE id='delete'")
        connection.execute(
            "INSERT INTO attempts(id,scope,grant_id,state,response,created) VALUES(?,?,?,?,?,?)",
            ("d", "job:TF", "delete", "succeeded", '{"RequestId":"delete"}', 3),
        )
    with pytest.raises(WorkflowError, match="continue-read"):
        assert_stack_source_ready(wire, "job:TF", cleanup=True)
    assert readonly_continuation(wire, "0", "job:TF", "ws~full", "cleanup")
    with sqlite3.connect(wire) as connection:
        connection.execute(
            "INSERT INTO attempts(id,scope,grant_id,state,response,created) VALUES(?,?,?,?,?,?)",
            ("s2", "job:TF", "status", "succeeded", json.dumps({"StackId": "stack", "Status": "DELETE_COMPLETE"}), 4),
        )
    assert_stack_source_ready(wire, "job:TF", cleanup=True)


def test_failed_terminal_stack_source_is_not_hidden(wire):
    with sqlite3.connect(wire) as connection:
        connection.execute(
            "UPDATE attempts SET response=? WHERE id='s'",
            (json.dumps({"StackId": "stack", "Status": "CREATE_FAILED"}),),
        )
    assert_stack_source_ready(wire, "job:TF", cleanup=False)


def test_failed_original_read_remains_capturable_without_continuing_unknown_calls(wire):
    with sqlite3.connect(wire) as connection:
        connection.execute("UPDATE attempts SET state='failed' WHERE id='s'")
    assert_stack_source_ready(wire, "job:TF", cleanup=False)
    with pytest.raises(WorkflowError):
        readonly_continuation(wire, "0", "job:TF", "ws~full", "source")


def negative_nas_read():
    return {
        "kind": "read",
        "state": "unknown",
        "call": json.dumps(
            {
                "product": "nas",
                "action": "DescribeAccessGroups",
                "params": {"AccessGroupName": "offline-owned", "FileSystemType": "standard"},
            }
        ),
        "response": json.dumps(
            {
                "Code": "InvalidAccessGroup.NotFound",
                "statusCode": 404,
                "RequestId": "actual-response-fixture",
            }
        ),
    }


@pytest.mark.parametrize(
    "damage", [None, "write", "product", "action", "name", "filesystem", "code", "http", "request", "null"]
)
def test_only_exact_known_nas_negative_read_is_resolved_not_success(damage):
    row = negative_nas_read()
    call, response = json.loads(row["call"]), json.loads(row["response"])
    if damage == "write":
        row["kind"] = "delete"
    elif damage in {"product", "action"}:
        call[damage] = "other"
    elif damage == "name":
        call["params"]["AccessGroupName"] = ""
    elif damage == "filesystem":
        call["params"]["FileSystemType"] = "other"
    elif damage in {"code", "http", "request"}:
        response[{"code": "Code", "http": "statusCode", "request": "RequestId"}[damage]] = (
            "" if damage == "request" else "wrong"
        )
    elif damage == "null":
        response = None
    row["call"], row["response"] = json.dumps(call), json.dumps(response)
    assert known_absence_read(row) is (damage is None)
    assert row["state"] == "unknown"  # Classification is not journal rewriting or a PASS.


def test_known_negative_read_does_not_hide_pending_stack_or_refill_counters(wire):
    row = negative_nas_read()
    with sqlite3.connect(wire) as db:
        db.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,?,?,?,?,?)",
            ("job:TF", "absence", "read", row["call"], 3, 1, "original:TF"),
        )
        db.execute("INSERT INTO authority_limits VALUES(?,?,?)", ("original:TF", "absence", 1))
        db.execute(
            "INSERT INTO attempts(id,scope,grant_id,state,response,created) VALUES(?,?,?,?,?,?)",
            ("n", "job:TF", "absence", "unknown", row["response"], 0),
        )
    before = wire.read_bytes()
    with pytest.raises(WorkflowError, match="continue-read"):
        assert_stack_source_ready(wire, "job:TF", cleanup=False)
    assert readonly_continuation(wire, "0", "job:TF", "ws~full", "source")
    assert wire.read_bytes() == before
