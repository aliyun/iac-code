"""One OPS-reviewed OTS provider-header trial; never a generic template retry."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

from .files import WorkflowError, contained, digest, read_json

KIND = "ops_ots_tf_provider_header_correction"
LOADED_SHA = digest(Path(__file__))


def _evidence(root: Path, ref: dict[str, Any]) -> Path:
    path = contained(root, ref.get("path", "missing"))
    if digest(path) != ref.get("sha256"):
        raise WorkflowError("provider trial original evidence changed")
    return path


def reviewed_provider_trial(
    root: Path, old: dict[str, Any], packet: dict[str, Any], request: dict[str, Any], proof: dict[str, Any],
) -> bool:
    from .temporary_samples import reviewed_scope

    reviews = proof.get("temporary_template_corrections", {})
    review = reviews.get(request.get("request_id"), {})
    flags = ("original_table_never_created", "runtime_source_ip_unknown", "bounded_trial_risk_accepted",
             "no_manual_source_ip_or_header", "original_cumulative_budget_retained")
    if (
        set(reviews) != {request.get("request_id")} or review.get("kind") != KIND
        or review.get("approved") is not True or review.get("task_id") != packet["task_id"]
        or any(review.get(flag) is not True for flag in flags) or not review.get("human_scope_interpretation")
        or request.get("dimension") != "TF" or packet.get("additional_source_requests") != [request]
        or set(packet.get("execution_grants", {})) != {"TF"}
        or set(packet.get("scenes", {})) != {packet["task_id"].rsplit(":", 1)[-1] + "-TF-1-iframe"}
    ):
        raise WorkflowError("one explicit remaining OTS TF iframe provider trial review required")
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as state:
        state.row_factory = sqlite3.Row
        task = state.execute("SELECT * FROM tasks WHERE id=?", (packet["task_id"],)).fetchone()
        row = state.execute("SELECT value FROM meta WHERE key=?",
                            ("temporary_samples:" + packet["task_id"],)).fetchone()
        if not task or not row:
            raise WorkflowError("provider trial requires immutable original temporary admission")
        saved = json.loads(row[0])
        run = state.execute("SELECT * FROM runs WHERE id=?", (task["run_id"],)).fetchone()
        if reviewed_scope(root, task, run, saved["evidence"]) != saved or (
            json.loads(task["spec"]).get("key") != "ALIYUN::OTS::Table"
            or review.get("original_admission") != {"path": saved["evidence"], "sha256": saved["evidence_sha256"]}
            or review.get("plan_id") != task["run_id"]
            or review.get("frozen_plan_sha256") != hashlib.sha256(run["plan"].encode()).hexdigest()
            or packet.get("budget_cents") != saved["budget_cents"]
            or {value["account"] for value in packet["scenes"].values()} != set(saved["accounts"])
            or old.get("task_id") != packet["task_id"]
        ):
            raise WorkflowError("provider trial cannot change original task, human scope, budget or account")
        owner = state.execute("SELECT * FROM owned WHERE id=? AND task_id=?",
                              (review.get("original_ownership_id"), packet["task_id"])).fetchone()
        cleanup_path = _evidence(root, review.get("original_cleanup", {}))
        cleanup = read_json(cleanup_path)
        if (
            not owner or owner["state"] != "deleted" or owner["dimension"] != "TF"
            or json.loads(owner["evidence"]) != review["original_cleanup"]
            or cleanup.get("ownership_id") != owner["id"] or cleanup.get("resource") != json.loads(owner["resource"])
            or cleanup.get("absent") is not True or cleanup.get("failed") is not False
            or cleanup.get("scope") != request["prior_scope"]
        ):
            raise WorkflowError("original provider trial generation needs independent committed cleanup")
        _evidence(root, {"path": cleanup.get("source_evidence"), "sha256": cleanup.get("source_sha256")})
    original = old.get("execution_grants", {}).get("TF", [])
    expected = json.loads(json.dumps(original))
    immutable = saved["execution_grants"]["TF"]
    if len(original) != len(immutable):
        raise WorkflowError("provider trial cannot add or remove original admitted calls")
    for entry, admitted in zip(original, immutable):
        prior = json.loads(json.dumps(admitted))
        if prior["kind"] == "create":
            params = prior["call"]["params"]
            params["TemplateBody"] = params["TemplateBody"].replace("STRING", "String")
        if {k: v for k, v in entry.items() if k != "quota"} != {k: v for k, v in prior.items() if k != "quota"}:
            raise WorkflowError("predecessor must retain original admitted calls and reviewed enum")
        # The predecessor's distinct operation already reviewed one identity and precheck.
        if entry["quota"] != (1 if entry["kind"] == "identity" or entry["id"] == "table-name-precheck"
                              else prior["quota"]):
            raise WorkflowError("provider trial cannot reset or expand original read/write quotas")
    creates = [entry for entry in expected if entry["kind"] == "create"]
    if len(creates) != 1:
        raise WorkflowError("one original managed TF stack required")
    create = creates[0]
    before = create["call"]["params"]["TemplateBody"]
    value = json.loads(before)
    hcl = value.get("Workspace", {}).get("main.tf", "")
    if value.get("Transform") != "Aliyun::Terraform-v1.5" or before.count("1.140.0") != 1 or (
        'version = "= 1.140.0"' not in hcl or 'type = "String"' not in hcl
    ):
        raise WorkflowError("trial predecessor must use the exact reviewed String/1.140.0 template")
    after = before.replace("1.140.0", "1.260.0")
    create["call"]["params"]["TemplateBody"] = after
    if expected != packet["execution_grants"]["TF"] or (
        review.get("original_template_sha256") != hashlib.sha256(before.encode()).hexdigest()
        or review.get("corrected_template_sha256") != hashlib.sha256(after.encode()).hexdigest()
    ):
        raise WorkflowError("provider trial may change exactly one version literal and nothing else")
    region = create["call"]["region_id"]
    with closing(sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True)) as gate:
        gate.row_factory = sqlite3.Row
        scope = request["prior_scope"]
        creation = gate.execute("SELECT * FROM attempts WHERE scope=? AND grant_id=?",
                                (scope, create["id"])).fetchall()
        failure = gate.execute("SELECT * FROM attempts WHERE scope=? AND id=?",
                               (scope, review.get("original_failure_attempt_id"))).fetchone()
        members = gate.execute("SELECT * FROM attempts WHERE scope=? AND id=?",
                               (scope, review.get("empty_membership_attempt_id"))).fetchone()
        if len(creation) != 1 or any(not row or row["state"] != "succeeded" for row in [*creation, failure, members]):
            raise WorkflowError("real successful create/status/empty-membership provenance required")
        stack_id = json.loads(creation[0]["response"]).get("StackId")
        status, membership = json.loads(failure["response"]), json.loads(members["response"])
        logs = status.get("Log", {}).get("TerraformLogs", [])
        failure_grant = next((entry for entry in original if entry["id"] == failure["grant_id"]), {})
        member_grant = next((entry for entry in original if entry["id"] == members["grant_id"]), {})
        versions = [json.loads(log["Content"]) for log in logs if log.get("Command") == "version"
                    and log.get("Stream") == "stdout"]
        if (
            not stack_id or stack_id != cleanup["resource"].get("id") or status.get("StackId") != stack_id
            or status.get("StackType") != "Terraform" or status.get("Status") != "CREATE_ROLLBACK_COMPLETE"
            or status.get("RegionId") != region or not status.get("RequestId")
            or status.get("OperationInfo", {}).get("ResourceType") != "alicloud_ots_table"
            or not any(log.get("Command") == "apply" and log.get("Stream") == "stderr"
                       and "OTSMissingHeader" in log.get("Content", "")
                       and "Missing header: 'x-ots-sourceip'" in log["Content"] for log in logs)
            or not any(v.get("terraform_version") == "1.5.7" and v.get("provider_selections", {}).get(
                "registry.terraform.io/aliyun/alicloud") == "1.140.0" for v in versions)
            or membership.get("Resources") != [] or not membership.get("RequestId")
            or not creation[0]["created"] < failure["created"] < members["created"]
        ):
            raise WorkflowError("only proven missing-header apply failure with no table child admits this trial")
        for entry, action in [(failure_grant, "GetStack"), (member_grant, "ListStackResources")]:
            if (entry.get("kind") != "read" or entry.get("call", {}).get("action") != action
                or entry.get("parent") != create["id"] or entry.get("response_path") != ["StackId"]):
                raise WorkflowError("failure and empty members must bind their original created stack")
        for entry in original:
            binding = gate.execute("SELECT kind,call FROM grants WHERE scope=? AND id=?",
                                   (scope, entry["id"])).fetchone()
            if not binding or tuple(binding) != (entry["kind"], canonical(entry["call"])):
                raise WorkflowError("provider trial predecessor signed calls changed")
            if entry["kind"] in {"create", "delete"}:
                grant = gate.execute("SELECT authority,quota,used FROM grants WHERE scope=? AND id=?",
                                     (scope, entry["id"])).fetchone()
                attempts = gate.execute("SELECT state FROM attempts WHERE scope=? AND grant_id=?",
                                        (scope, entry["id"])).fetchall()
                limit = gate.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                     (grant["authority"], entry["id"])).fetchone()
                if tuple(grant)[1:] != (1, 1) or [tuple(row) for row in attempts] != [("succeeded",)] or (
                    not limit or tuple(limit) != (1,)
                ):
                    raise WorkflowError("provider trial must preserve exact consumed original write provenance")
        if gate.execute("SELECT 1 FROM attempts WHERE scope=? AND state!='succeeded'", (scope,)).fetchone():
            raise WorkflowError("provider trial cannot ignore unknown original calls")
        identities = gate.execute("SELECT DISTINCT account FROM identities WHERE scope=?", (scope,)).fetchall()
        if [tuple(row) for row in identities] != [(saved["accounts"][0],)]:
            raise WorkflowError("provider trial requires real original signed account")
    _validate_capability_and_sources(root, review, saved["accounts"][0], region)
    for path in (root / "creation-authorizations").glob("*.json"):
        approval = read_json(path)
        if approval.get("task_id") != packet["task_id"]:
            continue
        previous = read_json(contained(root, approval["authorization_evidence"]))
        previous_reviews = previous.get("temporary_template_corrections", {})
        if any(v.get("kind") == KIND for v in previous_reviews.values()) and (
            previous_reviews != reviews or approval.get("additional_source_requests") != [request]
        ):
            raise WorkflowError("original temporary sample already has its one provider-header trial")
    return True


def _validate_capability_and_sources(root: Path, review: dict[str, Any], account: str, region: str) -> None:
    capability = read_json(_evidence(root, review.get("ros_capability", {})))
    calls = capability.get("calls", [])
    if len(calls) != 2:
        raise WorkflowError("same-account signed ROS capability original required")
    identity, call = calls
    body = call.get("response", {}).get("body", {})
    if (
        capability.get("actual_account") != account or identity.get("action") != "GetCallerIdentity"
        or identity.get("returncode") != 0 or json.loads(identity.get("stdout", "{}")).get("AccountId") != account
        or call.get("action") != "GetFeatureDetails" or call.get("version") != "2019-09-10"
        or call.get("params") != {"Feature": "Terraform", "RegionId": region}
        or call.get("sdk_endpoint") != "ros.aliyuncs.com" or call.get("success") is not True
        or call.get("response", {}).get("statusCode") != 200 or not body.get("RequestId")
        or not any(v.get("Transform") == "Aliyun::Terraform-v1.5" and v.get("TerraformVersion") == "1.5.7"
                   and any(p.get("ProviderName") == "alicloud" and "1.260.0" in p.get("SupportedVersions", [])
                           for p in v.get("ProviderVersions", []))
                   for v in body.get("Terraform", {}).get("SupportedVersions", []))
    ):
        raise WorkflowError("actual ROS capability must support this exact alicloud/TF version and region")
    sources = review.get("provider_sources", {})
    required = {
        "client": ("alicloud/connectivity/client.go", ('externalHeaders["x-ots-sourceip"] = client.config.SourceIp',
                                                     "NewClientWithExternalHeader")),
        "table": ("alicloud/resource_alicloud_ots_table.go",
                  ("reservedThroughput := new(tablestore.ReservedThroughput)",)),
        "module": ("go.mod", ("github.com/aliyun/aliyun-tablestore-go-sdk v1.7.16",)),
    }
    if set(sources) != set(required):
        raise WorkflowError("complete pinned provider client/table/module sources required")
    for key, (suffix, markers) in required.items():
        ref = sources[key]
        body = _evidence(root, ref).read_text(encoding="utf-8")
        url = "https://raw.githubusercontent.com/aliyun/terraform-provider-alicloud/v1.260.0/" + suffix
        if ref.get("url") != url or (
            any(marker not in body for marker in markers)
        ):
            raise WorkflowError("provider header/empty-table implementation needs unchanged pinned official sources")
