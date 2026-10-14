"""Offline contracts for the OPS-only, one-shot STS recovery capability."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow import native_identity_recovery as recovery
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write


def _calls():
    return {
        "identity": {
            "product": "sts",
            "action": "GetCallerIdentity",
            "version": "2015-04-01",
            "region_id": "cn-hangzhou",
            "params": {},
        },
        "create": {
            "product": "vpc",
            "action": "CreateVSwitch",
            "version": "2016-04-28",
            "region_id": "cn-hangzhou",
            "params": {"VSwitchName": "offline-owned"},
        },
        "delete": {
            "product": "vpc",
            "action": "DeleteVSwitch",
            "version": "2016-04-28",
            "region_id": "cn-hangzhou",
            "params": {"VSwitchId": "$created_id"},
        },
    }


@pytest.fixture
def permit_case(tmp_path, monkeypatch):
    path = tmp_path / "execution-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    scope = "original-job:QUERY"
    session = "ws~original-session"
    account = "123456789012"
    authority = "original-authority"
    grant_id = "ops-native-identity-original"
    calls = _calls()
    grants = [
        {"id": grant_id, "kind": "identity", "quota": 1, "call": calls["identity"]},
        {"id": "create-owned", "kind": "create", "quota": 1, "call": calls["create"]},
        {
            "id": "delete-owned",
            "kind": "delete",
            "quota": 1,
            "call": calls["delete"],
            "parent": "create-owned",
            "response_path": ["VSwitchId"],
            "target_param": "VSwitchId",
        },
    ]
    gate.install(path, "0", scope, session, account, None, grants, authority=authority)
    gate.cleanup(path, "0", scope, session)
    original_review = tmp_path / "original-review.json"
    json_write(original_review, {"approved": True}, private=True)
    original_review_sha = digest(original_review)
    with gate.database(path) as connection:
        connection.execute(
            "INSERT INTO cleanup_identity_reviews(scope,grant_id,session,account,authority,evidence,evidence_sha256) "
            "VALUES(?,?,?,?,?,?,?)",
            (scope, grant_id, session, account, authority, str(original_review), original_review_sha),
        )
    marker = tmp_path / "QUERY-identity-native-recovery-0001.json"
    proposal_sha = "a" * 64
    review_sha = "b" * 64
    intent = {
        "state": "may_have_submitted",
        "scope": scope,
        "session_id": session,
        "stage": "identity",
        "proposal_sha256": proposal_sha,
        "review_sha256": review_sha,
    }
    credential = SimpleNamespace(mode="AK", access_key_id="fake-key", access_key_secret="fake-secret")

    def create_marker():
        recovery._create_one_shot_marker(marker, intent)

    def arm():
        create_marker()
        gate.arm_native_identity_recovery(
            path,
            slot="0",
            scope=scope,
            session=session,
            account=account,
            grant_id=grant_id,
            authority=authority,
            call=calls["identity"],
            proposal_sha256=proposal_sha,
            review_sha256=review_sha,
            original_review_sha256=original_review_sha,
            marker_path=str(marker.resolve()),
        )

    return {
        "path": path,
        "scope": scope,
        "session": session,
        "account": account,
        "grant_id": grant_id,
        "authority": authority,
        "calls": calls,
        "credential": credential,
        "marker": marker,
        "intent": intent,
        "create_marker": create_marker,
        "arm": arm,
    }


def _counters(path):
    with sqlite3.connect(path) as connection:
        return (
            dict(connection.execute("SELECT id,used FROM grants")),
            dict(connection.execute("SELECT id,used FROM authority_limits")),
            connection.execute("SELECT id,state FROM attempts").fetchall(),
            connection.execute("SELECT state,attempt_id FROM native_identity_recovery_permits").fetchall(),
        )


def test_armed_permit_denies_delete_and_non_exact_sts_calls(permit_case):
    case = permit_case
    case["arm"]()
    before = _counters(case["path"])
    wrong_sts_args = {**case["calls"]["identity"], "params": {"RegionId": "cn-hangzhou"}}
    wrong_sts_region = {**case["calls"]["identity"], "region_id": "cn-shanghai"}
    for call in (case["calls"]["delete"], wrong_sts_args, wrong_sts_region):
        with pytest.raises(gate.GateDeniedError):
            gate.reserve(call, case["session"], case["credential"], read_only=call != case["calls"]["delete"])
    assert _counters(case["path"]) == before


def test_exact_sts_call_consumes_only_original_quota_and_result_must_match_account(permit_case):
    case = permit_case
    case["arm"]()
    assert '"DeleteVSwitch"' not in gate.execution_instructions(case["session"], "")
    assert gate.execution_instructions(case["session"], "").count('"GetCallerIdentity"') == 1
    ticket = gate.reserve(case["calls"]["identity"], case["session"], case["credential"], read_only=True)
    assert ticket
    with sqlite3.connect(case["path"]) as connection:
        assert (
            connection.execute(
                "SELECT used FROM grants WHERE scope=? AND id=?",
                (case["scope"], case["grant_id"]),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?",
                (case["authority"], case["grant_id"]),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            "SELECT state,attempt_id FROM native_identity_recovery_permits WHERE scope=?",
            (case["scope"],),
        ).fetchone() == ("consumed", ticket[1])
    gate.finish(ticket, {"AccountId": case["account"], "RequestId": "offline-identity"}, True)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(case["calls"]["identity"], case["session"], case["credential"], read_only=True)
    with sqlite3.connect(case["path"]) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identities WHERE scope=? AND account=?",
                (case["scope"], case["account"]),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT state FROM attempts WHERE id=?",
                (ticket[1],),
            ).fetchone()[0]
            == "succeeded"
        )


def test_concurrent_identity_reservations_consume_permit_once(permit_case):
    case = permit_case
    case["arm"]()

    def reserve_once(_):
        try:
            return gate.reserve(case["calls"]["identity"], case["session"], case["credential"], read_only=True)
        except gate.GateDeniedError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        tickets = list(executor.map(reserve_once, range(2)))
    assert sum(ticket is not None for ticket in tickets) == 1
    with sqlite3.connect(case["path"]) as connection:
        assert connection.execute(
            "SELECT state,attempt_id FROM native_identity_recovery_permits WHERE scope=?",
            (case["scope"],),
        ).fetchone() == ("consumed", next(ticket[1] for ticket in tickets if ticket))
        assert (
            connection.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?",
                (case["authority"], case["grant_id"]),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("failure", ["expired", "unknown", "consumed"])
def test_expired_consumed_or_unknown_permit_fails_closed(permit_case, failure):
    case = permit_case
    case["arm"]()
    if failure == "expired":
        with sqlite3.connect(case["path"]) as connection:
            connection.execute("UPDATE native_identity_recovery_permits SET expires=0 WHERE scope=?", (case["scope"],))
    else:
        ticket = gate.reserve(case["calls"]["identity"], case["session"], case["credential"], read_only=True)
        if failure == "unknown":
            gate.finish(ticket, {"ErrorCode": "offline-unknown"}, False)
    for call, read_only in ((case["calls"]["identity"], True), (case["calls"]["delete"], False)):
        with pytest.raises(gate.GateDeniedError):
            gate.reserve(call, case["session"], case["credential"], read_only=read_only)
    assert "No active reviewed API capability" in gate.execution_instructions(case["session"], "")


def test_marker_is_exclusive_and_never_overwritten_on_retry(permit_case):
    case = permit_case
    recovery._create_one_shot_marker(case["marker"], case["intent"])
    original = case["marker"].read_bytes()
    with pytest.raises(WorkflowError, match="already attempted"):
        recovery._create_one_shot_marker(case["marker"], {**case["intent"], "state": "retry"})
    assert case["marker"].read_bytes() == original


@pytest.mark.parametrize("tamper", ["account", "scope", "grant", "call", "marker"])
def test_permit_arming_rejects_changed_bindings_without_changing_gate(permit_case, tamper):
    case = permit_case
    case["create_marker"]()
    with sqlite3.connect(case["path"]) as connection:
        if tamper == "account":
            connection.execute("UPDATE active SET account='other' WHERE slot='0'")
        elif tamper == "grant":
            connection.execute("UPDATE grants SET used=1 WHERE scope=? AND id=?", (case["scope"], case["grant_id"]))
        elif tamper == "call":
            connection.execute(
                "UPDATE grants SET call=? WHERE scope=? AND id=?",
                (
                    json.dumps({**case["calls"]["identity"], "region_id": "cn-shanghai"}),
                    case["scope"],
                    case["grant_id"],
                ),
            )
    if tamper == "scope":
        bad_scope = case["scope"] + "-other"
    else:
        bad_scope = case["scope"]
    marker_path = case["marker"]
    if tamper == "marker":
        marker_path = marker_path.parent / "missing-marker.json"
    with pytest.raises(gate.GateDeniedError):
        gate.arm_native_identity_recovery(
            case["path"],
            slot="0",
            scope=bad_scope,
            session=case["session"],
            account=case["account"],
            grant_id=case["grant_id"],
            authority=case["authority"],
            call=case["calls"]["identity"],
            proposal_sha256="a" * 64,
            review_sha256="b" * 64,
            original_review_sha256="c" * 64 if tamper == "scope" else _original_review_sha(case),
            marker_path=str(marker_path.resolve()),
        )


def _original_review_sha(case):
    with sqlite3.connect(case["path"]) as connection:
        return connection.execute(
            "SELECT evidence_sha256 FROM cleanup_identity_reviews WHERE scope=?",
            (case["scope"],),
        ).fetchone()[0]
