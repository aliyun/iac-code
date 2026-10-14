"""Offline exact public-key import and single-owned-key deletion contracts."""

import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.cleanup_recovery import inspect_candidate
from scripts.web.e2e.resource_link_workflow.files import WorkflowError


def call(action, **params):
    return {"product": "ecs", "version": "2014-05-26", "action": action, "region_id": "cn-hangzhou", "params": params}


def grants():
    return [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 2,
            "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
        },
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": call("ImportKeyPair", KeyPairName="offline-new-key", PublicKeyBody="offline-public-key"),
        },
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "call": call("DeleteKeyPairs", KeyPairNames="$created_id"),
            "parent": "create",
            "response_path": ["KeyPairName"],
            "target_param": "KeyPairNames",
            "target_encoding": "json-singleton",
        },
        {
            "id": "absence",
            "kind": "read",
            "quota": 1,
            "call": call("DescribeKeyPairs", KeyPairName="$created_id"),
            "parent": "create",
            "response_path": ["KeyPairName"],
            "target_param": "KeyPairName",
            "read_policy": {"phase": "cleanup", "after_delete": "create", "required_cleanup": True},
        },
    ]


@pytest.fixture
def original(tmp_path, monkeypatch):
    path = tmp_path / "wire.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    entries = grants()
    gate.validate_grants(entries, require_read_policy=True)
    gate.install(path, "0", "job:CREATE", "ws~full", "123", None, entries, authority="original:CREATE")
    signer = SimpleNamespace(mode="AK", access_key_id="offline", access_key_secret="offline-secret")
    ticket = gate.reserve(entries[0]["call"], "ws~full", signer, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    ticket = gate.reserve(entries[1]["call"], "ws~full", signer, read_only=False)
    gate.finish(ticket, {"KeyPairName": "actual-response-name", "RequestId": "offline-import"}, True)
    return path, signer


def counters(path):
    with sqlite3.connect(path) as db:
        return dict(db.execute("SELECT id,used FROM authority_limits")), db.execute(
            "SELECT COUNT(*) FROM attempts"
        ).fetchone()[0]


@pytest.mark.parametrize(
    "target", ["actual-response-name", '["other"]', '["actual-response-name","other"]', '["offline-new-key"]']
)
def test_delete_cannot_borrow_requested_name_or_append_other_resource(original, target):
    path, signer = original
    gate.cleanup(path, "0", "job:CREATE", "ws~full")
    before = counters(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(call("DeleteKeyPairs", KeyPairNames=target), "ws~full", signer, read_only=False)
    assert counters(path) == before


def test_only_single_actual_response_name_can_be_deleted_once(original):
    path, signer = original
    delete = call("DeleteKeyPairs", KeyPairNames='["actual-response-name"]')
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete, "ws~full", signer, read_only=False)  # No premature cleanup.
    gate.cleanup(path, "0", "job:CREATE", "ws~full")
    assert '"target_encoding":"json-singleton"' in gate.execution_instructions("ws~full", "")
    gate.finish(gate.reserve(delete, "ws~full", signer, read_only=False), {"RequestId": "offline-delete"}, True)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete, "ws~full", signer, read_only=False)
    gate.finish(
        gate.reserve(call("DescribeKeyPairs", KeyPairName="actual-response-name"), "ws~full", signer, read_only=True),
        {"TotalCount": 0},
        True,
    )
    assert counters(path) == ({"identity": 1, "create": 1, "delete": 1, "absence": 1}, 4)


def test_readonly_recovery_preserves_encoding_policy_and_original_counters(original):
    path, _ = original
    before = counters(path)
    args = {
        "slot": "0",
        "scope": "job:CREATE",
        "session": "ws~full",
        "account": "123",
        "authority": "original:CREATE",
        "grants": grants(),
    }
    value = inspect_candidate(path, **args)
    assert value["owned_from_wire"][0]["identifier"] == "actual-response-name"
    delete = next(entry for entry in value["remaining_calls"] if entry["kind"] == "delete")
    assert delete["call"]["params"]["KeyPairNames"] == '["actual-response-name"]'
    assert counters(path) == before
    args["grants"][2].pop("target_encoding")
    with pytest.raises(WorkflowError, match="original exact calls"):
        inspect_candidate(path, **args)
    assert counters(path) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "product",
        "import_action",
        "import_version",
        "missing_public_key",
        "delete_action",
        "delete_param",
        "encoding",
        "response_path",
    ],
)
def test_import_exception_and_target_encoding_cannot_authorize_other_writes(mutation):
    entries = copy.deepcopy(grants())
    if mutation == "product":
        entries[1]["call"]["product"] = "other"
    elif mutation == "import_action":
        entries[1]["call"]["action"] = "ImportImage"
    elif mutation == "import_version":
        entries[1]["call"]["version"] = "unsupported"
    elif mutation == "missing_public_key":
        entries[1]["call"]["params"].pop("PublicKeyBody")
    elif mutation == "delete_action":
        entries[2]["call"]["action"] = "DeleteInstances"
    elif mutation == "delete_param":
        entries[2]["target_param"] = "Other"
    elif mutation == "encoding":
        entries[2]["target_encoding"] = "arbitrary-template"
    else:
        entries[2]["response_path"] = ["RequestId"]
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(entries, require_read_policy=True)


def test_singleton_encoding_does_not_interpret_json_inside_response_name():
    identifier = 'name","unowned'
    assert json.loads(gate.bound_target({"target_encoding": "json-singleton"}, identifier)) == [identifier]
    assert gate.bound_target({}, "legacy") == "legacy"


def test_additive_migration_keeps_original_calls_and_counters(original):
    path, _ = original
    before = counters(path)
    gate.initialize(path)
    assert counters(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT target_encoding FROM grants WHERE id='delete'").fetchone()[0] == "json-singleton"
