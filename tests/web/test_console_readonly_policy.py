from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.web.console_readonly_policy import (
    POLICY_DESCRIPTION,
    POLICY_NAME,
    REVIEWED_ACTIONS,
    managed_actions,
    policy_document,
    policy_revision,
)
from iac_code.web.resource_signin_role import (
    ConsoleSigninRoleError,
    console_signin_policy_settings,
    create_console_signin_role,
    invalidate_console_signin_role_cache,
    repair_console_signin_role,
)
from iac_code.web.settings import console_signin_role_settings, save_console_signin_role

ARN = "acs:ram::1001:role/iaccodeconsolesigninrole"
SOURCE = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")


class CloudError(Exception):
    def __init__(self, code):
        self.code = code


class Ram:
    def __init__(self):
        self.calls = []
        self.document = None
        self.description = POLICY_DESCRIPTION
        self.role_description = "Read-only role used by iac-code embedded console previews."
        self.attached = []
        self.history = []
        self.other_entities = False
        self.fail = ""
        self.stale_verification = False

    async def __call__(self, _credential, endpoint, action, version, query):
        self.calls.append((action, deepcopy(query)))
        assert endpoint in {"sts.aliyuncs.com", "ram.aliyuncs.com"}
        assert version in {"2015-04-01", "2015-05-01"}
        if action == self.fail:
            raise CloudError("Forbidden.RAM")
        if action == "GetCallerIdentity":
            return {"IdentityType": "RAMUser", "AccountId": "1001", "Arn": "acs:ram::1001:user/alice"}
        if action == "CreateRole":
            return {"Role": {"Arn": ARN}}
        if action == "GetRole":
            return {
                "Role": {
                    "RoleName": "IacCodeConsoleSigninRole",
                    "Arn": ARN,
                    "Description": self.role_description,
                    "MaxSessionDuration": 3600,
                    "AllowConsoleLogin": True,
                    "AssumeRolePolicyDocument": {
                        "Version": "1",
                        "Statement": [
                            {
                                "Action": "sts:AssumeRole",
                                "Effect": "Allow",
                                "Principal": {"RAM": ["acs:ram::1001:user/alice"]},
                            }
                        ],
                    },
                }
            }
        if action == "GetPolicy":
            if self.document is None:
                raise CloudError("EntityNotExist.Policy")
            return {
                "Policy": {
                    "PolicyName": POLICY_NAME,
                    "Description": self.description,
                    "PolicyType": "Custom",
                },
                "DefaultPolicyVersion": {"PolicyDocument": json.dumps(self.document), "VersionId": "v1"},
            }
        if action == "CreatePolicy":
            self.document = json.loads(query["PolicyDocument"])
            self.history = [deepcopy(self.document)]
            return {"Policy": {"DefaultVersion": "v1"}}
        if action == "ListEntitiesForPolicy":
            return {
                "Users": {"User": ["bob"] if self.other_entities else []},
                "Groups": {"Group": []},
                "Roles": {"Role": [{"RoleName": "IacCodeConsoleSigninRole"}]},
            }
        if action == "ListPolicyVersions":
            return {"PolicyVersions": {"PolicyVersion": [{"VersionId": f"v{i + 1}"} for i in range(len(self.history))]}}
        if action == "GetPolicyVersion":
            return {"PolicyVersion": {"PolicyDocument": json.dumps(self.history[int(query["VersionId"][1:]) - 1])}}
        if action == "CreatePolicyVersion":
            assert query["SetAsDefault"] is True
            assert query["RotateStrategy"] == "DeleteOldestNonDefaultVersionWhenLimitExceeded"
            if not self.stale_verification:
                self.document = json.loads(query["PolicyDocument"])
            self.history.append(json.loads(query["PolicyDocument"]))
            return {"PolicyVersion": {"VersionId": "v2", "IsDefaultVersion": True}}
        if action == "ListPoliciesForRole":
            assert query == {"RoleName": "IacCodeConsoleSigninRole"}
            return {"Policies": {"Policy": deepcopy(self.attached)}}
        if action == "AttachPolicyToRole":
            assert query["RoleName"] == "IacCodeConsoleSigninRole"
            self.attached.append({"PolicyName": query["PolicyName"], "PolicyType": query["PolicyType"]})
            return {}
        if action == "AssumeRole":
            return {
                "Credentials": {
                    "AccessKeyId": "fake-sts",
                    "AccessKeySecret": "fake-secret",
                    "SecurityToken": "fake-token",
                    "Expiration": "2099-01-01T00:00:00Z",
                }
            }
        raise AssertionError((action, query))


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    invalidate_console_signin_role_cache()
    yield
    invalidate_console_signin_role_cache()


def test_registry_is_exact_reviewed_and_revision_tracks_document():
    assert set(REVIEWED_ACTIONS) == {
        "oos:SearchInventory",
        "mse:CheckServiceLinkRole",
        "lindorm:CheckLindormInstanceValidate",
        "sae:CheckApplicationSlbConfigWarn",
        "odps:CheckTunnelAdditional",
    }
    owner_confirmed = {"odps:CheckTunnelAdditional": "owner-confirmed-readonly:2026-10-14"}
    assert {action: REVIEWED_ACTIONS[action] for action in owner_confirmed} == owner_confirmed
    assert all(
        "*" not in action and (source.startswith("https://help.aliyun.com/") or owner_confirmed.get(action) == source)
        for action, source in REVIEWED_ACTIONS.items()
    )
    assert REVIEWED_ACTIONS["sae:CheckApplicationSlbConfigWarn"] == (
        "https://help.aliyun.com/en/ram/developer-reference/aliyunsaereadonlyaccess"
    )
    assert managed_actions(policy_document()) == set(REVIEWED_ACTIONS)
    assert len(policy_revision()) == 64


@pytest.mark.parametrize(
    "mutation", ["wildcard", "write", "condition", "deny", "extra_statement", "resource", "duplicate"]
)
def test_customer_policy_is_never_treated_as_managed(mutation):
    document = policy_document()
    statement = document["Statement"][0]
    if mutation == "wildcard":
        statement["Action"] = ["oos:*"]
    elif mutation == "write":
        statement["Action"].append("oos:StartExecution")
    elif mutation == "condition":
        statement["Condition"] = {"StringEquals": {"acs:RequestedRegion": "cn-hangzhou"}}
    elif mutation == "deny":
        statement["Effect"] = "Deny"
    elif mutation == "extra_statement":
        document["Statement"].append(deepcopy(statement))
    elif mutation == "resource":
        statement["Resource"] = "acs:oos:*:*:*"
    else:
        statement["Action"].append(statement["Action"][0])
    assert managed_actions(document) is None


@pytest.mark.parametrize(
    "action",
    ["sae:CheckApplication*", "sae:CheckUnknown", "cms:CreateIntegrationPolicy", "odps:Check*", "odps:CheckUnknown"],
)
def test_unreviewed_console_actions_cannot_be_adopted(action):
    document = policy_document()
    document["Statement"][0]["Action"].append(action)
    assert managed_actions(document) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("include_sae", [False, True], ids=["previous-three-actions", "previous-four-actions"])
async def test_previous_policy_requires_explicit_additive_repair(include_sae):
    previous_actions = {
        "oos:SearchInventory",
        "mse:CheckServiceLinkRole",
        "lindorm:CheckLindormInstanceValidate",
    }
    if include_sae:
        previous_actions.add("sae:CheckApplicationSlbConfigWarn")
    previous_document = {
        "Version": "1",
        "Statement": [{"Effect": "Allow", "Action": sorted(previous_actions), "Resource": "*"}],
    }
    previous_revision = hashlib.sha256(
        json.dumps(previous_document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    save_console_signin_role(ARN, supplement_revision=previous_revision)
    ram = Ram()
    ram.document = deepcopy(previous_document)
    ram.history = [deepcopy(previous_document)]
    ram.attached = [
        {"PolicyName": "ReadOnlyAccess", "PolicyType": "System"},
        {"PolicyName": POLICY_NAME, "PolicyType": "Custom"},
    ]

    assert managed_actions(previous_document) == previous_actions
    assert policy_revision() != previous_revision
    hint = console_signin_policy_settings(console_signin_role_settings())["supplementPolicy"]
    assert hint["needsRepair"] is True
    assert hint["revision"] == policy_revision()
    assert ram.document == previous_document
    assert ram.calls == []
    assert console_signin_role_settings()["supplementRevision"] == previous_revision

    await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert ram.document == policy_document()
    expected_additions = {"odps:CheckTunnelAdditional"}
    if not include_sae:
        expected_additions.add("sae:CheckApplicationSlbConfigWarn")
    assert set(ram.document["Statement"][0]["Action"]) - previous_actions == expected_additions
    assert previous_actions.issubset(ram.document["Statement"][0]["Action"])
    assert ram.history == [previous_document, policy_document()]
    mutations = {"CreateRole", "CreatePolicy", "CreatePolicyVersion", "AttachPolicyToRole"}
    assert [action for action, _ in ram.calls if action in mutations] == ["CreatePolicyVersion"]
    assert console_signin_role_settings()["supplementRevision"] == policy_revision()
    assert not console_signin_policy_settings(console_signin_role_settings())["supplementPolicy"]["needsRepair"]

    ram.calls.clear()
    await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert not any(action in mutations for action, _ in ram.calls)
    assert ram.history == [previous_document, policy_document()]


@pytest.mark.asyncio
async def test_new_role_receives_both_policies_and_repeat_create_does_not_mutate():
    ram = Ram()
    result = await create_console_signin_role(SOURCE, rpc_call=ram)
    assert result["status"] == "ready"
    assert result["supplementPolicyName"] == POLICY_NAME
    assert ram.document == policy_document()
    assert ram.attached == [
        {"PolicyName": "ReadOnlyAccess", "PolicyType": "System"},
        {"PolicyName": POLICY_NAME, "PolicyType": "Custom"},
    ]
    assert console_signin_role_settings()["supplementRevision"] == policy_revision()
    ram.calls.clear()
    await create_console_signin_role(SOURCE, rpc_call=ram)
    assert [action for action, _query in ram.calls] == ["GetCallerIdentity", "AssumeRole"]


@pytest.mark.asyncio
async def test_legacy_role_one_click_repair_and_repeat_are_idempotent():
    save_console_signin_role(ARN)
    ram = Ram()
    ram.attached = [{"PolicyName": "ReadOnlyAccess", "PolicyType": "System"}]
    assert console_signin_policy_settings(console_signin_role_settings())["supplementPolicy"]["needsRepair"]
    await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert "CreateRole" not in [action for action, _query in ram.calls]
    ram.calls.clear()
    await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert not any(
        action in {"CreateRole", "CreatePolicy", "CreatePolicyVersion", "AttachPolicyToRole"}
        for action, _query in ram.calls
    )
    assert not console_signin_policy_settings(console_signin_role_settings())["supplementPolicy"]["needsRepair"]


@pytest.mark.asyncio
async def test_upgrade_replaces_only_managed_subset_default_and_keeps_history():
    save_console_signin_role(ARN)
    ram = Ram()
    ram.document = {
        "Version": "1",
        "Statement": [{"Effect": "Allow", "Action": ["oos:SearchInventory"], "Resource": "*"}],
    }
    ram.history = [deepcopy(ram.document) for _ in range(5)]
    await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert ram.document == policy_document()
    assert sum(action == "CreatePolicyVersion" for action, _query in ram.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["role", "policy_description", "policy_document", "other_entity", "old_version"])
async def test_conflict_refuses_adoption_overwrite_or_rotation(conflict):
    save_console_signin_role(ARN)
    ram = Ram()
    ram.document = {
        "Version": "1",
        "Statement": [{"Effect": "Allow", "Action": ["oos:SearchInventory"], "Resource": "*"}],
    }
    ram.history = [deepcopy(ram.document)]
    if conflict == "role":
        ram.role_description = "Customer role"
    elif conflict == "policy_description":
        ram.description = "Customer policy"
    elif conflict == "policy_document":
        ram.document["Statement"][0]["Action"] = ["oos:*"]
    elif conflict == "other_entity":
        ram.other_entities = True
    else:
        ram.history.append({"Version": "1", "Statement": [{"Effect": "Allow", "Action": ["*"], "Resource": "*"}]})
    with pytest.raises(ConsoleSigninRoleError, match="conflict|target_invalid"):
        await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert not any(action in {"CreateRole", "CreatePolicyVersion", "CreatePolicy"} for action, _query in ram.calls)
    assert not any(action == "AttachPolicyToRole" and query["PolicyType"] == "Custom" for action, query in ram.calls)


@pytest.mark.asyncio
async def test_wrong_account_or_custom_saved_role_never_receives_cloud_writes():
    for arn in ["acs:ram::2002:role/iaccodeconsolesigninrole", "acs:ram::1001:role/custom"]:
        save_console_signin_role(arn)
        ram = Ram()
        with pytest.raises(ConsoleSigninRoleError, match="repair_target_invalid"):
            await repair_console_signin_role(SOURCE, rpc_call=ram)
        assert [action for action, _query in ram.calls] == ["GetCallerIdentity"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["CreatePolicy", "AttachPolicyToRole", "CreatePolicyVersion"])
async def test_partial_failure_is_visible_and_can_resume_without_role_recreation(failure):
    save_console_signin_role(ARN)
    ram = Ram()
    ram.fail = failure
    if failure == "CreatePolicyVersion":
        ram.document = {
            "Version": "1",
            "Statement": [{"Effect": "Allow", "Action": ["oos:SearchInventory"], "Resource": "*"}],
        }
        ram.history = [deepcopy(ram.document)]
    with pytest.raises(ConsoleSigninRoleError, match="denied"):
        await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert console_signin_role_settings()["setupComplete"] is False
    ram.fail = ""
    await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert console_signin_role_settings()["setupComplete"] is True
    assert "CreateRole" not in [action for action, _query in ram.calls]


@pytest.mark.asyncio
async def test_unverified_policy_update_cannot_mark_setup_complete():
    save_console_signin_role(ARN)
    ram = Ram()
    ram.document = {
        "Version": "1",
        "Statement": [{"Effect": "Allow", "Action": ["oos:SearchInventory"], "Resource": "*"}],
    }
    ram.history = [deepcopy(ram.document)]
    ram.stale_verification = True
    with pytest.raises(ConsoleSigninRoleError, match="policy_verify_failed"):
        await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert console_signin_role_settings()["setupComplete"] is False


@pytest.mark.asyncio
async def test_concurrent_settings_clicks_do_not_duplicate_role_or_policy():
    ram = Ram()
    await asyncio.gather(*(create_console_signin_role(SOURCE, rpc_call=ram) for _ in range(4)))
    assert sum(action == "CreateRole" for action, _query in ram.calls) == 1
    assert sum(action == "CreatePolicy" for action, _query in ram.calls) == 1


def test_manually_saving_an_arn_cannot_claim_a_current_policy_revision():
    save_console_signin_role(ARN, supplement_revision=policy_revision())
    save_console_signin_role(ARN)
    assert "supplementRevision" not in console_signin_role_settings()


@pytest.mark.asyncio
async def test_unknown_create_outcome_is_not_replayed_on_retry():
    save_console_signin_role(ARN)
    ram = Ram()
    lost_response = True

    async def rpc(*args):
        nonlocal lost_response
        result = await ram(*args)
        if args[2] == "CreatePolicy" and lost_response:
            lost_response = False
            raise TimeoutError("response lost after create")
        return result

    with pytest.raises(ConsoleSigninRoleError, match="policy_failed"):
        await repair_console_signin_role(SOURCE, rpc_call=rpc)
    assert console_signin_role_settings()["setupComplete"] is False
    await repair_console_signin_role(SOURCE, rpc_call=rpc)
    assert sum(action == "CreatePolicy" for action, _ in ram.calls) == 1
    assert console_signin_role_settings()["setupComplete"] is True


@pytest.mark.asyncio
async def test_custom_attachment_failure_resumes_existing_policy_without_recreating_it():
    save_console_signin_role(ARN)
    ram = Ram()
    ram.attached = [{"PolicyName": "ReadOnlyAccess", "PolicyType": "System"}]
    ram.fail = "AttachPolicyToRole"
    with pytest.raises(ConsoleSigninRoleError, match="policy_denied"):
        await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert ram.document == policy_document()
    assert console_signin_role_settings()["setupComplete"] is False
    ram.fail = ""
    await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert sum(action == "CreatePolicy" for action, _ in ram.calls) == 1
    assert console_signin_role_settings()["setupComplete"] is True


@pytest.mark.asyncio
async def test_role_inspection_denial_does_not_mutate_cloud_permissions():
    save_console_signin_role(ARN)
    ram = Ram()
    ram.fail = "GetRole"
    with pytest.raises(ConsoleSigninRoleError, match="policy_denied"):
        await repair_console_signin_role(SOURCE, rpc_call=ram)
    assert [action for action, _ in ram.calls] == ["GetCallerIdentity", "GetRole"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["ListPoliciesForRole", "ListEntitiesForPolicy"])
async def test_malformed_policy_inspection_never_causes_an_authorization_write(action):
    save_console_signin_role(ARN)
    ram = Ram()
    ram.attached = [{"PolicyName": "ReadOnlyAccess", "PolicyType": "System"}]
    ram.document = {
        "Version": "1",
        "Statement": [{"Effect": "Allow", "Action": ["oos:SearchInventory"], "Resource": "*"}],
    }
    ram.history = [deepcopy(ram.document)]

    async def rpc(*args):
        return {} if args[2] == action else await ram(*args)

    with pytest.raises(ConsoleSigninRoleError):
        await repair_console_signin_role(SOURCE, rpc_call=rpc)
    assert not any(name in {"AttachPolicyToRole", "CreatePolicyVersion", "CreatePolicy"} for name, _ in ram.calls)
    assert console_signin_role_settings()["setupComplete"] is False
