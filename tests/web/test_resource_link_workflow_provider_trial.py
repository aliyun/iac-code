"""Provider upgrade admission offline contracts; fake sources, no cloud/network."""

import copy
import hashlib
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.additional_source import _validate_original_operation
from scripts.web.e2e.resource_link_workflow.creation import execution_authority, execution_digest
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, install
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow_template_correction import correction  # noqa: F401
from tests.web.test_resource_link_workflow_temporary_samples import sample  # noqa: F401


@pytest.fixture
def trial(correction, monkeypatch):  # noqa: F811
    store, old, _, _ = correction
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.selection.selected_scenes", lambda *_: [{"dimension": "TF"}]
    )
    Runtime(store).authorize_creation(store.root / "inputs/corrected.json", "inputs/corrected-review.json", 100)
    scope = "b" * 32 + ":TF"
    authority = execution_authority(store.root, old, "TF")
    account = next(iter(old["scenes"].values()))["account"]
    install(
        store.root / "execution-gate.sqlite3",
        "0",
        scope,
        "offline~trial",
        account,
        None,
        old["execution_grants"]["TF"],
        authority=authority,
    )
    logs = [
        {"Command": "apply", "Stream": "stderr", "Content": "OTSMissingHeader Missing header: 'x-ots-sourceip'"},
        {
            "Command": "version",
            "Stream": "stdout",
            "Content": json.dumps(
                {
                    "terraform_version": "1.5.7",
                    "provider_selections": {"registry.terraform.io/aliyun/alicloud": "1.140.0"},
                }
            ),
        },
    ]
    status = {
        "StackId": "offline-header-stack",
        "StackType": "Terraform",
        "Status": "CREATE_ROLLBACK_COMPLETE",
        "RegionId": "cn-hangzhou",
        "RequestId": "offline-status",
        "OperationInfo": {"ResourceType": "alicloud_ots_table"},
        "Log": {"TerraformLogs": logs},
    }
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as c:
        c.execute(
            "UPDATE grants SET used=1 WHERE scope=? AND id IN ('create','delete','identity','stack-source',"
            "'stack-members')",
            (scope,),
        )
        c.execute(
            "UPDATE authority_limits SET used=1 WHERE authority=? AND id IN ('create','delete','identity',"
            "'stack-source','stack-members')",
            (authority,),
        )
        c.execute("INSERT INTO identities VALUES(?,?,?,?)", (scope, "offline~trial", "offline-key", account))
        for index, (grant, body) in enumerate(
            [
                ("identity", {"AccountId": account}),
                ("create", {"StackId": "offline-header-stack"}),
                ("stack-source", status),
                ("stack-members", {"RequestId": "offline-members", "Resources": []}),
                ("delete", {"RequestId": "offline-delete"}),
            ]
        ):
            c.execute(
                "INSERT INTO attempts(id,scope,grant_id,state,response,created) VALUES(?,?,?,?,?,?)",
                ("header-" + grant, scope, grant, "succeeded", canonical(body), index + 10),
            )
    resource = {"id": "offline-header-stack", "region": "cn-hangzhou"}
    json_write(store.root / "inputs/cleanup-source.json", {"offline": "raw cleanup responses"})
    json_write(
        store.root / "inputs/cleanup.json",
        {
            "ownership_id": "offline-owner",
            "resource": resource,
            "scope": scope,
            "absent": True,
            "failed": False,
            "source_evidence": "inputs/cleanup-source.json",
            "source_sha256": digest(store.root / "inputs/cleanup-source.json"),
        },
    )
    cleanup = {"path": "inputs/cleanup.json", "sha256": digest(store.root / "inputs/cleanup.json")}
    with store.transaction() as c:
        c.execute(
            "INSERT INTO jobs(id,task_id,role,purpose,state,turn_id,capability,submission,created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("b" * 32, old["task_id"], "glm", "execute", "complete", "offline-header-turn", "offline", "confirmed", 10),
        )
        c.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            ("offline-owner", old["task_id"], "TF", canonical(resource), "deleted", canonical(cleanup)),
        )
    packet = copy.deepcopy(old)
    before = packet["execution_grants"]["TF"][1]["call"]["params"]["TemplateBody"]
    after = before.replace("1.140.0", "1.260.0")
    packet["execution_grants"]["TF"][1]["call"]["params"]["TemplateBody"] = after
    json_write(store.root / "inputs/header-risk.json", {"offline": "runtime SourceIp unknown"})
    request = {
        "request_id": "offline-provider-header-trial",
        "dimension": "TF",
        "prior_scope": scope,
        "predecessor_proposal": "inputs/corrected.json",
        "predecessor_sha256": digest(store.root / "inputs/corrected.json"),
        "remediation_evidence": "inputs/header-risk.json",
        "remediation_sha256": digest(store.root / "inputs/header-risk.json"),
    }
    packet["additional_source_requests"] = [request]
    sources = {}
    for key, suffix, body in [
        (
            "client",
            "alicloud/connectivity/client.go",
            'externalHeaders["x-ots-sourceip"] = client.config.SourceIp\nNewClientWithExternalHeader',
        ),
        (
            "table",
            "alicloud/resource_alicloud_ots_table.go",
            "reservedThroughput := new(tablestore.ReservedThroughput)",
        ),
        ("module", "go.mod", "github.com/aliyun/aliyun-tablestore-go-sdk v1.7.16"),
    ]:
        path = store.root / f"inputs/header-{key}.txt"
        path.write_text(body, encoding="utf-8")
        url = "https://raw.githubusercontent.com/aliyun/terraform-provider-alicloud/v1.260.0/" + suffix
        sources[key] = {"path": str(path.relative_to(store.root)), "sha256": digest(path), "url": url}
    capability = {
        "actual_account": account,
        "calls": [
            {"action": "GetCallerIdentity", "returncode": 0, "stdout": json.dumps({"AccountId": account})},
            {
                "action": "GetFeatureDetails",
                "version": "2019-09-10",
                "params": {"Feature": "Terraform", "RegionId": "cn-hangzhou"},
                "sdk_endpoint": "ros.aliyuncs.com",
                "success": True,
                "response": {
                    "statusCode": 200,
                    "body": {
                        "RequestId": "offline-capability",
                        "Terraform": {
                            "SupportedVersions": [
                                {
                                    "Transform": "Aliyun::Terraform-v1.5",
                                    "TerraformVersion": "1.5.7",
                                    "ProviderVersions": [
                                        {"ProviderName": "alicloud", "SupportedVersions": ["1.260.0"]}
                                    ],
                                }
                            ]
                        },
                    },
                },
            },
        ],
    }
    json_write(store.root / "inputs/header-capability.json", capability)
    review = {
        "kind": "ops_ots_tf_provider_header_correction",
        "approved": True,
        "task_id": packet["task_id"],
        "plan_id": packet["task_id"].rsplit(":", 1)[0],
        "original_table_never_created": True,
        "runtime_source_ip_unknown": True,
        "bounded_trial_risk_accepted": True,
        "no_manual_source_ip_or_header": True,
        "original_cumulative_budget_retained": True,
        "human_scope_interpretation": "offline one unused TF table",
        "original_admission": {"path": "inputs/review.json", "sha256": digest(store.root / "inputs/review.json")},
        "original_ownership_id": "offline-owner",
        "original_cleanup": cleanup,
        "original_template_sha256": hashlib.sha256(before.encode()).hexdigest(),
        "corrected_template_sha256": hashlib.sha256(after.encode()).hexdigest(),
        "original_failure_attempt_id": "header-stack-source",
        "empty_membership_attempt_id": "header-stack-members",
        "ros_capability": {
            "path": "inputs/header-capability.json",
            "sha256": digest(store.root / "inputs/header-capability.json"),
        },
        "provider_sources": sources,
    }
    with store.transaction() as c:
        plan = c.execute("SELECT plan FROM runs").fetchone()[0]
        review["frozen_plan_sha256"] = hashlib.sha256(plan.encode()).hexdigest()
    proof = json.loads((store.root / "inputs/corrected-review.json").read_text(encoding="utf-8"))
    proof["additional_sources_approved"] = [request]
    proof["temporary_template_corrections"] = {request["request_id"]: review}
    cost = json.loads((store.root / "inputs/cost.json").read_text(encoding="utf-8"))
    cost.update(execution_sha256=execution_digest(packet), additional_operations=[request])
    json_write(store.root / "inputs/header-cost.json", cost)
    for key in ["paid_operation_review", "budget_reservation_reuse"]:
        proof[key].update(
            cost_evidence="inputs/header-cost.json", cost_sha256=digest(store.root / "inputs/header-cost.json")
        )
    json_write(store.root / "inputs/header-proposal.json", packet)
    proof["proposal_sha256"] = digest(store.root / "inputs/header-proposal.json")
    json_write(store.root / "inputs/header-review.json", proof)
    return store, packet, proof, authority


def test_one_header_trial_keeps_old_admission_counters_budget_and_context(trial):
    store, packet, proof, old_authority = trial
    before = (store.root / "execution-gate.sqlite3").read_bytes()
    Runtime(store).authorize_creation(store.root / "inputs/header-proposal.json", "inputs/header-review.json", 100)
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        assert admitted(store.root, c, task, packet)["TF"] != old_authority
        c.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)", ("new", packet["task_id"], "TF", '{"id":"new"}', "present", "{}")
        )
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        assert admitted(store.root, c, task, packet)["TF"]
        assert c.execute("SELECT amount FROM budget_reservations").fetchone()[0] == 100
        with pytest.raises(WorkflowError, match="verified cleanup"):
            admitted(store.root, c, task, packet, review=proof)
    assert (store.root / "execution-gate.sqlite3").read_bytes() == before
    # The older enum authorization must remain valid after the provider trial is sealed.
    old = json.loads((store.root / "inputs/corrected.json").read_text(encoding="utf-8"))
    assert execution_authority(store.root, old, "TF") == old_authority


@pytest.mark.parametrize(
    "damage",
    [
        "approval",
        "risk",
        "runtime",
        "admission",
        "source",
        "cap-account",
        "cap-region",
        "cap-provider",
        "cap-version",
        "template",
        "quota",
        "open",
        "failure",
        "members",
        "identity",
        "unknown",
        "cleanup",
        "live-owner",
        "rename",
    ],
)
def test_header_trial_rejects_unreviewed_or_changed_provenance(trial, damage):
    store, packet, proof, _ = trial
    request = packet["additional_source_requests"][0]
    review = proof["temporary_template_corrections"][request["request_id"]]
    if damage in {"approval", "risk", "runtime"}:
        review[
            {"approval": "approved", "risk": "bounded_trial_risk_accepted", "runtime": "runtime_source_ip_unknown"}[
                damage
            ]
        ] = False
    elif damage == "admission":
        review["original_admission"]["sha256"] = "wrong"
    elif damage == "source":
        (store.root / "inputs/header-client.txt").write_text("drift", encoding="utf-8")
    elif damage.startswith("cap-"):
        path = store.root / "inputs/header-capability.json"
        cap = json.loads(path.read_text(encoding="utf-8"))
        if damage == "cap-account":
            cap["actual_account"] = "other"
        elif damage == "cap-region":
            cap["calls"][1]["params"]["RegionId"] = "cn-shanghai"
        else:
            version = cap["calls"][1]["response"]["body"]["Terraform"]["SupportedVersions"][0]
            key = "ProviderName" if damage == "cap-provider" else "SupportedVersions"
            version["ProviderVersions"][0][key] = "wrong"
        json_write(path, cap)
        review["ros_capability"]["sha256"] = digest(path)
    elif damage == "template":
        packet["execution_grants"]["TF"][1]["call"]["params"]["TemplateBody"] += " "
    elif damage == "quota":
        packet["execution_grants"]["TF"][0]["quota"] = 2
    elif damage == "open":
        packet["scenes"]["R0043-TF-1-open"] = next(iter(packet["scenes"].values()))
    elif damage in {"failure", "members", "identity", "unknown"}:
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as c:
            if damage == "identity":
                c.execute("UPDATE identities SET account='other' WHERE scope=?", (request["prior_scope"],))
            elif damage == "unknown":
                c.execute("UPDATE attempts SET state='unknown' WHERE id='header-create'")
            else:
                c.execute(
                    "UPDATE attempts SET response='{}' WHERE id=?",
                    ("header-stack-source" if damage == "failure" else "header-stack-members",),
                )
    elif damage in {"cleanup", "live-owner"}:
        with store.transaction() as c:
            c.execute("UPDATE owned SET " + ("evidence='{}'" if damage == "cleanup" else "state='present'"))
    else:
        saved = {
            "task_id": packet["task_id"],
            "authorization_evidence": "inputs/header-review.json",
            "additional_source_requests": [{**request, "request_id": "cloned-trial-id"}],
        }
        json_write(store.root / "creation-authorizations/previous-header.json", saved)
    with pytest.raises(WorkflowError):
        _validate_original_operation(store.root, packet, request, proof)
