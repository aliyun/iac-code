"""Cloud-free regression tests for existing-account, program-enforced capabilities."""

import copy
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate


def call(action, **params):
    return {"product": "vpc", "action": action, "region_id": "cn-hangzhou", "params": params}


def grants():
    return [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 5,
            "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
        },
        {"id": "read", "kind": "read", "quota": 5, "call": call("DescribeVSwitches", PageSize=10)},
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": call("CreateVSwitch", VSwitchName="rl-R0001", VpcId="vpc-existing", CidrBlock="10.0.1.0/24"),
        },
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "call": call("DeleteVSwitch", VSwitchId="$created_id"),
            "parent": "create",
            "response_path": ["VSwitchId"],
            "target_param": "VSwitchId",
        },
    ]


@pytest.fixture
def active(tmp_path, monkeypatch):
    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    gate.install(path, "0", "job:CREATE", "ws~complete", "123", time.time() + 1000, grants())
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    return path, credential


def identity(active, account="123"):
    _, credential = active
    ticket = gate.reserve(grants()[0]["call"], "ws~complete", credential, read_only=True)
    gate.finish(ticket, {"AccountId": account}, True)


def test_task_scoped_capability_survives_time_but_not_explicit_close(tmp_path, monkeypatch):
    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    gate.install(path, "0", "job:CREATE", "ws~complete", "123", None, grants())
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    monkeypatch.setattr(time, "time", lambda: 9999999999)
    identity((path, credential))
    ticket = gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-own"}, True)
    gate.cleanup(path, "0", "job:CREATE", "ws~complete")
    assert '"DeleteVSwitch"' in gate.execution_instructions("ws~complete", "")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE active SET phase='closed'")
    assert "No active reviewed" in gate.execution_instructions("ws~complete", "")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(call("DeleteVSwitch", VSwitchId="vsw-own"), "ws~complete", credential, read_only=False)
    with pytest.raises(gate.GateDeniedError):
        gate.cleanup(path, "0", "job:CREATE", "ws~complete")


def test_opt_in_and_missing_file_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv(gate.ENV, raising=False)
    assert gate.check("bash", {}, "") is None
    monkeypatch.setenv(gate.ENV, str(tmp_path / "missing"))
    assert gate.check("bash", {}, "") == gate.ERROR
    assert gate.check("aliyun_api", call("DescribeVSwitches"), "") == gate.ERROR


def test_execution_plan_is_opt_in_session_scoped_and_does_not_grant_access(active, monkeypatch):
    monkeypatch.delenv(gate.ENV)
    assert gate.execution_instructions("ws~complete", "") == ""
    monkeypatch.setenv(gate.ENV, str(active[0]))
    assert "VpcId" not in gate.execution_instructions("other-session", "")
    text = gate.execution_instructions("ws~complete", "")
    assert '"CreateVSwitch"' in text
    assert '"DeleteVSwitch"' not in text  # Cleanup phase is not yet authorized.
    assert "real API responses" in text
    assert gate.check("aliyun_api", call("CreateVSwitch", VpcId="unreviewed"), "ws~complete") == gate.ERROR
    identity(active)
    ticket = gate.reserve(grants()[2]["call"], "ws~complete", active[1], read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-actual"}, True)
    assert '"remaining":0' in gate.execution_instructions("ws~complete", "")
    gate.cleanup(active[0], "0", "job:CREATE", "ws~complete")
    text = gate.execution_instructions("ws~complete", "")
    assert '"DeleteVSwitch"' in text
    assert '"CreateVSwitch"' not in text


@pytest.mark.parametrize("damage", [None, "extra", "target", "session", "authority", "phase", "consumed", "sha"])
def test_cleanup_recovery_delivers_only_original_exact_response_bound_call(active, monkeypatch, damage):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import exact_delete_request
    from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write

    path, credential = active
    identity(active)
    ticket = gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-original-response"}, True)
    gate.cleanup(path, "0", "job:CREATE", "ws~complete")
    root = path.parent
    path = path.rename(root / "execution-gate.sqlite3")
    monkeypatch.setenv(gate.ENV, str(path))
    exact = call("DeleteVSwitch", VSwitchId="vsw-original-response")
    review = {
        "kind": "ops_cleanup_only_review",
        "approved": True,
        "original_job_id": "job",
        "dimension": "CREATE",
        "scope": "job:CREATE",
        "authority": "job:CREATE",
        "account": "123",
        "delete_call": exact,
    }
    recovery = {
        "original_job_id": "job",
        "dimension": "CREATE",
        "authority": "job:CREATE",
        "evidence": "inputs/review.json",
    }
    if damage == "extra":
        review["delete_call"]["params"]["DisableRecovery"] = False
    elif damage == "target":
        review["delete_call"]["params"]["VSwitchId"] = "vsw-other"
    elif damage == "authority":
        recovery["authority"] = "another-authority"
    elif damage == "phase":
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE active SET phase='execute'")
    elif damage == "consumed":
        ticket = gate.reserve(exact, "ws~complete", credential, read_only=False)
        gate.finish(ticket, {"RequestId": "offline-delete"}, True)
    json_write(root / recovery["evidence"], review, private=True)
    recovery["evidence_sha256"] = "0" * 64 if damage == "sha" else digest(root / recovery["evidence"])
    tables = ("grants", "attempts", "identities", "authority_limits", "active")
    with sqlite3.connect(path) as connection:
        before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    if damage:
        with pytest.raises(WorkflowError):
            exact_delete_request(root, recovery, "0", "ws~other" if damage == "session" else "ws~complete")
    else:
        rendered = exact_delete_request(root, recovery, "0", "ws~complete")
        assert json.loads(rendered.rsplit("\n", 1)[1]) == exact
        assert "$created_id" not in rendered
        assert "逐字使用其全部参数" in rendered
    with sqlite3.connect(path) as connection:
        after = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    assert after == before  # Delivery/denial never installs/refills/reserves a cloud operation.


def test_admin_account_requires_actual_same_credential_identity(active):
    _, credential = active
    request = grants()[2]["call"]
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(request, "ws~complete", credential, read_only=False)
    identity(active, "wrong-account")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(request, "ws~complete", credential, read_only=False)
    identity(active)
    other = SimpleNamespace(mode="AK", access_key_id="other", access_key_secret="fake-secret")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(request, "ws~complete", other, read_only=False)
    assert gate.reserve(request, "ws~complete", credential, read_only=False)


def test_cleanup_instruction_and_wire_reuse_do_not_replenish_original_identity_quota(tmp_path, monkeypatch):
    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    entries = grants()
    entries[0]["quota"] = 2
    authority = "unchanged-original-plan:TF"
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    gate.install(path, "0", "old-job:TF", "ws~old", "123", time.time() + 1000, entries, authority=authority)
    ticket = gate.reserve(entries[0]["call"], "ws~old", credential, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE active SET expires=0")

    gate.install(path, "0", "new-job:TF", "ws~new", "123", time.time() + 1000, entries, authority=authority)
    before = gate.execution_instructions("ws~new", "")
    assert "First make ONLY the STS identity call" in before
    ticket = gate.reserve(entries[0]["call"], "ws~new", credential, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    ticket = gate.reserve(entries[2]["call"], "ws~new", credential, read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-this-task"}, True)
    gate.cleanup(path, "0", "new-job:TF", "ws~new")
    text = gate.execution_instructions("ws~new", "")
    assert "Do not repeat STS solely because the phase changed" in text
    assert "First make ONLY the STS identity call" not in text
    assert "If the signer rotated or that binding is missing" in text
    assert "remaining quota is exhausted" in text
    assert '"remaining":0' in text
    assert '"CreateVSwitch"' not in text
    assert '"DeleteVSwitch"' in text

    with pytest.raises(gate.GateDeniedError):
        gate.reserve(entries[0]["call"], "ws~new", credential, read_only=True)
    rotated = SimpleNamespace(mode="AK", access_key_id="rotated", access_key_secret="fake-secret")
    delete = call("DeleteVSwitch", VSwitchId="vsw-this-task")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete, "ws~new", rotated, read_only=False)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete, "ws~old", credential, read_only=False)
    ticket = gate.reserve(delete, "ws~new", credential, read_only=False)
    gate.finish(ticket, {"RequestId": "offline-delete-fixture"}, True)
    with sqlite3.connect(path) as connection:
        assert dict(connection.execute("SELECT id,used FROM authority_limits WHERE authority=?", (authority,))) == {
            "identity": 2,
            "read": 0,
            "create": 1,
            "delete": 1,
        }
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 4


@pytest.mark.parametrize("account", ["123", "wrong-account"])
def test_cleanup_instruction_never_substitutes_for_actual_identity(active, account):
    path, credential = active
    identity(active)
    ticket = gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-owned"}, True)
    gate.cleanup(path, "0", "job:CREATE", "ws~complete")
    with sqlite3.connect(path) as connection:
        if account == "123":
            connection.execute("DELETE FROM identities")
        else:
            connection.execute("UPDATE identities SET account=?", (account,))
    assert "reuse the execution identity ONLY" in gate.execution_instructions("ws~complete", "")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(call("DeleteVSwitch", VSwitchId="vsw-owned"), "ws~complete", credential, read_only=False)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT used FROM grants WHERE id='delete'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2


def test_execute_continuation_reuses_only_actual_machine_bound_identity(active):
    path, credential = active
    assert "First make ONLY the STS identity call" in gate.execution_instructions("ws~complete", "")
    identity(active)
    text = gate.execution_instructions("ws~complete", "")
    assert "First make ONLY the STS identity call" not in text
    assert "execute continuation, reuse the execution identity ONLY" in text
    assert "If the signer rotated or that binding is missing" in text
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT used FROM grants WHERE id='identity'").fetchone()[0] == 1
    rotated = SimpleNamespace(mode="AK", access_key_id="rotated", access_key_secret="fake-secret")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(grants()[1]["call"], "ws~complete", rotated, read_only=True)
    assert gate.reserve(grants()[1]["call"], "ws~complete", credential, read_only=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT used FROM grants WHERE id='identity'").fetchone()[0] == 1


def test_execute_continuation_does_not_treat_wrong_account_as_identity(active):
    identity(active, "wrong-account")
    assert "First make ONLY the STS identity call" in gate.execution_instructions("ws~complete", "")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(grants()[1]["call"], "ws~complete", active[1], read_only=True)


def test_unknown_phase_instruction_fails_closed(active):
    with sqlite3.connect(active[0]) as connection:
        connection.execute("UPDATE active SET phase='unrecognized'")
    text = gate.execution_instructions("ws~complete", "")
    assert "No active reviewed API capability" in text
    assert "CreateVSwitch" not in text
    assert "DeleteVSwitch" not in text


def test_reference_scope_proof_is_actual_signer_and_successful_tool_bound(active):
    from iac_code.services.providers.aliyun import AliyunCredential
    from iac_code.tools.cloud.aliyun.resource_reference_extractors import credential_reference_context

    path, _ = active
    signer = AliyunCredential(access_key_id="fake", access_key_secret="fake-secret")
    ticket = gate.reserve(grants()[0]["call"], "ws~complete", signer, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    ticket = gate.reserve(grants()[1]["call"], "ws~complete", signer, read_only=True, tool_use_id="actual-call")
    assert gate.reference_observations(path, "job:CREATE", "ws~complete") == []
    gate.finish(ticket, {"VSwitches": {"VSwitch": []}}, True)
    assert gate.reference_observations(path, "job:CREATE", "ws~complete") == [
        {
            "account": "123",
            "reference_scope": credential_reference_context(signer)["account_scope"],
            "tool_use_id": "actual-call",
            "region": "cn-hangzhou",
        }
    ]
    assert gate.reference_observations(path, "other:CREATE", "ws~complete") == []
    assert gate.reference_observations(path, "job:CREATE", "other") == []


def test_startup_attestation_is_opt_in_private_and_contains_no_credentials(active, monkeypatch):
    import json

    path, _ = active
    chmod = Mock(wraps=gate.os.chmod)
    monkeypatch.setattr(gate.os, "chmod", chmod)
    gate.attest_web_runtime()
    marker = path.parent / "runtime-attestations/slot-0.json"
    value = json.loads(marker.read_text(encoding="utf-8"))
    assert value["slot"] == "0"
    assert {item["module"] for item in value["modules"]} == set(gate.ATTEST_MODULES)
    assert {
        "iac_code.services.providers.aliyun",
        "iac_code.tools.result_storage",
        "iac_code.providers.openai_provider",
        "iac_code.providers.dashscope_provider",
        "iac_code.providers.thinking",
        "iac_code.web.token_transport",
        "iac_code.web.resource_signin",
        "iac_code.web.app",
    } <= set(gate.ATTEST_MODULES)
    for item in value["modules"]:
        assert item["sha256"] == gate.hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
    assert all(item["sha256"] and item["path"].endswith(".py") for item in value["modules"])
    assert {item["relative_path"] for item in value["assets"]} == set(gate.ATTEST_ASSETS)
    assert all(item["sha256"] and Path(item["path"]).is_file() for item in value["assets"])
    assert "fake-secret" not in marker.read_text(encoding="utf-8")
    assert any(Path(args[0]).parent == marker.parent and args[1] == 0o600 for args, _ in chmod.call_args_list)
    # Windows stat exposes CRT mode bits; it cannot report a POSIX 0600 mode.
    assert marker.stat().st_mode & 0o777 == (0o666 if gate.os.name == "nt" else 0o600)
    monkeypatch.delenv(gate.ENV)
    monkeypatch.setattr(gate.importlib, "import_module", lambda *_: pytest.fail("ordinary startup must not attest"))
    gate.attest_web_runtime()


def test_oauth_binds_actual_refreshed_sts_and_rejects_rotation(active):
    credential = SimpleNamespace(
        mode="OAuth",
        access_key_id="fake",
        access_key_secret="fake-secret",
        sts_token="fake-sts",
        sts_expiration=int(time.time()) + 3600,
    )
    ticket = gate.reserve(grants()[0]["call"], "ws~complete", credential, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    rotated = SimpleNamespace(**{**vars(credential), "sts_token": "rotated"})
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(grants()[2]["call"], "ws~complete", rotated, read_only=False)
    assert gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=False)


@pytest.mark.parametrize("changes", [{"sts_token": ""}, {"sts_expiration": 0}, {"sts_expiration": True}])
def test_oauth_missing_or_expired_sts_fails_before_wire(active, changes):
    credential = SimpleNamespace(
        **{
            "mode": "OAuth",
            "access_key_id": "fake",
            "access_key_secret": "fake-secret",
            "sts_token": "fake-sts",
            "sts_expiration": int(time.time()) + 3600,
            **changes,
        }
    )
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(grants()[0]["call"], "ws~complete", credential, read_only=True)
    with sqlite3.connect(active[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_session_region_params_hosts_and_bypass_tools_are_denied(active):
    for tool in ("bash", "write_file", "ros_deploy", "mcp__cloud", "task", "subagent"):
        assert gate.check(tool, {}, "ws~complete") == gate.ERROR
    assert gate.check("aliyun_api", grants()[2]["call"], "display-uuid") == gate.ERROR
    for extra in ({"region_id": "cn-shanghai"}, {"endpoint": "evil.example"}, {"params": {"VpcId": "other"}}):
        assert gate.check("aliyun_api", {**grants()[2]["call"], **extra}, "ws~complete") == gate.ERROR


def test_atomic_one_create_unknown_outcome_survives_restart(active):
    path, credential = active
    identity(active)

    def attempt(_):
        try:
            return gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=False)
        except gate.GateDeniedError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(item is not None for item in results) == 1
    gate.initialize(path)
    assert attempt(0) is None
    with pytest.raises(gate.GateDeniedError):
        gate.install(path, "0", "job:CREATE", "ws~complete", "123", time.time() + 1000, grants())
    with pytest.raises(gate.GateDeniedError):
        gate.install(path, "0", "another-job", "other", "123", time.time() + 1000, grants())


def test_delete_only_actual_created_id_and_cleanup_phase(active):
    path, credential = active
    identity(active)
    ticket = gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=False)
    gate.finish(ticket, {"RequestId": "real-fixture-response", "VSwitchId": "vsw-new"}, True)
    assert gate.check("aliyun_api", call("DeleteVSwitch", VSwitchId="vsw-new"), "ws~complete") == gate.ERROR
    gate.cleanup(path, "0", "job:CREATE", "ws~complete")
    assert gate.check("aliyun_api", call("DeleteVSwitch", VSwitchId="vsw-existing"), "ws~complete") == gate.ERROR
    ticket = gate.reserve(call("DeleteVSwitch", VSwitchId="vsw-new"), "ws~complete", credential, read_only=False)
    gate.finish(ticket, {"RequestId": "actual-delete"}, True)
    assert gate.check("aliyun_api", call("DeleteVSwitch", VSwitchId="vsw-new"), "ws~complete") == gate.ERROR
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 3


def test_quota_metadata_and_stale_lease(active):
    path, credential = active
    identity(active)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=True)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE active SET expires=0")
    assert gate.check("aliyun_api", grants()[2]["call"], "ws~complete") == gate.ERROR


def test_response_bound_read_and_separate_slots(tmp_path, monkeypatch):
    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    entries = grants()
    entries.append(
        {
            "id": "status",
            "kind": "read",
            "quota": 10,
            "parent": "create",
            "response_path": ["VSwitchId"],
            "target_param": "VSwitchId",
            "call": call("DescribeVSwitches", VSwitchId="$created_id"),
        }
    )
    gate.install(path, "0", "job:CREATE", "ws~zero", "123", time.time() + 1000, entries)
    gate.install(path, "1", "other:CREATE", "ws~one", "456", time.time() + 1000, entries)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    ticket = gate.reserve(entries[0]["call"], "ws~zero", credential, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    ticket = gate.reserve(entries[2]["call"], "ws~zero", credential, read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-actual"}, True)
    assert gate.check("aliyun_api", call("DescribeVSwitches", VSwitchId="vsw-actual"), "ws~zero") is None
    assert gate.check("aliyun_api", call("DescribeVSwitches", VSwitchId="vsw-existing"), "ws~zero") == gate.ERROR
    monkeypatch.setenv(gate.SLOT_ENV, "1")
    assert gate.check("aliyun_api", entries[2]["call"], "ws~zero") == gate.ERROR
    assert gate.check("aliyun_api", call("DeleteVSwitch", VSwitchId="vsw-actual"), "ws~one") == gate.ERROR


def test_internal_uuid_binds_complete_key_and_exact_workspace(tmp_path, monkeypatch):
    from iac_code.web.session_manager import _encode_web_session_id

    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    session = _encode_web_session_id("/slot/0/workspace", "actual-uuid")
    gate.install(path, "0", "job:CREATE", session, "123", time.time() + 1000, grants())
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    request = grants()[0]["call"]
    assert gate.check("aliyun_api", request, "actual-uuid", cwd="/slot/0/workspace") is None
    assert gate.check("aliyun_api", request, "actual-uuid", cwd="/slot/1/workspace") == gate.ERROR
    assert gate.check("aliyun_api", request, "actual-uuid") == gate.ERROR
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    ticket = gate.reserve(request, "actual-uuid", credential, read_only=True, cwd="/slot/0/workspace")
    gate.finish(ticket, {"AccountId": "123"}, True)
    assert gate.reserve(grants()[2]["call"], "actual-uuid", credential, read_only=False, cwd="/slot/0/workspace")


def test_new_attempt_does_not_reset_same_authorization_quantity(tmp_path, monkeypatch):
    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    gate.install(
        path, "0", "job-one:CREATE", "ws~one", "123", time.time() + 1000, grants(), authority="reviewed-plan:CREATE"
    )
    ticket = gate.reserve(grants()[0]["call"], "ws~one", credential, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    ticket = gate.reserve(grants()[2]["call"], "ws~one", credential, read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-new"}, True)
    gate.cleanup(path, "0", "job-one:CREATE", "ws~one")
    ticket = gate.reserve(call("DeleteVSwitch", VSwitchId="vsw-new"), "ws~one", credential, read_only=False)
    gate.finish(ticket, {"RequestId": "actual-delete"}, True)
    gate.install(
        path, "0", "job-two:CREATE", "ws~two", "123", time.time() + 1000, grants(), authority="reviewed-plan:CREATE"
    )
    ticket = gate.reserve(grants()[0]["call"], "ws~two", credential, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(grants()[2]["call"], "ws~two", credential, read_only=False)


@pytest.mark.parametrize("phase", ["execute", "cleanup", "closed"])
def test_independently_released_slot_retains_failed_original_cleanup(active, phase):
    path, credential = active
    identity(active)
    created = gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=False)
    gate.finish(created, {"VSwitchId": "vsw-owned"}, True)
    gate.cleanup(path, "0", "job:CREATE", "ws~complete")
    deletion = gate.reserve(call("DeleteVSwitch", VSwitchId="vsw-owned"), "ws~complete", credential, read_only=False)
    gate.finish(deletion, {"Code": "DependencyViolation"}, False)
    with sqlite3.connect(path) as db:
        # Only the workflow ownership review can release the real scope. This
        # fixture simulates its explicit close, never overwriting old attempts.
        db.execute("UPDATE active SET phase=?", (phase,))
        before = db.execute("SELECT id,state,response FROM attempts ORDER BY id").fetchall()
        counts = db.execute("SELECT authority,id,used FROM authority_limits ORDER BY authority,id").fetchall()
    if phase == "closed":
        gate.install(path, "0", "next-resource:CREATE", "ws~next", "123", None, grants(), authority="new-resource")
    else:
        with pytest.raises(gate.GateDeniedError):
            gate.install(path, "0", "next-resource:CREATE", "ws~next", "123", None, grants())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id,state,response FROM attempts ORDER BY id").fetchall() == before
        assert (
            db.execute(
                "SELECT authority,id,used FROM authority_limits WHERE authority!='new-resource' ORDER BY authority,id"
            ).fetchall()
            == counts
        )


@pytest.mark.asyncio
async def test_direct_legacy_api_cannot_bypass_runtime_gate(active):
    from iac_code.tools.base import ToolContext
    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi

    result = await AliyunApi().execute(
        tool_input=call("DeleteVSwitch", VSwitchId="vsw-existing"), context=ToolContext()
    )
    assert result.is_error and result.content == gate.ERROR


@pytest.mark.asyncio
async def test_executor_blocks_non_api_before_tool_execution(active):
    from iac_code.tools.base import ToolContext
    from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor

    class Forbidden:
        def validate_input(self, *_):
            pytest.fail("no unauthorized tool may even begin validation/execution")

    registry = SimpleNamespace(get=lambda name: Forbidden())
    result = await ToolExecutor(registry)._validate_and_execute(
        ToolCallRequest(id="call", name="bash", input={"command": "anything"}), ToolContext()
    )
    assert result.is_error and result.content == gate.ERROR


def test_delete_recipe_and_write_limits_cannot_expand():
    for modify in (
        lambda entries: entries[2].update(quota=2),
        lambda entries: entries[3]["call"]["params"].update(VSwitchId="vsw-existing"),
        lambda entries: entries[2]["call"].update(action="ModifyVSwitchAttribute"),
        lambda entries: entries[2]["call"]["params"].update(TemplateURL="file:///tmp/template"),
    ):
        entries = copy.deepcopy(grants())
        modify(entries)
        with pytest.raises(gate.GateDeniedError):
            gate.validate_grants(entries)


@pytest.mark.asyncio
async def test_bypass_permission_mode_cannot_override_execution_gate(active):
    from iac_code.services.permissions.pipeline import check_tool_permission
    from iac_code.types.permissions import PermissionMode, ToolPermissionContext

    class Forbidden:
        name = "bash"

        async def check_permissions(self, *_):
            pytest.fail("must reject before consulting bypass mode or any model permission rule")

    result = await check_tool_permission(
        Forbidden(), {"command": "aliyun vpc DeleteVpc"}, ToolPermissionContext(mode=PermissionMode.BYPASS_PERMISSIONS)
    )
    assert result.behavior == "deny"


def test_ros_managed_template_escaping_round_trip_remains_byte_exact(tmp_path, monkeypatch):
    import json

    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    template = {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Transform": "Aliyun::OpenTofu-v1.8",
        "Workspace": {"main.tf": 'output "fixture" {\n  value = "fixture"\n}\n'},
    }
    reviewed = grants()
    reviewed[2]["call"] = {
        "product": "ros",
        "action": "CreateStack",
        "region_id": "cn-hangzhou",
        "params": {"TemplateBody": json.dumps(template, separators=(",", ":")), "StackName": "fixture"},
    }
    reviewed[3].update(
        call={
            "product": "ros",
            "action": "DeleteStack",
            "region_id": "cn-hangzhou",
            "params": {"StackId": "$created_id"},
        },
        response_path=["StackId"],
        target_param="StackId",
    )
    gate.install(path, "0", "job:TF", "ws~complete", "123", time.time() + 1000, reviewed)
    instructions = gate.execution_instructions("ws~complete", "")
    exposed = json.loads(instructions.rsplit("\n", 1)[1])
    approved = next(entry["call"] for entry in exposed if entry["kind"] == "create")
    assert approved == reviewed[2]["call"]
    assert json.loads(approved["params"]["TemplateBody"]) == template
    assert gate.check("aliyun_api", approved, "ws~complete") is None

    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    ticket = gate.reserve(reviewed[0]["call"], "ws~complete", credential, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    malformed = copy.deepcopy(approved)
    malformed["params"]["TemplateBody"] = approved["params"]["TemplateBody"].replace(r"\n", "\n")
    with pytest.raises(json.JSONDecodeError):
        json.loads(malformed["params"]["TemplateBody"])
    assert gate.check("aliyun_api", malformed, "ws~complete") == gate.ERROR
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(malformed, "ws~complete", credential, read_only=False)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert connection.execute("SELECT used FROM grants WHERE id='create'").fetchone()[0] == 0
    assert gate.reserve(approved, "ws~complete", credential, read_only=False)


def oss_call(action, **params):
    return {"product": "oss", "action": action, "version": "2019-05-17", "region_id": "cn-hangzhou", "params": params}


def oss_grants():
    bound = {"parent": "create", "response_path": ["headers", "location"], "target_param": "bucket"}
    return [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 2,
            "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
        },
        {
            "id": "precheck",
            "kind": "read",
            "quota": 1,
            "call": oss_call("GetBucketInfo", bucket="offline-new-bucket"),
            "read_policy": {"phase": "execute"},
        },
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": oss_call(
                "PutBucket",
                bucket="offline-new-bucket",
                **{
                    "x-oss-acl": "private",
                    "body": {"CreateBucketConfiguration": {"StorageClass": "Standard", "DataRedundancyType": "LRS"}},
                },
            ),
        },
        {
            "id": "source",
            "kind": "read",
            "quota": 1,
            "call": oss_call("GetBucketInfo", bucket="$created_id"),
            **bound,
            "read_policy": {"phase": "execute"},
        },
        {"id": "delete", "kind": "delete", "quota": 1, "call": oss_call("DeleteBucket", bucket="$created_id"), **bound},
        {
            "id": "absence",
            "kind": "read",
            "quota": 1,
            "call": oss_call("GetBucketInfo", bucket="$created_id"),
            **bound,
            "read_policy": {"phase": "cleanup", "after_delete": "create", "required_cleanup": True},
        },
    ]


@pytest.fixture
def oss_active(tmp_path, monkeypatch):
    path = tmp_path / "oss-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    entries = oss_grants()
    gate.validate_grants(entries, require_read_policy=True)
    gate.install(path, "0", "oss-job:CREATE", "ws~full", "123", None, entries, authority="original-oss")
    signer = SimpleNamespace(mode="AK", access_key_id="offline-oss", access_key_secret="offline-secret")
    gate.finish(gate.reserve(entries[0]["call"], "ws~full", signer, read_only=True), {"AccountId": "123"}, True)
    return path, signer


def oss_precheck(oss_active, *, status=404, code="NoSuchBucket", bucket="offline-new-bucket"):
    _, signer = oss_active
    body = {"Code": code, "BucketName": bucket, "RequestId": "offline-precheck"}
    ticket = gate.reserve(oss_grants()[1]["call"], "ws~full", signer, read_only=True)
    gate.finish(ticket, body, False, status=status, headers={"x-oss-request-id": "offline-precheck"})


def test_oss_original_header_projection_and_exact_once_only_read_delete(oss_active):
    path, signer = oss_active
    entries = oss_grants()
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(entries[2]["call"], "ws~full", signer, read_only=False)
    oss_precheck(oss_active)
    ticket = gate.reserve(entries[2]["call"], "ws~full", signer, read_only=False)
    gate.finish(
        ticket,
        None,
        True,
        status=200,
        headers={
            "Location": "/offline-new-bucket",
            "x-oss-request-id": "offline-create",
            "Authorization": "do-not-retain",
        },
    )
    created = gate.creations(path, ["oss-job"])[0]
    assert created["resource"]["id"] == "offline-new-bucket"
    assert created["response"]["body"] is None
    assert created["response"]["headers"]["Location"] == "/offline-new-bucket"
    assert created["state"] == "succeeded"
    assert "Authorization" not in str(created)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(entries[2]["call"], "ws~full", signer, read_only=False)
    source = oss_call("GetBucketInfo", bucket="offline-new-bucket")
    gate.finish(
        gate.reserve(source, "ws~full", signer, read_only=True),
        {"bucket_info": {"name": "offline-new-bucket"}},
        True,
        status=200,
        headers={},
    )
    delete = oss_call("DeleteBucket", bucket="offline-new-bucket")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete, "ws~full", signer, read_only=False)
    gate.cleanup(path, "0", "oss-job:CREATE", "ws~full")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(oss_call("DeleteBucket", bucket="other-bucket"), "ws~full", signer, read_only=False)
    gate.finish(gate.reserve(delete, "ws~full", signer, read_only=False), None, True, status=204, headers={})
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete, "ws~full", signer, read_only=False)
    gate.finish(
        gate.reserve(source, "ws~full", signer, read_only=True),
        {
            "Code": "NoSuchBucket",
            "BucketName": "offline-new-bucket",
            "RequestId": "offline-absent",
        },
        False,
        status=404,
        headers={"x-oss-request-id": "offline-absent"},
    )
    with gate.database(path) as db:
        assert {row["id"]: row["used"] for row in db.execute("SELECT * FROM authority_limits")} == {
            "identity": 1,
            "precheck": 1,
            "create": 1,
            "source": 1,
            "delete": 1,
            "absence": 1,
        }


@pytest.mark.parametrize(
    "status,code,bucket",
    [
        (403, "AccessDenied", "offline-new-bucket"),
        (500, "NoSuchBucket", "offline-new-bucket"),
        (404, "NoSuchBucket", "other-bucket"),
        (404, "NotFound", "offline-new-bucket"),
    ],
)
def test_oss_create_requires_actual_same_signer_exact_negative_precheck(oss_active, status, code, bucket):
    _, signer = oss_active
    oss_precheck(oss_active, status=status, code=code, bucket=bucket)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(oss_grants()[2]["call"], "ws~full", signer, read_only=False)


@pytest.mark.parametrize(
    "headers,status,body",
    [
        ({}, 200, None),
        ({"Location": "/offline-new-bucket"}, 200, None),
        ({"Location": "/other-bucket", "x-oss-request-id": "offline"}, 200, None),
        ({"Location": "https://example.invalid/offline-new-bucket", "x-oss-request-id": "offline"}, 200, None),
        ({"Location": "/offline-new-bucket?query", "x-oss-request-id": "offline"}, 200, None),
        ({"Location": "/offline-new-%62ucket", "x-oss-request-id": "offline"}, 200, None),
        ({"Location": "/offline-new-bucket/path", "x-oss-request-id": "offline"}, 200, None),
        ({"Location": "/offline-new-bucket", "location": "/other-bucket", "x-oss-request-id": "offline"}, 200, None),
        ({"Location": "/offline-new-bucket", "x-oss-request-id": "offline"}, 403, None),
        ({"Location": "/offline-new-bucket", "x-oss-request-id": "offline"}, 200, {"BucketName": "fabricated"}),
    ],
)
def test_oss_missing_wrong_or_contradictory_header_keeps_unknown_without_owned_target(
    oss_active,
    headers,
    status,
    body,
):
    path, signer = oss_active
    oss_precheck(oss_active)
    ticket = gate.reserve(oss_grants()[2]["call"], "ws~full", signer, read_only=False)
    gate.finish(ticket, body, status == 200, status=status, headers=headers)
    record = gate.creations(path, ["oss-job"])[0]
    assert record["state"] == "unknown"
    assert record["resource"]["id"] is None
    assert record["response"]["body"] == body
    gate.cleanup(path, "0", "oss-job:CREATE", "ws~full")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(oss_call("DeleteBucket", bucket="offline-new-bucket"), "ws~full", signer, read_only=False)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(oss_grants()[2]["call"], "ws~full", signer, read_only=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("product", "other"),
        ("action", "PutObject"),
        ("version", "other"),
    ],
)
def test_exact_put_bucket_exception_does_not_admit_other_product_action_version(field, value):
    entries = oss_grants()
    entries[2]["call"][field] = value
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(entries, require_read_policy=True)


@pytest.mark.parametrize("mutation", ["public", "paid", "no_precheck", "body_path", "wrong_delete", "wrong_param"])
def test_put_bucket_configuration_and_response_binding_fail_closed(mutation):
    entries = oss_grants()
    if mutation == "public":
        entries[2]["call"]["params"]["x-oss-acl"] = "public-read"
    elif mutation == "paid":
        entries[2]["call"]["params"]["body"]["CreateBucketConfiguration"]["DataRedundancyType"] = "ZRS"
    elif mutation == "no_precheck":
        entries.pop(1)
    elif mutation == "body_path":
        entries[4]["response_path"] = ["BucketName"]
    elif mutation == "wrong_delete":
        entries[4]["call"]["action"] = "DeleteObject"
    else:
        entries[4]["target_param"] = "key"
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(entries, require_read_policy=True)


def test_oss_header_binding_preserves_body_only_legacy_wire_records(active):
    path, signer = active
    identity(active)
    ticket = gate.reserve(grants()[2]["call"], "ws~complete", signer, read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-original"}, True, status=200, headers={"Location": "/ignored"})
    with gate.database(path) as db:
        original = db.execute("SELECT response FROM attempts WHERE id=?", (ticket[1],)).fetchone()[0]
    assert original == '{"VSwitchId":"vsw-original"}'
    gate.initialize(path)
    assert gate.creations(path, ["job"])[0]["resource"]["id"] == "vsw-original"


def test_oss_precheck_is_not_borrowed_after_signer_rotation(oss_active):
    path, signer = oss_active
    oss_precheck(oss_active)
    rotated = SimpleNamespace(mode="AK", access_key_id="rotated-oss", access_key_secret="rotated-secret")
    identity_call = oss_grants()[0]["call"]
    gate.finish(gate.reserve(identity_call, "ws~full", rotated, read_only=True), {"AccountId": "123"}, True)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(oss_grants()[2]["call"], "ws~full", rotated, read_only=False)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(oss_grants()[1]["call"], "ws~full", rotated, read_only=True)
    with gate.database(path) as db:
        assert db.execute("SELECT used FROM grants WHERE id='create'").fetchone()[0] == 0
    assert gate.reserve(oss_grants()[2]["call"], "ws~full", signer, read_only=False)


def test_oss_write_cannot_be_misclassified_as_read_or_replayed_in_wrong_session(oss_active):
    path, signer = oss_active
    oss_precheck(oss_active)
    for session, read_only in (("ws~full", True), ("ws~wrong", False)):
        with pytest.raises(gate.GateDeniedError):
            gate.reserve(oss_grants()[2]["call"], session, signer, read_only=read_only)
    with gate.database(path) as db:
        assert db.execute("SELECT used FROM grants WHERE id='create'").fetchone()[0] == 0


@pytest.mark.parametrize(
    "damage",
    [
        "product",
        "action",
        "version",
        "status",
        "request_id",
        "duplicate_request_id",
        "path",
    ],
)
def test_oss_response_projection_rejects_invalid_contract_or_request_id(damage):
    call = oss_grants()[2]["call"]
    response = {
        "status": 200,
        "body": None,
        "headers": {
            "location": "/offline-new-bucket",
            "x-oss-request-id": "offline-response",
        },
    }
    path = ["headers", "location"]
    if damage in {"product", "action", "version"}:
        call[damage] = "wrong"
    elif damage == "status":
        response["status"] = 200.0
    elif damage == "request_id":
        response["headers"]["x-oss-request-id"] = " "
    elif damage == "duplicate_request_id":
        response["headers"]["X-Oss-Request-Id"] = "offline-response"
    else:
        path = ["BucketName"]
    assert gate.response_identifier(call, response, path) is None


@pytest.fixture
def ots_precheck(tmp_path, monkeypatch):
    """One fake managed table with unchanged, independently bounded grants."""
    path = tmp_path / "ots-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    main = """terraform {
  required_providers {
    alicloud = {
      source = "aliyun/alicloud"
      version = "= 1.260.0"
    }
  }
}
provider "alicloud" {
  region = "cn-hangzhou"
}
resource "alicloud_ots_table" "table" {
  instance_name = "offline-parent"
  table_name = "offline_new_table"
  primary_key {
    name = "offline_pk"
    type = "String"
  }
  time_to_live = -1
  max_version = 1
}
output "table_id" {
  value = alicloud_ots_table.table.id
}
output "instance_name" {
  value = alicloud_ots_table.table.instance_name
}
output "table_name" {
  value = alicloud_ots_table.table.table_name
}
"""
    create = {
        "product": "ros",
        "action": "CreateStack",
        "version": "2019-09-10",
        "region_id": "cn-hangzhou",
        "params": {
            "RegionId": "cn-hangzhou",
            "StackName": "offline-stack",
            "TemplateBody": json.dumps(
                {
                    "ROSTemplateFormatVersion": "2015-09-01",
                    "Transform": "Aliyun::Terraform-v1.5",
                    "Workspace": {"main.tf": main},
                }
            ),
        },
    }
    read = {
        "product": "tablestore",
        "action": "ListTable",
        "version": "2015-12-31",
        "region_id": "cn-hangzhou",
        "params": {"InstanceName": "offline-parent"},
    }
    entries = [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 2,
            "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
        },
        {"id": "table-name-precheck", "kind": "read", "quota": 1, "read_policy": {"phase": "execute"}, "call": read},
        {"id": "create", "kind": "create", "quota": 1, "call": create},
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "parent": "create",
            "response_path": ["StackId"],
            "target_param": "StackId",
            "call": {
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "$created_id"},
            },
        },
    ]
    gate.install(path, "0", "offline-ots:TF", "ws~ots-full", "123", None, entries)
    signer = SimpleNamespace(mode="AK", access_key_id="offline-key", access_key_secret="offline-secret")
    gate.finish(gate.reserve(entries[0]["call"], "ws~ots-full", signer, read_only=True), {"AccountId": "123"}, True)
    return path, signer, entries


def ots_absence():
    return {
        "InstanceName": "offline-parent",
        "Tables": [{"InstanceName": "offline-parent", "TableName": "offline_existing"}],
    }


def ots_snapshot(path):
    with sqlite3.connect(path) as db:
        return {
            name: db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
            for name in ("active", "grants", "attempts", "identities", "authority_limits")
        }


def assert_ots_create_denied(ots_precheck):
    path, signer, entries = ots_precheck
    before = ots_snapshot(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(entries[2]["call"], "ws~ots-full", signer, read_only=False)
    assert ots_snapshot(path) == before  # No create wire intent, quota consumption or history rewrite.


def test_ots_create_requires_completed_precheck_and_consumes_create_only_once(ots_precheck):
    path, signer, entries = ots_precheck
    assert_ots_create_denied(ots_precheck)
    ticket = gate.reserve(entries[1]["call"], "ws~ots-full", signer, read_only=True)
    assert_ots_create_denied(ots_precheck)  # A parallel read has not finished.
    gate.finish(ticket, ots_absence(), True, status=200)
    before = ots_snapshot(path)
    created = gate.reserve(entries[2]["call"], "ws~ots-full", signer, read_only=False)
    gate.finish(created, {"StackId": "offline-own-stack", "RequestId": "offline-created"}, True, status=200)
    after = ots_snapshot(path)
    assert len(after["attempts"]) == len(before["attempts"]) + 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT used FROM grants WHERE id='create'").fetchone()[0] == 1
        assert db.execute("SELECT used FROM authority_limits WHERE id='create'").fetchone()[0] == 1
    assert_ots_create_denied(ots_precheck)


@pytest.mark.parametrize(
    "damage",
    [
        "present",
        "failed",
        "forbidden",
        "boolean_status",
        "missing_status",
        "missing_tables",
        "null_tables",
        "wrong_parent",
        "wrong_row_parent",
        "malformed_row",
        "duplicate",
        "truncated",
        "error_body",
        "old_body",
        "unknown",
        "other_session",
        "other_signer",
        "other_scope",
        "cleanup_read",
        "other_read",
        "extra_params",
        "wrong_region",
        "wrong_version",
        "missing_grant",
        "no_identity",
    ],
)
def test_ots_absence_is_exact_successful_same_scope_session_signer_execute_read(ots_precheck, damage):
    path, signer, entries = ots_precheck
    body, status, success = ots_absence(), 200, True
    if damage == "present":
        body["Tables"].append({"InstanceName": "offline-parent", "TableName": "offline_new_table"})
    elif damage == "failed":
        success = False
    elif damage == "forbidden":
        status = 403
    elif damage == "boolean_status":
        status = True
    elif damage == "missing_status":
        status = None
    elif damage == "missing_tables":
        body.pop("Tables")
    elif damage == "null_tables":
        body["Tables"] = None
    elif damage == "wrong_parent":
        body["InstanceName"] = "another-parent"
    elif damage == "wrong_row_parent":
        body["Tables"][0]["InstanceName"] = "another-parent"
    elif damage == "malformed_row":
        body["Tables"].append("not-a-complete-row")
    elif damage == "duplicate":
        body["Tables"] *= 2
    elif damage == "truncated":
        body["NextToken"] = "unread-page"
    elif damage == "error_body":
        body = {"Code": "AccessDenied", "Message": "offline denial"}
    ticket = gate.reserve(entries[1]["call"], "ws~ots-full", signer, read_only=True)
    gate.finish(ticket, body, success, status=status)
    with sqlite3.connect(path) as db:
        if damage == "old_body":
            db.execute("UPDATE attempts SET response=? WHERE grant_id='table-name-precheck'", (json.dumps(body),))
        elif damage == "unknown":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='table-name-precheck'")
        elif damage == "other_session":
            db.execute("UPDATE attempts SET session='ws~other' WHERE grant_id='table-name-precheck'")
        elif damage == "other_signer":
            db.execute("UPDATE attempts SET credential='different' WHERE grant_id='table-name-precheck'")
        elif damage == "other_scope":
            db.execute("UPDATE attempts SET scope='old-ots:TF' WHERE grant_id='table-name-precheck'")
        elif damage == "cleanup_read":
            db.execute(
                "UPDATE grants SET read_policy=? WHERE id='table-name-precheck'", (json.dumps({"phase": "cleanup"}),)
            )
        elif damage == "other_read":
            db.execute("UPDATE attempts SET grant_id='table-survival' WHERE grant_id='table-name-precheck'")
        elif damage in {"extra_params", "wrong_region", "wrong_version"}:
            changed = copy.deepcopy(entries[1]["call"])
            if damage == "extra_params":
                changed["params"]["TableName"] = "offline_new_table"
            elif damage == "wrong_region":
                changed["region_id"] = "cn-beijing"
            else:
                changed["version"] = "other-version"
            db.execute("UPDATE grants SET call=? WHERE id='table-name-precheck'", (json.dumps(changed),))
        elif damage == "missing_grant":
            db.execute("DELETE FROM grants WHERE id='table-name-precheck'")
        elif damage == "no_identity":
            db.execute("DELETE FROM identities")
    assert_ots_create_denied(ots_precheck)


@pytest.mark.parametrize(
    "damage",
    [
        "extra_workspace",
        "extra_resource",
        "expression",
        "provider_region",
        "call_region",
        "wrong_transform",
        "different_parent",
        "duplicate_target",
        "invalid_template",
    ],
)
def test_ots_precheck_cannot_authorize_ambiguous_or_different_template(ots_precheck, damage):
    path, signer, entries = ots_precheck
    gate.finish(
        gate.reserve(entries[1]["call"], "ws~ots-full", signer, read_only=True), ots_absence(), True, status=200
    )
    create = copy.deepcopy(entries[2]["call"])
    template = json.loads(create["params"]["TemplateBody"])
    main = template["Workspace"]["main.tf"]
    if damage == "extra_workspace":
        template["Workspace"]["extra.tf"] = main
    elif damage == "extra_resource":
        template["Workspace"]["main.tf"] += '\nresource "alicloud_ots_table" "other" {}'
    elif damage == "expression":
        template["Workspace"]["main.tf"] = main.replace('"offline_new_table"', "var.table_name")
    elif damage == "provider_region":
        template["Workspace"]["main.tf"] = main.replace("cn-hangzhou", "cn-beijing")
    elif damage == "call_region":
        create["params"]["RegionId"] = "cn-beijing"
    elif damage == "wrong_transform":
        template["Transform"] = "other-transform"
    elif damage == "different_parent":
        template["Workspace"]["main.tf"] = main.replace("offline-parent", "other-parent")
    elif damage == "duplicate_target":
        template["Workspace"]["main.tf"] = main.replace(
            'table_name = "offline_new_table"', 'table_name = "offline_new_table"\n  table_name = "other"'
        )
    create["params"]["TemplateBody"] = "not-json" if damage == "invalid_template" else json.dumps(template)
    entries[2]["call"] = create
    with sqlite3.connect(path) as db:
        db.execute("UPDATE grants SET call=? WHERE id='create'", (json.dumps(create),))
    assert_ots_create_denied(ots_precheck)


def test_ots_precheck_allows_complete_empty_list(ots_precheck):
    _, signer, entries = ots_precheck
    gate.finish(
        gate.reserve(entries[1]["call"], "ws~ots-full", signer, read_only=True),
        {"InstanceName": "offline-parent", "Tables": []},
        True,
        status=200,
    )
    assert gate.reserve(entries[2]["call"], "ws~ots-full", signer, read_only=False)


def test_ots_managed_precheck_rejects_real_signer_rotation_without_new_read(ots_precheck):
    _, signer, entries = ots_precheck
    gate.finish(
        gate.reserve(entries[1]["call"], "ws~ots-full", signer, read_only=True), ots_absence(), True, status=200
    )
    rotated = SimpleNamespace(mode="AK", access_key_id="offline-rotated", access_key_secret="offline-other-secret")
    gate.finish(gate.reserve(entries[0]["call"], "ws~ots-full", rotated, read_only=True), {"AccountId": "123"}, True)
    assert_ots_create_denied((ots_precheck[0], rotated, entries))


def test_managed_precheck_does_not_change_native_ros_creation_policy(ots_precheck):
    path, signer, entries = ots_precheck
    create = copy.deepcopy(entries[2]["call"])
    create["params"]["TemplateBody"] = json.dumps(
        {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Resources": {
                "Table": {
                    "Type": "ALIYUN::OTS::Table",
                    "Properties": {
                        "InstanceName": "offline-parent",
                        "TableName": "offline_new_table",
                    },
                }
            },
        }
    )
    with sqlite3.connect(path) as db:
        db.execute("UPDATE grants SET call=? WHERE id='create'", (json.dumps(create),))
    assert gate.reserve(create, "ws~ots-full", signer, read_only=False)


NATIVE_NAME = "iac-link-r0036-1234abcd"
NATIVE_IDS = {
    "create-filesystem-vpc": "vpc-fs",
    "create-filesystem-vswitch": "vsw-fs",
    "create-export-vpc": "vpc-exp",
    "create-export-vswitch": "vsw-exp",
    "create-bucket": NATIVE_NAME,
    "create-filesystem": "cpfs-own",
    "create-fileset": "fset-own",
    "create-protocol": "ptc-own",
    "create-export": "exp-own",
    "create-dataflow": "df-own",
}


@pytest.fixture(params=gate.NAS_NATIVE_PLAN_VARIANTS)
def native(tmp_path, monkeypatch, request):
    path = tmp_path / "native.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    plan = gate.nas_native_plan(NATIVE_NAME, variant=request.param)
    gate.install(path, "0", "native:QUERY", "ws~native", "123", None, plan)
    from iac_code.services.providers.aliyun import AliyunCredential

    credential = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    return path, credential, {grant["id"]: grant for grant in plan}


def native_call(native, ident):
    grant = native[2][ident]
    value = copy.deepcopy(grant["call"])
    for parameter, parent in grant["response_path"]["bindings"].items():
        value["params"][parameter] = ("oss://" if parameter == "SourceStorage" else "") + NATIVE_IDS[parent]
    return value


def native_body(native, ident):
    grant = native[2][ident]
    params = native_call(native, ident)["params"]
    body = {"RequestId": "offline-native"}
    if ident == "delete-bucket":
        return None
    if ident == "identity":
        body["AccountId"] = native[3] if len(native) > 3 else "123"
    if grant["kind"] == "create":
        if grant["call"]["product"] == "oss":
            return None
        body[gate.NAS_NATIVE_IDS[grant["call"]["action"]][0]] = NATIVE_IDS[ident]
    elif ident in {"read-filesystem-vpc", "read-export-vpc"}:
        side = "filesystem" if ident == "read-filesystem-vpc" else "export"
        body.update(
            TotalCount=1,
            PageNumber=1,
            PageSize=10,
            Vpcs={
                "Vpc": [
                    {
                        "VpcId": params["VpcId"],
                        "Status": "Available",
                        "CidrBlock": "172.28.0.0/16" if side == "filesystem" else "172.29.0.0/16",
                    }
                ]
            },
        )
    elif ident in {"read-filesystem-vswitch", "read-export-vswitch"}:
        side = "filesystem" if ident == "read-filesystem-vswitch" else "export"
        body.update(
            TotalCount=1,
            PageNumber=1,
            PageSize=10,
            VSwitches={
                "VSwitch": [
                    {
                        "VSwitchId": params["VSwitchId"],
                        "VpcId": NATIVE_IDS["create-" + side + "-vpc"],
                        "Status": "Available",
                        "AvailableIpAddressCount": 200,
                        "ZoneId": "cn-hangzhou-k",
                        "CidrBlock": "172.28.0.0/24" if side == "filesystem" else "172.29.0.0/24",
                    }
                ]
            },
        )
    elif ident == "read-filesystem-ready":
        body.update(
            TotalCount=1,
            PageNumber=1,
            PageSize=10,
            FileSystems={
                "FileSystem": [
                    {
                        "FileSystemId": params["FileSystemId"],
                        "Status": "Running",
                        "RegionId": "cn-hangzhou",
                        "FileSystemType": "cpfs",
                        "StorageType": "advance_200",
                        "Capacity": 3600,
                        "Bandwidth": 720,
                        "ChargeType": "PayAsYouGo",
                        "ZoneId": "cn-hangzhou-k",
                        "Version": "2.3.4",
                        "VpcId": NATIVE_IDS["create-filesystem-vpc"],
                        "VswIds": {"VswId": [NATIVE_IDS["create-filesystem-vswitch"]]},
                        "MountTargets": {"MountTarget": []},
                    }
                ]
            },
        )
    elif ident == "read-fileset-ready":
        body.update(
            Entries={
                "Entrie": [
                    {
                        "FileSystemId": params["FileSystemId"],
                        "FsetId": NATIVE_IDS["create-fileset"],
                        "Status": "CREATED",
                        "Description": NATIVE_NAME,
                        "DeletionProtection": False,
                        "FileSystemPath": "/" + NATIVE_NAME + "/",
                    }
                ]
            },
            NextToken="",
        )
    elif ident == "read-protocol-ready":
        body.update(
            ProtocolServices=[
                {
                    "FileSystemId": params["FileSystemId"],
                    "ProtocolServiceId": params["ProtocolServiceIds"],
                    "Status": "Running",
                    "ProtocolSpec": "General",
                    "ProtocolType": "NFS",
                    "Description": NATIVE_NAME,
                    "VpcId": NATIVE_IDS["create-filesystem-vpc"],
                    "VSwitchId": NATIVE_IDS["create-filesystem-vswitch"],
                }
            ],
            NextToken="",
        )
    elif ident == "read-export-ready":
        body.update(
            ProtocolMountTarget={
                "ExportId": params["ExportId"],
                "Status": "AVAILABLE",
                "ProtocolType": "NFS",
                "FsetId": NATIVE_IDS["create-fileset"],
                "Path": "/" + NATIVE_NAME + "/",
                "Description": NATIVE_NAME,
                "AccessGroupName": "DEFAULT_VPC_GROUP_NAME",
                "VpcId": NATIVE_IDS["create-export-vpc"],
                "VSwitchId": NATIVE_IDS["create-export-vswitch"],
            },
            NextToken="",
        )
    elif ident == "read-bucket":
        body = {
            "bucket_info": {
                "name": NATIVE_NAME,
                "location": "oss-cn-hangzhou",
                "storage_class": "Standard",
                "data_redundancy_type": "LRS",
                "acl": "private",
                "owner": {"id": native[3] if len(native) > 3 else "123"},
            }
        }
    elif ident == "read-bucket-empty":
        body = {"name": NATIVE_NAME, "is_truncated": False, "key_count": 0}
    elif ident == "query-dataflow-source":
        body.update(
            DataFlowInfo={
                "DataFlow": [
                    {
                        "DataFlowId": NATIVE_IDS["create-dataflow"],
                        "FileSystemId": params["FileSystemId"],
                        "FsetId": NATIVE_IDS["create-fileset"],
                        "FileSystemPath": "/" + NATIVE_NAME + "/",
                        "Description": NATIVE_NAME,
                        "SourceStorage": "oss://" + NATIVE_NAME,
                        "Status": "Running",
                        "ErrorMessage": "",
                        "SourceSecurityType": "SSL",
                        "Throughput": 600,
                        "AutoRefreshPolicy": "None",
                    }
                ]
            },
            NextToken="",
        )
    elif (
        ident.startswith("delete-")
        and ident.endswith("-absence")
        or ident.endswith(("sg-absence", "eni-absence", "implicit-members"))
    ):
        action = grant["call"]["action"]
        if action == "GetProtocolMountTarget":
            body = {"Code": "Resource.NotFound", "RequestId": "offline-not-found"}
        elif action == "GetBucketInfo":
            return {"Code": "NoSuchBucket", "BucketName": NATIVE_NAME, "RequestId": "offline-oss"}
        else:
            collection = {
                "DescribeVpcs": ("Vpcs", "Vpc"),
                "DescribeVSwitches": ("VSwitches", "VSwitch"),
                "DescribeFileSystems": ("FileSystems", "FileSystem"),
                "DescribeFilesets": ("Entries", "Entrie"),
                "DescribeDataFlows": ("DataFlowInfo", "DataFlow"),
                "DescribeSecurityGroups": ("SecurityGroups", "SecurityGroup"),
                "DescribeNetworkInterfaces": ("NetworkInterfaceSets", "NetworkInterfaceSet"),
            }.get(action)
            if collection:
                body[collection[0]] = {collection[1]: []}
            else:
                body["ProtocolServices"] = []
            if "PageSize" in params:
                body.update(TotalCount=0, PageNumber=1, PageSize=params["PageSize"])
            body["NextToken"] = ""
    elif ident == "precheck-bucket-absent":
        return {"Code": "NoSuchBucket", "BucketName": NATIVE_NAME, "RequestId": "offline-oss"}
    return body


def native_wire(native, ident, *, body=None, status=None, succeed=True):
    grant = native[2][ident]
    value = native_call(native, ident)
    if status is None:
        status = (
            204
            if ident == "delete-bucket"
            else 404
            if ident in {"precheck-bucket-absent", "delete-bucket-absence", "delete-export-absence"}
            else 200
        )
    if body is None:
        body = native_body(native, ident)
    headers = {"x-oss-request-id": "offline-oss"} if value["product"] == "oss" else {}
    if ident == "create-bucket":
        headers["Location"] = "/" + NATIVE_NAME
    ticket = gate.reserve(
        value, "ws~native", native[1], read_only=grant["kind"] in {"identity", "read"}, tool_use_id="tool-" + ident
    )
    gate.finish(ticket, body, succeed and 200 <= status < 300, status=status, headers=headers)
    return ticket


def native_network(native):
    native_wire(native, "identity")
    for side in ("filesystem", "export"):
        for prefix, suffix in (("create", "vpc"), ("read", "vpc"), ("create", "vswitch"), ("read", "vswitch")):
            native_wire(native, prefix + "-" + side + "-" + suffix)
        native_wire(native, "read-" + side + "-implicit-members")


def native_filesystem(native):
    native_network(native)
    native_wire(native, "precheck-filesystem")
    native_wire(native, "create-filesystem")
    native_wire(native, "read-filesystem-ready")


def native_complete(native):
    native_filesystem(native)
    for ident in (
        "create-fileset",
        "read-fileset-ready",
        "precheck-protocol",
        "create-protocol",
        "read-protocol-ready",
        "precheck-export",
        "create-export",
        "read-export-ready",
        "precheck-bucket-absent",
        "create-bucket",
        "read-bucket",
        "read-bucket-empty",
        "precheck-dataflow",
        "create-dataflow",
    ):
        native_wire(native, ident)
    for side in ("filesystem", "export"):
        native_wire(native, "read-" + side + "-implicit-members")


def test_native_navigation_requires_own_signed_read_after_create(native):
    native_complete(native)
    with pytest.raises(gate.GateDeniedError):
        gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")
    native_wire(native, "query-dataflow-source")
    before = native[0].read_bytes()
    proof = gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")
    assert proof["identifier"] == NATIVE_IDS["create-dataflow"]
    assert proof["tool_use_id"] == "tool-query-dataflow-source"
    assert proof["region"] == "cn-hangzhou" and proof["reference_scope"]
    assert native[0].read_bytes() == before
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    with pytest.raises(gate.GateDeniedError):
        gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")


def test_native_phase_continuation_and_cleanup_preserve_negative_reads_and_bound_calls(native):
    from scripts.web.e2e.resource_link_workflow.phases import (
        assert_cleanup_reads_complete,
        known_original_resolved_read,
        readonly_continuation,
    )

    native_complete(native)
    text = readonly_continuation(native[0], "0", "native:QUERY", "ws~native", "source")
    assert "继续" in text
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    for target in ("dataflow", "export", "protocol", "fileset", "filesystem", "bucket"):
        native_wire(native, "delete-" + target)
        native_wire(native, "delete-" + target + "-absence")
    for side in ("filesystem", "export"):
        native_wire(native, "read-" + side + "-implicit-sg-absence")
        native_wire(native, "read-" + side + "-eni-absence")
        for target in ("vswitch", "vpc"):
            native_wire(native, "delete-" + side + "-" + target)
            if (side, target) != ("export", "vpc"):
                native_wire(native, "delete-" + side + "-" + target + "-absence")
    pending = readonly_continuation(native[0], "0", "native:QUERY", "ws~native", "cleanup")
    assert '"VpcId":"vpc-exp"' in pending and "$created_id" not in pending
    native_wire(native, "delete-export-vpc-absence")
    before = native[0].read_bytes()
    assert_cleanup_reads_complete(native[0], "native:QUERY")
    with gate.database(native[0]) as db:
        rows = db.execute(
            "SELECT a.*,g.kind,g.call,g.parent FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.state='unknown'",
        ).fetchall()
        assert {r["grant_id"] for r in rows} == {
            "precheck-bucket-absent",
            "delete-bucket-absence",
            "delete-export-absence",
        }
        assert all(known_original_resolved_read(db, row) for row in rows)
    assert native[0].read_bytes() == before  # Original unknown states and quotas survive.


@pytest.mark.parametrize(
    "damage",
    [
        "DataFlowId",
        "FileSystemId",
        "FsetId",
        "FileSystemPath",
        "Description",
        "SourceStorage",
        "Status",
        "ErrorMessage",
        "SourceSecurityType",
        "Throughput",
        "AutoRefreshPolicy",
        "pagination",
        "two_rows",
        "session",
        "account",
        "scope",
        "signer",
        "tool",
        "prior_read",
        "dependency",
    ],
)
def test_native_navigation_rejects_foreign_unhealthy_or_unproven_source(native, damage):
    native_complete(native)
    body = native_body(native, "query-dataflow-source")
    if damage == "pagination":
        body["NextToken"] = "more"
    elif damage == "two_rows":
        body["DataFlowInfo"]["DataFlow"] *= 2
    elif damage not in {"session", "account", "scope", "signer", "tool", "prior_read", "dependency"}:
        body["DataFlowInfo"]["DataFlow"][0][damage] = "foreign-or-unhealthy"
    native_wire(native, "query-dataflow-source", body=body)
    with gate.database(native[0]) as db:
        if damage in {"signer", "tool"}:
            column = "credential" if damage == "signer" else "tool_use_id"
            db.execute(f"UPDATE attempts SET {column}='' WHERE grant_id='query-dataflow-source'")
        elif damage == "prior_read":
            db.execute("UPDATE attempts SET created=0 WHERE grant_id='query-dataflow-source'")
        elif damage == "dependency":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='read-export-ready'")
    with pytest.raises(gate.GateDeniedError):
        gate.nas_native_navigation_source(
            native[0],
            "wrong" if damage == "scope" else "native:QUERY",
            "wrong" if damage == "session" else "ws~native",
            "wrong" if damage == "account" else "123",
        )


@pytest.mark.parametrize(
    "damage",
    [
        "false",
        "string",
        "extra",
        "cycle",
        "path",
        "encoding",
        "action",
        "quota",
        "region",
    ],
)
def test_native_plan_is_a_closed_graph(damage):
    plan = gate.nas_native_plan(NATIVE_NAME)
    grant = next(g for g in plan if g["id"] == "precheck-protocol")
    if damage in {"false", "string"}:
        grant["call"]["params"]["DryRun"] = False if damage == "false" else "true"
    elif damage == "extra":
        grant["call"]["params"]["Throughput"] = 1000
    elif damage == "cycle":
        grant["response_path"]["bindings"]["FileSystemId"] = "create-protocol"
    elif damage == "path":
        grant["response_path"]["path"] = ["Arbitrary", "Id"]
    elif damage == "encoding":
        grant["target_encoding"] = "json-singleton"
    elif damage == "action":
        grant["call"]["action"] = "CreateMountTarget"
    elif damage == "quota":
        grant["quota"] = 2
    elif damage == "region":
        grant["call"]["region_id"] = "cn-beijing"
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(plan, require_read_policy=True)


def test_native_prechecks_are_serial_noncreating_and_not_read_aliases(native):
    native_network(native)
    value = native_call(native, "precheck-filesystem")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(value, "ws~native", native[1], read_only=True, tool_use_id="bad-read")
    native_wire(native, "precheck-filesystem")
    owners = gate.creations(native[0], ["native"])
    assert len(owners) == 4
    assert all(owner["resource"]["product"] == "vpc" for owner in owners)
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "precheck-filesystem")  # one-shot cumulative quota
    assert "DryRun=true" in gate.execution_instructions("ws~native", "")


@pytest.mark.parametrize(
    "damage",
    [
        "id",
        "unknown",
        "missing",
        "session",
        "signer",
        "scope",
        "account",
        "region",
        "extra",
    ],
)
def test_native_raw_parent_and_precheck_proof_fail_closed(native, damage):
    native_network(native)
    if damage == "id":
        native_wire(native, "precheck-filesystem", body={"RequestId": "r", "FileSystemId": "cpfs-unexpected"})
    elif damage == "unknown":
        native_wire(native, "precheck-filesystem", succeed=False)
    elif damage != "missing":
        native_wire(native, "precheck-filesystem")
        with sqlite3.connect(native[0]) as db:
            if damage in {"session", "signer", "scope"}:
                column = {"session": "session", "signer": "credential", "scope": "scope"}[damage]
                db.execute(f"UPDATE attempts SET {column}=? WHERE grant_id='precheck-filesystem'", ("other",))
            elif damage == "account":
                db.execute("UPDATE identities SET account='456'")
            else:
                raw = json.loads(
                    db.execute(
                        "SELECT response FROM attempts WHERE grant_id='precheck-filesystem'",
                    ).fetchone()[0]
                )
                if damage == "region":
                    raw["call"]["region_id"] = "cn-beijing"
                else:
                    raw["call"]["params"]["Capacity"] = 10800
                db.execute("UPDATE attempts SET response=? WHERE grant_id='precheck-filesystem'", (json.dumps(raw),))
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "create-filesystem")


@pytest.mark.parametrize("filesystem_not_found", [False, True])
def test_native_full_wire_dependency_graph_and_reverse_cleanup(native, filesystem_not_found):
    native_complete(native)
    owners = gate.creations(native[0], ["native"])
    assert {owner["resource"]["id"] for owner in owners} == set(NATIVE_IDS.values())
    assert len(owners) == 10
    assert all(owner["dimension"] == "QUERY" and owner["state"] == "succeeded" for owner in owners)
    assert all(owner["session_id"] == "ws~native" and owner["tool_use_id"] for owner in owners)
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "delete-filesystem")  # live own children cannot be skipped
    bad = native_call(native, "delete-dataflow")
    bad["params"]["DataFlowId"] = "df-business"
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(bad, "ws~native", native[1], read_only=False, tool_use_id="bad-delete")
    for target in ("dataflow", "export", "protocol", "fileset", "filesystem", "bucket"):
        native_wire(native, "delete-" + target)
        if target == "filesystem" and filesystem_not_found:
            call = native_call(native, "delete-filesystem-absence")
            ticket = gate.reserve(call, "ws~native", native[1], read_only=True, tool_use_id="own-fs-absence")
            gate.finish(
                ticket,
                {
                    "Code": "InvalidFileSystem.NotFound",
                    "RequestId": "own-fs-gone",
                    "HostId": "nas.cn-hangzhou.aliyuncs.com",
                    "statusCode": 404,
                },
                False,
                status=404,
                headers={"x-acs-request-id": "own-fs-gone"},
            )
            from scripts.web.e2e.resource_link_workflow.phases import known_original_native_nas_absence_read

            with gate.database(native[0]) as db:
                row = db.execute(
                    "SELECT a.*,g.kind,g.call FROM attempts a JOIN grants g "
                    "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?",
                    (ticket[1],),
                ).fetchone()
                assert row["state"] == "unknown"
                assert known_original_native_nas_absence_read(db, row)
        else:
            native_wire(native, "delete-" + target + "-absence")
    for side in ("filesystem", "export"):
        native_wire(native, "read-" + side + "-implicit-sg-absence")
        native_wire(native, "read-" + side + "-eni-absence")
        for target in ("vswitch", "vpc"):
            native_wire(native, "delete-" + side + "-" + target)
            native_wire(native, "delete-" + side + "-" + target + "-absence")
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "delete-dataflow")
    assert all(owner["state"] == "succeeded" for owner in gate.creations(native[0], ["native"]))


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "status",
        "code",
        "host",
        "header",
        "missing_request",
        "blank_request",
        "action",
        "version",
        "product",
        "target",
        "region",
        "page",
        "size",
        "extra",
        "body_status",
        "source_read",
    ],
)
def test_native_exact_filesystem_not_found_only_satisfies_cleanup(damage):
    call = {
        "product": "nas",
        "action": "DescribeFileSystems",
        "version": "2017-06-26",
        "region_id": "cn-hangzhou",
        "params": {"FileSystemId": "cpfs-own", "PageNumber": 1, "PageSize": 10},
    }
    raw = {
        "call": copy.deepcopy(call),
        "status": 404,
        "headers": {"x-acs-request-id": "own-fs-gone"},
        "body": {
            "Code": "InvalidFileSystem.NotFound",
            "RequestId": "own-fs-gone",
            "statusCode": 404,
            "HostId": "nas.cn-hangzhou.aliyuncs.com",
        },
    }
    ident = "delete-filesystem-absence"
    if damage == "status":
        raw["status"] = 403
    elif damage == "code":
        raw["body"]["Code"] = "Forbidden"
    elif damage == "host":
        raw["body"]["HostId"] = "nas.cn-beijing.aliyuncs.com"
    elif damage == "header":
        raw["headers"]["x-acs-request-id"] = "another-read"
    elif damage == "missing_request":
        raw["body"].pop("RequestId")
    elif damage == "blank_request":
        raw["body"]["RequestId"] = " "
    elif damage in {"action", "version", "product", "region"}:
        call[{"region": "region_id"}.get(damage, damage)] = "different"
    elif damage == "target":
        call["params"]["FileSystemId"] = "cpfs-business"
    elif damage == "page":
        call["params"]["PageNumber"] = 2
    elif damage == "size":
        call["params"]["PageSize"] = 100
    elif damage == "extra":
        call["params"]["VpcId"] = "unreviewed"
    elif damage == "body_status":
        raw["body"]["statusCode"] = 200
    elif damage == "source_read":
        ident = "read-filesystem-ready"
    assert gate.nas_native_ready(ident, call, raw) is (damage is None)


@pytest.mark.parametrize(
    "damage",
    [
        "200",
        "202",
        "403",
        "body",
        "missing_id",
        "duplicate_id",
        "whitespace_id",
        "long_id",
        "headers",
        "read",
        "action",
        "version",
        "region",
        "parameter",
    ],
)
def test_native_oss_delete_requires_exact_documented_wire(damage):
    call = {
        "product": "oss",
        "action": "DeleteBucket",
        "version": "2019-05-17",
        "region_id": "cn-hangzhou",
        "params": {"bucket": NATIVE_NAME},
    }
    raw = {"status": 204, "body": None, "headers": {"x-oss-request-id": "offline-delete"}}
    assert gate.nas_native_success(call, raw, "delete")
    kind = "delete"
    if damage.isdigit():
        raw["status"] = int(damage)
    elif damage == "body":
        raw["body"] = {"Code": "BucketNotEmpty"}
    elif damage == "missing_id":
        raw["headers"] = {}
    elif damage == "duplicate_id":
        raw["headers"]["X-Oss-Request-Id"] = "conflicting-id"
    elif damage == "whitespace_id":
        raw["headers"]["x-oss-request-id"] = "invalid id"
    elif damage == "long_id":
        raw["headers"]["x-oss-request-id"] = "x" * 257
    elif damage == "headers":
        raw["headers"] = []
    elif damage == "read":
        kind = "read"
    elif damage == "action":
        call["action"] = "GetBucketInfo"
    elif damage == "version":
        call["version"] = "other"
    elif damage == "region":
        call["region_id"] = "cn-beijing"
    else:
        call["params"]["foreign"] = "bucket"
    assert not gate.nas_native_success(call, raw, kind)


@pytest.mark.parametrize(
    "damage",
    [
        "version",
        "bandwidth",
        "missing_bandwidth",
        "client",
        "fileset",
        "export",
        "bucket",
        "not_empty",
    ],
)
def test_native_readiness_uses_real_same_row_fields(native, damage):
    native_filesystem(native)
    # Corrupt raw source before the next dependent write, not a parser-only assertion.
    ident = "read-filesystem-ready"
    next_ident = "create-fileset"
    if damage in {"fileset", "export", "bucket", "not_empty"}:
        for target in (
            "create-fileset",
            "read-fileset-ready",
            "precheck-protocol",
            "create-protocol",
            "read-protocol-ready",
        ):
            native_wire(native, target)
        ident, next_ident = "read-fileset-ready", "precheck-export"
        if damage in {"export", "bucket", "not_empty"}:
            for target in (
                "precheck-export",
                "create-export",
                "read-export-ready",
                "precheck-bucket-absent",
                "create-bucket",
                "read-bucket",
                "read-bucket-empty",
            ):
                native_wire(native, target)
            ident, next_ident = "read-export-ready", "precheck-dataflow"
            if damage == "bucket":
                ident = "read-bucket"
            elif damage == "not_empty":
                ident = "read-bucket-empty"
    with sqlite3.connect(native[0]) as db:
        raw = json.loads(db.execute("SELECT response FROM attempts WHERE grant_id=?", (ident,)).fetchone()[0])
        body = raw["body"]
        if damage == "version":
            body["FileSystems"]["FileSystem"][0]["Version"] = "2.3.3"
        elif damage == "bandwidth":
            body["FileSystems"]["FileSystem"][0]["Bandwidth"] = 600
        elif damage == "missing_bandwidth":
            del body["FileSystems"]["FileSystem"][0]["Bandwidth"]
        elif damage == "client":
            body["FileSystems"]["FileSystem"][0]["MountTargets"]["MountTarget"] = [{"ClientMasterNodes": {}}]
        elif damage == "fileset":
            body["Entries"]["Entrie"][0]["FsetId"] = "fset-business"
        elif damage == "export":
            body["ProtocolMountTarget"]["VpcId"] = "vpc-business"
        elif damage == "bucket":
            body["bucket_info"]["owner"]["id"] = "456"
        elif damage == "not_empty":
            body["contents"] = [{"Key": "business"}]
        db.execute("UPDATE attempts SET response=? WHERE grant_id=?", (json.dumps(raw), ident))
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, next_ident)


def test_native_tag_does_not_expand_ordinary_oss_grants():
    plan = gate.nas_native_plan(NATIVE_NAME)
    tagged = next(g for g in plan if g["id"] == "create-bucket")["call"]
    assert gate.nas_native_bucket(tagged)
    assert not gate.is_oss_bucket_create(tagged)
    for value in ("other=true", "cpfs-dataflow=false", "cpfs-dataflow=true&extra=true"):
        modified = copy.deepcopy(tagged)
        modified["params"]["x-oss-bucket-tagging"] = value
        assert not gate.nas_native_bucket(modified)


def test_native_partial_failure_can_cleanup_only_own_network(native):
    native_network(native)
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    for side in ("filesystem", "export"):
        native_wire(native, "read-" + side + "-implicit-sg-absence")
        native_wire(native, "read-" + side + "-eni-absence")
        for target in ("vswitch", "vpc"):
            native_wire(native, "delete-" + side + "-" + target)
            native_wire(native, "delete-" + side + "-" + target + "-absence")
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "delete-filesystem")  # no created response, no delete capability


@pytest.mark.parametrize("damage", ["status", "request_id", "header", "pagination", "identity", "pending_delete"])
def test_native_wire_and_cleanup_never_treat_errors_as_absence(native, damage):
    native_complete(native)
    if damage == "identity":
        with sqlite3.connect(native[0]) as db:
            db.execute("UPDATE attempts SET reference_scope='other' WHERE grant_id='identity'")
        with pytest.raises(gate.GateDeniedError):
            native_wire(native, "query-dataflow-source")
        return
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    native_wire(native, "delete-dataflow")
    body = native_body(native, "delete-dataflow-absence")
    if damage == "pagination":
        body["NextToken"] = "unread"
    elif damage == "pending_delete":
        body["DataFlowInfo"]["DataFlow"] = [{"DataFlowId": "df-own", "Status": "Deleting"}]
    elif damage == "request_id":
        body.pop("RequestId")
    ticket = native_wire(native, "delete-dataflow-absence", body=body, status=403 if damage == "status" else 200)
    if damage == "header":
        with sqlite3.connect(native[0]) as db:
            raw = json.loads(db.execute("SELECT response FROM attempts WHERE id=?", (ticket[1],)).fetchone()[0])
            raw["headers"] = {"x-acs-request-id": "other"}
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(raw), ticket[1]))
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "delete-export")


def test_native_ownership_export_keeps_original_call_and_rejects_unknown_precheck(native):
    native_filesystem(native)
    owners = gate.creations(native[0], ["native"])
    filesystem = next(owner for owner in owners if owner["resource"]["action"] == "CreateFileSystem")
    raw = filesystem["response"]
    deletion = native[2]["delete-filesystem"]
    assert (
        gate.response_identifier(native[2]["create-filesystem"]["call"], raw, deletion["response_path"]) == "cpfs-own"
    )
    raw = copy.deepcopy(raw)
    raw["call"]["region_id"] = "cn-beijing"
    assert gate.response_identifier(native[2]["create-filesystem"]["call"], raw, deletion["response_path"]) is None


def test_native_implicit_members_require_precreate_baseline_and_original_signed_inventory(native):
    native_complete(native)
    with sqlite3.connect(native[0]) as db:
        record = db.execute(
            "SELECT id,response FROM attempts WHERE grant_id='read-filesystem-implicit-members' "
            "ORDER BY created DESC LIMIT 1",
        ).fetchone()
        raw = json.loads(record[1])
        raw["body"]["SecurityGroups"]["SecurityGroup"] = [{"SecurityGroupId": "sg-own", "VpcId": "vpc-fs"}]
        raw["body"]["TotalCount"] = 1
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(raw), record[0]))
    owners = gate.creations(native[0], ["native"])
    child = next(owner for owner in owners if owner["resource"]["id"] == "sg-own")
    assert child["state"] == "succeeded"
    assert child["parent_attempt_id"]
    assert child["resource"]["vpc_id"] == "vpc-fs"
    assert child["response"]["call"]["action"] == "DescribeSecurityGroups"
    with sqlite3.connect(native[0]) as db:
        db.execute("UPDATE attempts SET session='ws~other' WHERE id=?", (record[0],))
    owners = gate.creations(native[0], ["native"])
    assert any(owner["state"] == "unknown" and owner["resource"]["id"] is None for owner in owners)


def test_native_implicit_post_inventory_before_child_creation_is_unresolved(native):
    native_complete(native)
    with sqlite3.connect(native[0]) as db:
        source = db.execute(
            "SELECT id FROM attempts WHERE grant_id='read-export-implicit-members' ORDER BY created DESC LIMIT 1",
        ).fetchone()[0]
        created = db.execute("SELECT created FROM attempts WHERE grant_id='create-filesystem'").fetchone()[0]
        db.execute("UPDATE attempts SET created=? WHERE id=?", (created + 0.000001, source))
    assert any(
        owner["state"] == "unknown" and owner["resource"]["id"] is None
        for owner in gate.creations(native[0], ["native"])
    )


def native_partial_cleanup(native):
    native_empty_export(native, protocol="NFS")  # No DataFlow or post-create member snapshots.
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    for target in ("export", "protocol", "fileset", "filesystem", "bucket"):
        native_wire(native, "delete-" + target)
        native_wire(native, "delete-" + target + "-absence")
    for side in ("filesystem", "export"):
        for suffix in ("implicit-sg-absence", "eni-absence"):
            native_wire(native, "read-" + side + "-" + suffix)
        for target in ("vswitch", "vpc"):
            native_wire(native, "delete-" + side + "-" + target)
            native_wire(native, "delete-" + side + "-" + target + "-absence")


@pytest.mark.parametrize("renewed", [False, True])
def test_native_missing_derived_inventory_resolves_only_after_real_network_cleanup(native, renewed):
    native_partial_cleanup(native)
    if renewed:
        native_signer_review(native)
        native_renew(rotated_native(native))
    before = native[0].read_bytes()
    owners = gate.creations(native[0], ["native"])
    assert len(owners) == 9
    assert all(owner["state"] == "succeeded" and owner["resource"]["id"] for owner in owners)
    assert native[0].read_bytes() == before  # No rewrite of unknown calls, IDs, quotas or history.


@pytest.mark.parametrize("side", ["filesystem", "export"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing_delete",
        "unknown_delete",
        "missing_absence",
        "early_absence",
        "present_vpc",
        "foreign_session",
        "foreign_signer",
        "foreign_scope",
        "foreign_vpc",
        "missing_tool",
        "dirty_sg",
        "dirty_eni",
        "pagination",
        "early_inventory",
        "missing_parent_absence",
    ],
)
def test_native_missing_derived_inventory_stays_unresolved_without_real_cleanup(native, side, damage):
    native_partial_cleanup(native)
    ident = "delete-" + side + "-vpc-absence"
    if damage in {"missing_delete", "unknown_delete"}:
        ident = "delete-" + side + "-vpc"
    elif damage in {"dirty_sg", "pagination", "early_inventory"}:
        ident = "read-" + side + "-implicit-sg-absence"
    elif damage == "dirty_eni":
        ident = "read-" + side + "-eni-absence"
    elif damage == "missing_parent_absence":
        ident = "delete-filesystem-absence" if side == "filesystem" else "delete-export-absence"
    with gate.database(native[0]) as db:
        row = dict(db.execute("SELECT * FROM attempts WHERE grant_id=?", (ident,)).fetchone())
        raw = json.loads(row["response"])
        if damage.startswith("missing_") and damage != "missing_tool":
            db.execute("DELETE FROM attempts WHERE id=?", (row["id"],))
        elif damage == "unknown_delete":
            db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (row["id"],))
        elif damage in {"early_absence", "early_inventory"}:
            db.execute("UPDATE attempts SET created=0 WHERE id=?", (row["id"],))
        elif damage.startswith("foreign_") and damage != "foreign_vpc":
            column = {"foreign_session": "session", "foreign_signer": "credential", "foreign_scope": "reference_scope"}[
                damage
            ]
            db.execute(f"UPDATE attempts SET {column}='foreign' WHERE id=?", (row["id"],))
        elif damage == "missing_tool":
            db.execute("UPDATE attempts SET tool_use_id='' WHERE id=?", (row["id"],))
        else:
            if damage == "foreign_vpc":
                raw["call"]["params"]["VpcId"] = "vpc-foreign"
            elif damage == "present_vpc":
                raw["body"]["Vpcs"]["Vpc"] = [{"VpcId": "vpc-live"}]
                raw["body"]["TotalCount"] = 1
            elif damage == "pagination":
                raw["body"]["NextToken"] = "more"
            else:
                collection, member = (
                    ("SecurityGroups", "SecurityGroup")
                    if damage == "dirty_sg"
                    else ("NetworkInterfaceSets", "NetworkInterfaceSet")
                )
                raw["body"][collection][member] = [{"Id": "still-live"}]
                raw["body"]["TotalCount"] = 1
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(raw), row["id"]))
    owners = gate.creations(native[0], ["native"])
    assert any(
        owner["state"] == "unknown" and owner["resource"]["id"] is None and ":" + side + ":" in owner["attempt_id"]
        for owner in owners
    )


def test_native_export_baseline_can_follow_cpfs_but_must_precede_its_own_nas_use(native):
    native_complete(native)
    with gate.database(native[0]) as db:
        cpfs = db.execute("SELECT created FROM attempts WHERE grant_id='create-filesystem'").fetchone()[0]
        export = db.execute("SELECT created FROM attempts WHERE grant_id='create-export'").fetchone()[0]
        baseline = db.execute(
            "SELECT id FROM attempts WHERE grant_id='read-export-implicit-members' ORDER BY created LIMIT 1"
        ).fetchone()[0]
        db.execute("UPDATE attempts SET created=? WHERE id=?", ((cpfs + export) / 2, baseline))
    assert all(owner["state"] == "succeeded" for owner in gate.creations(native[0], ["native"]))
    with gate.database(native[0]) as db:
        db.execute("UPDATE attempts SET created=? WHERE id=?", (export + 0.000001, baseline))
    assert any(owner["state"] == "unknown" for owner in gate.creations(native[0], ["native"]))


def test_native_deleted_parent_requires_actual_later_child_absence(native):
    native_complete(native)
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    native_wire(native, "delete-dataflow")
    native_wire(native, "delete-dataflow-absence")
    with sqlite3.connect(native[0]) as db:
        created = db.execute("SELECT created FROM attempts WHERE grant_id='delete-dataflow'").fetchone()[0]
        db.execute("UPDATE attempts SET created=? WHERE grant_id='delete-dataflow-absence'", (created - 1,))
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "delete-export")


@pytest.mark.parametrize("damage", ["null", "list", "missing", "wrong_path"])
def test_native_malformed_original_detail_is_denied_without_crashing(native, damage):
    native_filesystem(native)
    with sqlite3.connect(native[0]) as db:
        raw = json.loads(
            db.execute(
                "SELECT response FROM attempts WHERE grant_id='read-filesystem-ready'",
            ).fetchone()[0]
        )
        value = raw["body"]["FileSystems"]["FileSystem"][0]
        if damage == "null":
            value["VswIds"] = None
        elif damage == "list":
            value["VswIds"] = []
        elif damage == "missing":
            value.pop("VswIds")
        else:
            value["VswIds"] = {"OtherId": ["vsw-fs"]}
        db.execute("UPDATE attempts SET response=? WHERE grant_id='read-filesystem-ready'", (json.dumps(raw),))
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "create-fileset")


def native_bandwidth_omission_candidate():
    """Independent packet delta: no general optional-parameter or token override."""
    plan = gate.nas_native_plan(NATIVE_NAME)
    for grant in plan:
        if grant["id"] in {
            "create-filesystem-vpc",
            "create-filesystem-vswitch",
            "precheck-filesystem",
            "create-filesystem",
        }:
            grant["call"]["params"]["ClientToken"] += "-bwo1"
        if grant["id"] in {"precheck-filesystem", "create-filesystem"}:
            del grant["call"]["params"]["Bandwidth"]
    return plan


def test_native_bandwidth_omission_candidate_validates_without_changing_legacy():
    candidate = native_bandwidth_omission_candidate()
    assert len(candidate) == 53
    assert sum(grant["quota"] for grant in candidate) == 487
    assert gate.validate_nas_native_plan(candidate)
    gate.validate_grants(candidate, require_read_policy=True)
    assert gate.nas_native_plan(NATIVE_NAME, variant="bandwidth_omission_bwo1") == candidate
    legacy = gate.nas_native_plan(NATIVE_NAME)
    assert gate.validate_nas_native_plan(legacy)
    for grant in legacy:
        if grant["id"] in {"precheck-filesystem", "create-filesystem"}:
            assert grant["call"]["params"]["Bandwidth"] == 720
        assert not grant["call"]["params"].get("ClientToken", "").endswith("-bwo1")


@pytest.mark.parametrize(
    "damage",
    [
        "precheck_bandwidth",
        "create_bandwidth",
        "precheck_token",
        "create_token",
        "vpc_token",
        "vswitch_token",
        "unreviewed_suffix",
        "export_token",
        "capacity",
        "dryrun",
        "quota",
        "cleanup_quota",
        "cleanup_policy",
        "cleanup_binding",
        "extra_role",
        "missing_role",
    ],
)
def test_native_bandwidth_omission_candidate_rejects_mixed_or_expanded_graph(damage):
    plan = native_bandwidth_omission_candidate()
    roles = {grant["id"]: grant for grant in plan}
    if damage in {"precheck_bandwidth", "create_bandwidth"}:
        role = "precheck-filesystem" if damage == "precheck_bandwidth" else "create-filesystem"
        roles[role]["call"]["params"]["Bandwidth"] = 720
    elif damage in {"precheck_token", "create_token", "vpc_token", "vswitch_token"}:
        role = {
            "precheck_token": "precheck-filesystem",
            "create_token": "create-filesystem",
            "vpc_token": "create-filesystem-vpc",
            "vswitch_token": "create-filesystem-vswitch",
        }[damage]
        roles[role]["call"]["params"]["ClientToken"] = roles[role]["call"]["params"]["ClientToken"][:-5]
    elif damage == "unreviewed_suffix":
        for grant in plan:
            params = grant["call"]["params"]
            if params.get("ClientToken", "").endswith("-bwo1"):
                params["ClientToken"] = params["ClientToken"][:-1] + "2"
    elif damage == "export_token":
        roles["create-export-vpc"]["call"]["params"]["ClientToken"] += "-bwo1"
    elif damage == "capacity":
        roles["create-filesystem"]["call"]["params"]["Capacity"] = 7200
    elif damage == "dryrun":
        roles["precheck-filesystem"]["call"]["params"]["DryRun"] = "true"
    elif damage in {"quota", "cleanup_quota"}:
        roles["identity" if damage == "quota" else "delete-filesystem-absence"]["quota"] += 1
    elif damage == "cleanup_policy":
        roles["delete-filesystem-absence"]["read_policy"]["required_cleanup"] = False
    elif damage == "cleanup_binding":
        roles["delete-filesystem"]["response_path"]["bindings"]["FileSystemId"] = "create-protocol"
    elif damage == "extra_role":
        plan.append(copy.deepcopy(roles["identity"]))
    elif damage == "missing_role":
        plan.remove(roles["delete-filesystem-absence"])
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(plan, require_read_policy=True)


@pytest.mark.parametrize("variant", ["bandwidth_omission_bwo2", "", None, True])
def test_native_plan_rejects_unreviewed_variants(variant):
    with pytest.raises(gate.GateDeniedError):
        gate.nas_native_plan(NATIVE_NAME, variant=variant)


@pytest.mark.parametrize("role", ["precheck-filesystem", "create-filesystem"])
@pytest.mark.parametrize("damage", ["bandwidth", "token"])
def test_native_installed_variant_rejects_other_variant_wire_without_consuming_quota(native, role, damage):
    native_network(native)
    if role == "create-filesystem":
        native_wire(native, "precheck-filesystem")
    call = native_call(native, role)
    params = call["params"]
    if damage == "bandwidth":
        if "Bandwidth" in params:
            del params["Bandwidth"]
        else:
            params["Bandwidth"] = 720
    elif params["ClientToken"].endswith("-bwo1"):
        params["ClientToken"] = params["ClientToken"][:-5]
    else:
        params["ClientToken"] += "-bwo1"
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(call, "ws~native", native[1], read_only=False, tool_use_id="wrong-variant")
    ticket = native_wire(native, role)
    with sqlite3.connect(native[0]) as db:
        response = json.loads(db.execute("SELECT response FROM attempts WHERE id=?", (ticket[1],)).fetchone()[0])
    assert response["call"] == native_call(native, role)
    owners = gate.creations(native[0], ["native"])
    if role == "create-filesystem":
        filesystem = next(owner for owner in owners if owner["resource"]["action"] == "CreateFileSystem")
        assert filesystem["response"]["call"] == native_call(native, role)


def native_signer_review(native):
    """Simulate an independent OPS append; production code never writes this review."""
    path = native[0]
    with gate.database(path) as db:
        active = db.execute("SELECT * FROM active").fetchone()
        original = db.execute("SELECT * FROM grants WHERE id='identity'").fetchone()
        rows = db.execute("SELECT * FROM attempts ORDER BY created,rowid").fetchall()
        preserved = []
        for grant in db.execute("SELECT * FROM grants ORDER BY id"):
            definition = {k: v for k, v in dict(grant).items() if k != "used"}
            preserved.append(
                {"id": grant["id"], "definition_sha256": gate.nas_native_snapshot(definition), "used": grant["used"]}
            )
        review = {
            "kind": "ops_native_signer_continuation_review",
            "approved": True,
            "additional_identity_reads": 1,
            **{k: active[k] for k in ("scope", "session", "account")},
            "authority": original["authority"],
            "grant_id": "ops-native-identity-renewal",
            "identity_call": json.loads(original["call"]),
            "original_identity_attempt": next(r["id"] for r in rows if r["grant_id"] == "identity"),
            "preserved_attempts": [{"id": r["id"], "sha256": gate.nas_native_snapshot(r)} for r in rows],
            "preserved_grants": preserved,
        }
        file = path.parent / "native-signer-review.json"
        file.write_text(gate.canonical(review), encoding="utf-8")
        file.chmod(0o600)
        db.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,'identity',?,1,0,?)",
            (active["scope"], review["grant_id"], original["call"], original["authority"]),
        )
        db.execute("INSERT INTO authority_limits VALUES(?,?,0)", (original["authority"], review["grant_id"]))
        db.execute(
            "INSERT INTO cleanup_identity_reviews VALUES(?,?,?,?,?,?,?)",
            (
                active["scope"],
                review["grant_id"],
                active["session"],
                active["account"],
                original["authority"],
                str(file),
                gate.hashlib.sha256(file.read_bytes()).hexdigest(),
            ),
        )
    return file, review, [dict(row) for row in rows]


def native_renew(native, *, account="123", status=200):
    ticket = gate.reserve(
        native_call(native, "identity"), "ws~native", native[1], read_only=True, tool_use_id="tool-reviewed-renewal"
    )
    gate.finish(ticket, {"AccountId": account, "RequestId": "offline-renewal"}, status == 200, status=status)
    return ticket


def rotated_native(native):
    from iac_code.services.providers.aliyun import AliyunCredential

    return (
        native[0],
        AliyunCredential(
            mode="OAuth",
            access_key_id="fake-new",
            access_key_secret="fake-new-secret",
            sts_token="fake-new-sts",
            sts_expiration=int(time.time()) + 1000,
        ),
        native[2],
    )


def test_native_rotation_without_review_fails_before_consuming_original_quota(native):
    native_filesystem(native)
    rotated = rotated_native(native)
    with gate.database(native[0]) as db:
        before = [dict(r) for r in db.execute("SELECT * FROM grants")]
    with pytest.raises(gate.GateDeniedError):
        native_wire(rotated, "read-filesystem-ready")
    with pytest.raises(gate.GateDeniedError):
        native_renew(rotated)
    with gate.database(native[0]) as db:
        assert before == [dict(r) for r in db.execute("SELECT * FROM grants")]


def test_native_reviewed_rotation_preserves_original_generation_and_reverse_cleanup(native, monkeypatch):
    native_filesystem(native)
    file, review, rows = native_signer_review(native)
    rotated = rotated_native(native)
    with pytest.raises(gate.GateDeniedError):
        native_wire(rotated, "read-filesystem-ready")  # Review cannot replace actual signed identity.
    native_renew(rotated)
    # Same original IDs, remaining grants and actual healthy read; no recreation.
    clock = [rotated[1].sts_expiration - 500.0]

    def tick():
        clock[0] += 0.01
        return clock[0]

    monkeypatch.setattr(time, "time", tick)
    native_wire(rotated, "read-filesystem-ready")
    for ident in (
        "create-fileset",
        "read-fileset-ready",
        "precheck-protocol",
        "create-protocol",
        "read-protocol-ready",
        "precheck-export",
        "create-export",
        "read-export-ready",
        "precheck-bucket-absent",
        "create-bucket",
        "read-bucket",
        "read-bucket-empty",
        "precheck-dataflow",
        "create-dataflow",
        "read-filesystem-implicit-members",
        "read-export-implicit-members",
        "query-dataflow-source",
    ):
        native_wire(rotated, ident)
    source = gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")
    assert source["tool_use_id"] == "tool-query-dataflow-source"
    with gate.database(native[0]) as db:
        original = db.execute("SELECT * FROM attempts WHERE id=?", (rows[0]["id"],)).fetchone()
        assert source["reference_scope"] != original["reference_scope"]
        assert db.execute("SELECT used FROM grants WHERE id='identity'").fetchone()[0] == 1
        assert db.execute("SELECT used FROM grants WHERE id='create-filesystem'").fetchone()[0] == 1
        assert db.execute("SELECT used FROM grants WHERE id='read-filesystem-ready'").fetchone()[0] == 2
        for row in rows:
            assert dict(db.execute("SELECT * FROM attempts WHERE id=?", (row["id"],)).fetchone()) == row
    with pytest.raises(gate.GateDeniedError):
        native_renew(rotated)  # Exactly one independently reviewed renewal.
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "read-filesystem-ready")  # Old signer cannot resume.
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    for ident in (
        "delete-dataflow",
        "delete-export",
        "delete-protocol",
        "delete-fileset",
        "delete-filesystem",
        "delete-bucket",
    ):
        native_wire(rotated, ident)
        native_wire(rotated, ident + "-absence")
    for side in ("filesystem", "export"):
        for suffix in ("implicit-sg-absence", "eni-absence"):
            native_wire(rotated, "read-" + side + "-" + suffix)
        for kind in ("vswitch", "vpc"):
            ident = "delete-" + side + "-" + kind
            native_wire(rotated, ident)
            native_wire(rotated, ident + "-absence")
    assert file.read_text(encoding="utf-8") == gate.canonical(review)


@pytest.mark.parametrize(
    "damage",
    [
        "sha",
        "unapproved",
        "account",
        "session",
        "authority",
        "original_wire",
        "missing_attempt",
        "expanded_quota",
        "refill",
        "extra_grant",
        "failed_identity",
        "wrong_identity",
        "reference_scope",
        "unknown_identity",
        "unreviewed_old_attempt",
        "malformed_review",
    ],
)
def test_native_signer_continuation_rejects_tampering_and_unproven_identity(native, damage):
    native_filesystem(native)
    file, review, rows = native_signer_review(native)
    rotated = rotated_native(native)
    if damage in {"unapproved", "account", "session", "authority", "missing_attempt"}:
        if damage == "unapproved":
            review["approved"] = False
        elif damage == "missing_attempt":
            review["preserved_attempts"].pop()
        else:
            review[damage] = "foreign"
        file.write_text(gate.canonical(review), encoding="utf-8")
        with gate.database(native[0]) as db:
            db.execute(
                "UPDATE cleanup_identity_reviews SET evidence_sha256=?",
                (gate.hashlib.sha256(file.read_bytes()).hexdigest(),),
            )
    elif damage == "malformed_review":
        file.write_text("[]", encoding="utf-8")
        with gate.database(native[0]) as db:
            db.execute(
                "UPDATE cleanup_identity_reviews SET evidence_sha256=?",
                (gate.hashlib.sha256(file.read_bytes()).hexdigest(),),
            )
    elif damage == "sha":
        file.write_text("{}", encoding="utf-8")
    elif damage in {"original_wire", "expanded_quota", "refill", "extra_grant"}:
        with gate.database(native[0]) as db:
            if damage == "original_wire":
                db.execute("UPDATE attempts SET response='{}' WHERE id=?", (rows[-1]["id"],))
            elif damage == "expanded_quota":
                db.execute("UPDATE grants SET quota=quota+1 WHERE id='read-filesystem-ready'")
            elif damage == "refill":
                db.execute("UPDATE grants SET used=0 WHERE id='create-filesystem'")
                db.execute("UPDATE authority_limits SET used=0 WHERE id='create-filesystem'")
            else:
                db.execute("INSERT INTO grants(scope,id) VALUES('native:QUERY','unreviewed')")
    elif damage in {"failed_identity", "wrong_identity", "unknown_identity"}:
        if damage == "unknown_identity":
            gate.reserve(
                native_call(native, "identity"), "ws~native", rotated[1], read_only=True, tool_use_id="tool-unknown"
            )
        else:
            native_renew(
                rotated,
                account="foreign" if damage == "wrong_identity" else "123",
                status=403 if damage == "failed_identity" else 200,
            )
    else:
        native_renew(rotated)
        with gate.database(native[0]) as db:
            if damage == "reference_scope":
                db.execute("UPDATE attempts SET reference_scope='foreign' WHERE grant_id LIKE 'ops-native-%'")
            else:
                columns = list(rows[-1])
                value = {**rows[-1], "id": "unreviewed-old"}
                db.execute(
                    "INSERT INTO attempts(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                    tuple(value[k] for k in columns),
                )
    with pytest.raises(gate.GateDeniedError):
        native_wire(rotated, "read-filesystem-ready")


def test_native_normal_oauth_refresh_precedes_wire_gate_and_requires_reviewed_identity(native, monkeypatch):
    from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
    from iac_code.services.providers.aliyun_oauth import OAuthStsCredentials

    now = int(time.time())
    credential = AliyunCredential(
        mode="OAuth",
        oauth_site_type="CN",
        oauth_access_token="fake-access",
        oauth_refresh_token="fake-refresh",
        oauth_access_token_expire=now + 10000,
        access_key_id="fake-old-ak",
        access_key_secret="fake-old-sk",
        sts_token="fake-old-sts",
        sts_expiration=now + 400,
    )
    original = (native[0], credential, native[2])
    native_filesystem(original)
    native_signer_review(original)
    expired = copy.copy(credential)
    expired.sts_expiration = now + 59
    with pytest.raises(gate.GateDeniedError):
        native_wire((native[0], expired, native[2]), "read-filesystem-ready")
    saved = []

    class Client:
        def exchange_access_token_for_sts(self, token):
            assert token == "fake-access"
            return OAuthStsCredentials("fake-new-ak", "fake-new-sk", "fake-new-sts", now + 5000)

    monkeypatch.setattr(AliyunCredentials, "save", saved.append)
    # The normal provider refreshes with 120s skew, before the gate's 60s guard.
    refreshed = AliyunCredentials.refresh_oauth_if_needed(credential, oauth_client=Client(), now=now + 281)
    assert saved == [refreshed] and refreshed.access_key_id == "fake-new-ak"
    renewed = (native[0], refreshed, native[2])
    with pytest.raises(gate.GateDeniedError):
        native_wire(renewed, "read-filesystem-ready")
    native_renew(renewed)
    monkeypatch.setattr(time, "time", lambda: now + 282)
    native_wire(renewed, "read-filesystem-ready")


def test_native_renewal_cannot_replay_pre_renewal_navigation_source(native, monkeypatch):
    native_complete(native)
    native_wire(native, "query-dataflow-source")
    native_signer_review(native)
    rotated = rotated_native(native)
    native_renew(rotated)
    with pytest.raises(gate.GateDeniedError):
        gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")
    monkeypatch.setattr(time, "time", lambda: rotated[1].sts_expiration - 500)
    native_wire(rotated, "query-dataflow-source")
    source = gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")
    assert source["tool_use_id"] == "tool-query-dataflow-source"


def native_review_file(native, name, value):
    file = native[0].parent / name
    file.write_text(gate.canonical(value), encoding="utf-8")
    file.chmod(0o600)
    return {"evidence": str(file.resolve()), "evidence_sha256": gate.hashlib.sha256(file.read_bytes()).hexdigest()}


def native_prefix(db):
    grants = []
    for row in db.execute("SELECT * FROM grants ORDER BY id"):
        grants.append(
            {
                "id": row["id"],
                "used": row["used"],
                "definition_sha256": gate.nas_native_snapshot({k: v for k, v in dict(row).items() if k != "used"}),
            }
        )
    rows = [
        {"id": row["id"], "sha256": gate.nas_native_snapshot(row)}
        for row in db.execute("SELECT * FROM attempts ORDER BY created,rowid")
    ]
    return {"preserved_grants": grants, "preserved_attempts": rows}


def native_permit_success(native, *, account="123", status=200, prepared=False):
    if not prepared:
        native_filesystem(native)
    file, review, originals = native_signer_review(native)
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    marker = native_review_file(
        native,
        "identity-marker.json",
        {
            "state": "may_have_submitted",
            "scope": "native:QUERY",
            "session_id": "ws~native",
            "stage": "identity",
            "proposal_sha256": "a" * 64,
            "review_sha256": "b" * 64,
        },
    )
    gate.arm_native_identity_recovery(
        native[0],
        slot="0",
        scope="native:QUERY",
        session="ws~native",
        account="123",
        grant_id=review["grant_id"],
        authority="native:QUERY",
        call=review["identity_call"],
        proposal_sha256="a" * 64,
        review_sha256="b" * 64,
        original_review_sha256=gate.hashlib.sha256(file.read_bytes()).hexdigest(),
        marker_path=marker["evidence"],
    )
    rotated = rotated_native(native)
    ticket = native_renew(rotated, account=account, status=status)
    with gate.database(native[0]) as db:
        permit = dict(db.execute("SELECT * FROM native_identity_recovery_permits").fetchone())
        signed = db.execute("SELECT * FROM attempts WHERE id=?", (ticket[1],)).fetchone()
        handoff = {
            "kind": "ops_native_continuation_handoff",
            "approved": True,
            "new_creation_quota": 0,
            "scope": "native:QUERY",
            "session": "ws~native",
            "account": "123",
            "slot": "0",
            "authority": "native:QUERY",
            "grant_id": review["grant_id"],
            "phases": ["execute", "cleanup"],
            "permit_sha256": gate.nas_native_snapshot(permit),
            "original_review_sha256": permit["original_review_sha256"],
            "marker_sha256": marker["evidence_sha256"],
            "identity_attempt": {"id": signed["id"], "sha256": gate.nas_native_snapshot(signed)},
            "grant_ids": sorted(g["id"] for g in native[2].values() if g["kind"] != "identity"),
            **native_prefix(db),
        }
    return rotated, handoff, permit, originals


def native_rotation_request(native, sequence):
    with gate.database(native[0]) as db:
        active = db.execute("SELECT * FROM active").fetchone()
        prior = gate.native_consumed_handoff(db, active)
        key = (
            "native:QUERY:native-handoff"
            if sequence == 1
            else ("native:QUERY:native-renewal:" + str(sequence - 1).zfill(6))
        )
        previous = db.execute("SELECT * FROM cleanup_identity_reviews WHERE scope=?", (key,)).fetchone()
        value = {
            "kind": "ops_native_signer_renewal_review",
            "approved": True,
            "new_creation_quota": 0,
            "scope": "native:QUERY",
            "session": "ws~native",
            "account": "123",
            "slot": "0",
            "authority": "native:QUERY",
            "sequence": sequence,
            "additional_identity_reads": 1,
            "grant_id": "ops-native-identity-renewal-" + str(sequence),
            "identity_call": native_call(native, "identity"),
            "previous_review_sha256": previous["evidence_sha256"],
            "previous_identity": {
                "id": prior[1]["id"],
                "sha256": gate.nas_native_snapshot(
                    db.execute("SELECT * FROM attempts WHERE id=?", (prior[1]["id"],)).fetchone()
                ),
            },
            **native_prefix(db),
        }
    return value


def native_next_credential(native, number):
    from iac_code.services.providers.aliyun import AliyunCredential

    return (
        native[0],
        AliyunCredential(
            mode="OAuth",
            access_key_id="fake-next-" + str(number),
            access_key_secret="fake-next-secret-" + str(number),
            sts_token="fake-next-token-" + str(number),
            sts_expiration=int(time.time()) + 5000,
        ),
        native[2],
    )


def test_native_consumed_permit_remains_closed_until_independent_handoff(native):
    rotated, handoff, permit, _ = native_permit_success(native)
    with gate.database(native[0]) as db:
        before = native_prefix(db)
    assert "No active reviewed" in gate.execution_instructions("ws~native", "")
    for item in ("read-filesystem-ready", "delete-filesystem"):
        with pytest.raises(gate.GateDeniedError):
            native_wire(rotated, item)
    with pytest.raises(gate.GateDeniedError):
        native_renew(rotated)
    with gate.database(native[0]) as db:
        assert native_prefix(db) == before
        assert dict(db.execute("SELECT * FROM native_identity_recovery_permits").fetchone()) == permit
    gate.register_native_continuation_handoff(native[0], **native_review_file(native, "handoff.json", handoff))
    assert '"DeleteFileSystem"' in gate.execution_instructions("ws~native", "")
    assert '"GetCallerIdentity"' not in gate.execution_instructions("ws~native", "")


@pytest.mark.parametrize(
    "damage",
    [
        "unapproved",
        "account",
        "scope",
        "session",
        "authority",
        "slot",
        "permit_sha256",
        "marker_sha256",
        "original_review_sha256",
        "identity_attempt",
        "missing_attempt",
        "missing_grant",
        "other_api",
        "unknown",
        "failed",
        "wrong_account",
        "raw_changed",
        "quota",
        "refill",
        "extra_grant",
        "marker",
    ],
)
def test_native_handoff_registration_rejects_unproven_or_expanded_authority(native, damage):
    rotated, value, _, _ = native_permit_success(
        native,
        account="foreign" if damage == "wrong_account" else "123",
        status=403 if damage == "failed" else 200,
    )
    if damage == "unapproved":
        value["approved"] = False
    elif damage in {
        "account",
        "scope",
        "session",
        "authority",
        "slot",
        "permit_sha256",
        "marker_sha256",
        "original_review_sha256",
    }:
        value[damage] = "foreign"
    elif damage == "identity_attempt":
        value["identity_attempt"]["sha256"] = "0" * 64
    elif damage == "missing_attempt":
        value["preserved_attempts"].pop()
    elif damage == "missing_grant":
        value["preserved_grants"].pop()
    elif damage == "other_api":
        value["grant_ids"].append("unreviewed")
    elif damage in {"unknown", "raw_changed", "quota", "refill", "extra_grant", "marker"}:
        with gate.database(native[0]) as db:
            if damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='ops-native-identity-renewal'")
            elif damage == "raw_changed":
                db.execute("UPDATE attempts SET response='{}' WHERE grant_id='ops-native-identity-renewal'")
            elif damage == "quota":
                db.execute("UPDATE grants SET quota=2 WHERE id='create-filesystem'")
            elif damage == "refill":
                db.execute("UPDATE grants SET used=0 WHERE id='create-filesystem'")
                db.execute("UPDATE authority_limits SET used=0 WHERE id='create-filesystem'")
            elif damage == "extra_grant":
                db.execute("INSERT INTO grants(scope,id) VALUES('native:QUERY','unreviewed')")
            else:
                marker = db.execute("SELECT marker_path FROM native_identity_recovery_permits").fetchone()[0]
                Path(marker).write_text("{}", encoding="utf-8")
    kwargs = native_review_file(native, "handoff-bad.json", value)
    with pytest.raises((gate.GateDeniedError, sqlite3.Error, KeyError)):
        gate.register_native_continuation_handoff(native[0], **kwargs)
    with gate.database(native[0]) as db:
        assert not db.execute("SELECT 1 FROM cleanup_identity_reviews WHERE scope LIKE '%:native-handoff'").fetchone()
    with pytest.raises(gate.GateDeniedError):
        native_wire(rotated, "read-filesystem-ready")


@pytest.mark.parametrize("restart", [False, True])
def test_native_same_signer_restart_requires_fresh_reviewed_identity_not_scope_prefix(native, monkeypatch, restart):
    from collections import OrderedDict

    from iac_code.tools.cloud.aliyun import resource_reference_extractors as refs

    rotated, handoff, _, originals = native_permit_success(native)
    gate.register_native_continuation_handoff(native[0], **native_review_file(native, "handoff.json", handoff))
    with gate.database(native[0]) as db:
        db.execute("UPDATE active SET phase='execute'")
    if restart:
        monkeypatch.setattr(refs, "_SCOPES", OrderedDict())
        with pytest.raises(gate.GateDeniedError):
            native_wire(rotated, "read-filesystem-ready")
    review = native_rotation_request(native, 1)
    gate.register_native_signer_renewal(native[0], **native_review_file(native, "restart-renewal.json", review))
    native_renew(rotated)
    observed = time.time()
    monkeypatch.setattr(time, "time", lambda: observed + 31)
    if restart:
        with gate.database(native[0]) as db:
            active = db.execute("SELECT * FROM active").fetchone()
            assert gate.native_consumed_handoff(db, active)[0]["used"] == 1
        native_wire(rotated, "read-filesystem-ready")
        with gate.database(native[0]) as db:
            for original in originals:
                assert dict(db.execute("SELECT * FROM attempts WHERE id=?", (original["id"],)).fetchone()) == original
    else:
        with pytest.raises(gate.GateDeniedError):
            native_wire(rotated, "read-filesystem-ready")  # Same signer AND scope is not a fresh binding.


def test_native_handoff_renewal_chain_preserves_raw_owners_and_finishes_graph_and_cleanup(native, monkeypatch):
    rotated, handoff, permit, originals = native_permit_success(native)
    # The real controller may mark its original identity marker submitted after STS.
    marker = Path(permit["marker_path"])
    body = json.loads(marker.read_text(encoding="utf-8"))
    body["state"] = "submitted"
    marker.write_text(gate.canonical(body), encoding="utf-8")
    handoff["marker_sha256"] = gate.hashlib.sha256(marker.read_bytes()).hexdigest()
    gate.register_native_continuation_handoff(native[0], **native_review_file(native, "handoff.json", handoff))
    # Only the independently reviewed controller changes phase, not admission.
    with gate.database(native[0]) as db:
        assert db.execute("SELECT phase FROM active").fetchone()[0] == "cleanup"
        db.execute("UPDATE active SET phase='execute'")
        raw_identity = dict(db.execute("SELECT * FROM attempts WHERE id=?", (permit["attempt_id"],)).fetchone())
        assert "body" not in json.loads(raw_identity["response"])
    clock = [time.time() + 200]

    def tick():
        clock[0] += 0.01
        return clock[0]

    monkeypatch.setattr(time, "time", tick)
    for number in (1, 2):
        newer = native_next_credential(native, number)
        with pytest.raises(gate.GateDeniedError):
            native_wire(newer, "read-filesystem-ready")
        review = native_rotation_request(native, number)
        gate.register_native_signer_renewal(
            native[0],
            **native_review_file(native, "renewal-" + str(number) + ".json", review),
        )
        assert '"GetCallerIdentity"' in gate.execution_instructions("ws~native", "")
        assert '"DescribeFileSystems"' not in gate.execution_instructions("ws~native", "")
        with pytest.raises(gate.GateDeniedError):
            native_wire(rotated, "read-filesystem-ready")
        native_renew(newer)
        with pytest.raises(gate.GateDeniedError):
            native_renew(newer)
        native_wire(newer, "read-filesystem-ready")
        clock[0] += 31
        rotated = newer
    for ident in (
        "create-fileset",
        "read-fileset-ready",
        "precheck-protocol",
        "create-protocol",
        "read-protocol-ready",
        "precheck-export",
        "create-export",
        "read-export-ready",
        "precheck-bucket-absent",
        "create-bucket",
        "read-bucket",
        "read-bucket-empty",
        "precheck-dataflow",
        "create-dataflow",
        "read-filesystem-implicit-members",
        "read-export-implicit-members",
        "query-dataflow-source",
    ):
        native_wire(rotated, ident)
    source = gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")
    assert source["identifier"] == NATIVE_IDS["create-dataflow"]
    with gate.database(native[0]) as db:
        assert source["reference_scope"] != raw_identity["reference_scope"]
        assert dict(db.execute("SELECT * FROM attempts WHERE id=?", (permit["attempt_id"],)).fetchone()) == raw_identity
        assert dict(db.execute("SELECT * FROM native_identity_recovery_permits").fetchone()) == permit
        for original in originals:
            assert dict(db.execute("SELECT * FROM attempts WHERE id=?", (original["id"],)).fetchone()) == original
        for ident in ("identity", "create-filesystem", "create-filesystem-vpc", "create-filesystem-vswitch"):
            assert db.execute("SELECT used FROM grants WHERE id=?", (ident,)).fetchone()[0] == 1
    for ident in ("create-filesystem", "create-filesystem-vpc", "create-filesystem-vswitch"):
        with pytest.raises(gate.GateDeniedError):
            native_wire(rotated, ident)
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    for ident in (
        "delete-dataflow",
        "delete-export",
        "delete-protocol",
        "delete-fileset",
        "delete-filesystem",
        "delete-bucket",
    ):
        native_wire(rotated, ident)
        native_wire(rotated, ident + "-absence")
    for side in ("filesystem", "export"):
        for suffix in ("implicit-sg-absence", "eni-absence"):
            native_wire(rotated, "read-" + side + "-" + suffix)
        for kind in ("vswitch", "vpc"):
            ident = "delete-" + side + "-" + kind
            native_wire(rotated, ident)
            native_wire(rotated, ident + "-absence")


@pytest.mark.parametrize(
    "damage",
    [
        "previous_review_sha256",
        "previous_identity",
        "wrong_account",
        "unknown",
        "quota",
        "scope",
        "raw",
    ],
)
def test_native_successor_does_not_bypass_review_or_actual_signer_proof(native, damage):
    rotated, handoff, _, _ = native_permit_success(native)
    gate.register_native_continuation_handoff(native[0], **native_review_file(native, "handoff.json", handoff))
    value = native_rotation_request(native, 1)
    newer = native_next_credential(native, 1)
    if damage == "previous_review_sha256":
        value["previous_review_sha256"] = "wrong"
    elif damage == "previous_identity":
        value["previous_identity"]["sha256"] = "wrong"
    elif damage == "scope":
        value["scope"] = "foreign"
    if damage in {"previous_review_sha256", "previous_identity", "scope"}:
        with pytest.raises(gate.GateDeniedError):
            gate.register_native_signer_renewal(native[0], **native_review_file(native, "bad-renewal.json", value))
    else:
        gate.register_native_signer_renewal(native[0], **native_review_file(native, "renewal.json", value))
        if damage == "unknown":
            gate.reserve(native_call(native, "identity"), "ws~native", newer[1], read_only=True, tool_use_id="unknown")
        else:
            native_renew(newer, account="foreign" if damage == "wrong_account" else "123")
        with gate.database(native[0]) as db:
            if damage == "quota":
                db.execute("UPDATE grants SET quota=2 WHERE id='ops-native-identity-renewal-1'")
            elif damage == "raw":
                db.execute("UPDATE attempts SET response='{}' WHERE grant_id='ops-native-identity-renewal-1'")
    with pytest.raises(gate.GateDeniedError):
        native_wire(newer, "delete-filesystem")


def test_native_handoff_rejects_unreviewed_backdated_old_signer_even_with_matching_counts(native):
    rotated, handoff, _, _ = native_permit_success(native)
    gate.register_native_continuation_handoff(native[0], **native_review_file(native, "handoff.json", handoff))
    value = native_rotation_request(native, 1)
    gate.register_native_signer_renewal(native[0], **native_review_file(native, "renewal.json", value))
    newer = native_next_credential(native, 1)
    native_renew(newer)
    with gate.database(native[0]) as db:
        row = dict(db.execute("SELECT * FROM attempts WHERE grant_id='read-filesystem-ready'").fetchone())
        row["id"] = "unreviewed-backdated-old-signer"
        columns = list(row)
        db.execute(
            "INSERT INTO attempts(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
            tuple(row[k] for k in columns),
        )
        db.execute("UPDATE grants SET used=used+1 WHERE id='read-filesystem-ready'")
        db.execute("UPDATE authority_limits SET used=used+1 WHERE id='read-filesystem-ready'")
    with pytest.raises(gate.GateDeniedError):
        native_wire(newer, "delete-filesystem")
    assert "No active reviewed" in gate.execution_instructions("ws~native", "")


def test_native_handoff_resumes_original_partial_thirteen_calls_without_recreating_parents(native, monkeypatch):
    clock = [time.time()]

    def tick():
        clock[0] += 0.01
        return clock[0]

    monkeypatch.setattr(time, "time", tick)
    for ident in (
        "identity",
        "create-filesystem-vpc",
        "read-filesystem-vpc",
        "create-filesystem-vswitch",
        "read-filesystem-vswitch",
        "read-filesystem-implicit-members",
        "precheck-filesystem",
        "create-filesystem",
    ):
        native_wire(native, ident)
    pending = native_body(native, "read-filesystem-ready")
    pending["FileSystems"]["FileSystem"][0]["Status"] = "Pending"
    for _ in range(5):
        clock[0] += 31
        native_wire(native, "read-filesystem-ready", body=pending)
    with gate.database(native[0]) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 13
        assert db.execute("SELECT used FROM grants WHERE id='create-export-vpc'").fetchone()[0] == 0
    rotated, handoff, permit, originals = native_permit_success(native, prepared=True)
    gate.register_native_continuation_handoff(native[0], **native_review_file(native, "partial-handoff.json", handoff))
    with gate.database(native[0]) as db:
        db.execute("UPDATE active SET phase='execute'")
    clock[0] += 31
    native_wire(rotated, "read-filesystem-ready")
    for ident in (
        "create-export-vpc",
        "read-export-vpc",
        "create-export-vswitch",
        "read-export-vswitch",
        "read-export-implicit-members",
        "create-fileset",
    ):
        native_wire(rotated, ident)
    with gate.database(native[0]) as db:
        assert db.execute("SELECT used FROM grants WHERE id='read-filesystem-ready'").fetchone()[0] == 6
        for ident in ("identity", "create-filesystem", "create-filesystem-vpc", "create-filesystem-vswitch"):
            assert db.execute("SELECT used FROM grants WHERE id=?", (ident,)).fetchone()[0] == 1
        assert dict(db.execute("SELECT * FROM native_identity_recovery_permits").fetchone()) == permit
        for original in originals:
            assert dict(db.execute("SELECT * FROM attempts WHERE id=?", (original["id"],)).fetchone()) == original


def native_empty_export(native, protocol=""):
    native_filesystem(native)
    for ident in (
        "create-fileset",
        "read-fileset-ready",
        "precheck-protocol",
        "create-protocol",
        "read-protocol-ready",
        "precheck-export",
        "create-export",
    ):
        native_wire(native, ident)
    body = native_body(native, "read-export-ready")
    if protocol == "missing":
        del body["ProtocolMountTarget"]["ProtocolType"]
    else:
        body["ProtocolMountTarget"]["ProtocolType"] = protocol
    # Same optional fields/shape as the real AVAILABLE wire, with offline IDs.
    body["ProtocolMountTarget"].update(
        ProtocolMountTargetDomain="cpfs-offline-exp-offline.cn-hangzhou.cpfs.aliyuncs.com",
        VSwitchIds=[],
    )
    native_wire(native, "read-export-ready", body=body)
    for ident in ("precheck-bucket-absent", "create-bucket", "read-bucket", "read-bucket-empty"):
        native_wire(native, ident)


@pytest.mark.parametrize("protocol", ["", "missing"])
def test_native_empty_export_uses_signed_parent_without_rewriting_wire(native, protocol):
    native_empty_export(native, protocol)
    with gate.database(native[0]) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY created,rowid")]
        grants = [dict(r) for r in db.execute("SELECT * FROM grants ORDER BY id")]
        raw = json.loads(next(r["response"] for r in rows if r["grant_id"] == "read-export-ready"))
        # A standalone blank response cannot prove NFS.
        assert not gate.nas_native_ready("read-export-ready", raw["call"], raw)
    native_wire(native, "precheck-dataflow")
    native_wire(native, "create-dataflow")
    for ident in ("read-filesystem-implicit-members", "read-export-implicit-members", "query-dataflow-source"):
        native_wire(native, ident)
    assert gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")["identifier"]
    with gate.database(native[0]) as db:
        for row in rows:
            assert dict(db.execute("SELECT * FROM attempts WHERE id=?", (row["id"],)).fetchone()) == row
        for grant in grants:
            actual = dict(db.execute("SELECT * FROM grants WHERE id=?", (grant["id"],)).fetchone())
            assert {k: v for k, v in actual.items() if k != "used"} == {k: v for k, v in grant.items() if k != "used"}
            if grant["kind"] == "create" and grant["id"] != "create-dataflow":
                assert actual["used"] == grant["used"]


@pytest.mark.parametrize(
    "damage",
    [
        "conflict",
        "null",
        "whitespace",
        "missing_parent",
        "parent_protocol",
        "parent_status",
        "parent_fs",
        "parent_id",
        "parent_vpc",
        "parent_vswitch",
        "parent_description",
        "duplicate_parent",
        "parent_page",
        "parent_call",
        "parent_session",
        "parent_signer",
        "parent_scope",
        "parent_tool",
        "parent_unknown",
        "parent_failed",
        "parent_http",
        "parent_before_create",
        "filesystem_status",
        "fileset_status",
        "export_id",
        "export_fs",
        "export_parent",
        "export_fset",
        "export_path",
        "export_vpc",
        "export_vswitch",
        "export_description",
        "export_group",
        "export_status",
        "export_page",
        "export_session",
        "export_signer",
        "export_call",
        "creation_parent",
        "creation_unknown",
    ],
)
def test_native_empty_export_rejects_unbound_or_unhealthy_parent(native, damage):
    native_empty_export(native)
    with gate.database(native[0]) as db:
        ident = "read-protocol-ready"
        if damage.startswith("export_") or damage in {"conflict", "null", "whitespace"}:
            ident = "read-export-ready"
        elif damage == "filesystem_status":
            ident = "read-filesystem-ready"
        elif damage == "fileset_status":
            ident = "read-fileset-ready"
        elif damage.startswith("creation_"):
            ident = "create-export"
        row = dict(db.execute("SELECT * FROM attempts WHERE grant_id=?", (ident,)).fetchone())
        raw = json.loads(row["response"])
        if ident == "read-protocol-ready":
            value = raw["body"]["ProtocolServices"][0]
        elif ident == "read-export-ready":
            value = raw["body"]["ProtocolMountTarget"]
        else:
            value = {}
        fields = {
            "parent_protocol": ("ProtocolType", "SMB"),
            "parent_status": ("Status", "Creating"),
            "parent_fs": ("FileSystemId", "cpfs-foreign"),
            "parent_id": ("ProtocolServiceId", "ptc-foreign"),
            "parent_vpc": ("VpcId", "vpc-foreign"),
            "parent_vswitch": ("VSwitchId", "vsw-foreign"),
            "parent_description": ("Description", "foreign"),
            "export_id": ("ExportId", "exp-foreign"),
            "export_fs": ("FileSystemId", "cpfs-foreign"),
            "export_parent": ("ProtocolServiceId", "ptc-foreign"),
            "export_fset": ("FsetId", "fset-foreign"),
            "export_path": ("Path", "/foreign/"),
            "export_vpc": ("VpcId", "vpc-foreign"),
            "export_vswitch": ("VSwitchId", "vsw-foreign"),
            "export_description": ("Description", "foreign"),
            "export_group": ("AccessGroupName", "foreign"),
            "export_status": ("Status", "CREATING"),
            "conflict": ("ProtocolType", "SMB"),
            "null": ("ProtocolType", None),
            "whitespace": ("ProtocolType", " "),
        }
        if damage in fields:
            key, value_ = fields[damage]
            value[key] = value_
        elif damage == "duplicate_parent":
            raw["body"]["ProtocolServices"].append(copy.deepcopy(value))
        elif damage in {"parent_page", "export_page"}:
            raw["body"]["NextToken"] = "more"
        elif damage in {"parent_call", "export_call", "creation_parent"}:
            key = "ProtocolServiceId" if damage != "parent_call" else "ProtocolServiceIds"
            raw["call"]["params"][key] = "ptc-foreign"
        elif damage in {"parent_session", "export_session"}:
            row["session"] = "ws~foreign"
        elif damage in {"parent_signer", "export_signer"}:
            row["credential"] = "foreign"
        elif damage == "parent_scope":
            row["reference_scope"] = "credential:foreign"
        elif damage == "parent_tool":
            row["tool_use_id"] = ""
        elif damage in {"parent_unknown", "creation_unknown"}:
            row["state"] = "unknown"
        elif damage == "parent_failed":
            row["state"] = "failed"
        elif damage == "parent_http":
            raw["status"] = 403
        elif damage == "parent_before_create":
            row["created"] = 0
        elif damage == "filesystem_status":
            raw["body"]["FileSystems"]["FileSystem"][0]["Status"] = "Pending"
        elif damage == "fileset_status":
            raw["body"]["Entries"]["Entrie"][0]["Status"] = "CREATING"
        row["response"] = json.dumps(raw)
        db.execute(
            "UPDATE attempts SET response=?,session=?,credential=?,reference_scope=?,"
            "tool_use_id=?,state=?,created=? "
            "WHERE id=?",
            tuple(
                row[k]
                for k in (
                    "response",
                    "session",
                    "credential",
                    "reference_scope",
                    "tool_use_id",
                    "state",
                    "created",
                    "id",
                )
            ),
        )
        if damage == "missing_parent":
            db.execute("DELETE FROM attempts WHERE id=?", (row["id"],))
        before = [dict(r) for r in db.execute("SELECT * FROM grants ORDER BY id")]
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "precheck-dataflow")
    with gate.database(native[0]) as db:
        assert [dict(r) for r in db.execute("SELECT * FROM grants ORDER BY id")] == before


def test_native_empty_export_preserves_reviewed_parent_across_signer_renewal(native):
    native_empty_export(native)
    _, _, rows = native_signer_review(native)
    rotated = rotated_native(native)
    native_renew(rotated)
    native_wire(rotated, "precheck-dataflow")
    native_wire(rotated, "create-dataflow")
    for ident in ("read-filesystem-implicit-members", "read-export-implicit-members", "query-dataflow-source"):
        native_wire(rotated, ident)
    assert gate.nas_native_navigation_source(native[0], "native:QUERY", "ws~native", "123")["identifier"]
    with gate.database(native[0]) as db:
        for row in rows:
            assert dict(db.execute("SELECT * FROM attempts WHERE id=?", (row["id"],)).fetchone()) == row
        assert db.execute("SELECT used FROM grants WHERE id='create-filesystem'").fetchone()[0] == 1
    with pytest.raises(gate.GateDeniedError):
        native_wire(native, "precheck-dataflow")
