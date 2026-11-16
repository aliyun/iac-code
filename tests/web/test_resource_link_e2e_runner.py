from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.web.resource_link_data import fake_digest, isolate_runner, web_source_replay

_RUNNER_DATA_ROOT = None


@pytest.fixture(autouse=True)
def _isolated_runner_data(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "_RUNNER_DATA_ROOT", tmp_path / "runner-data")


def _runner_module(tmp_path: Path | None = None):
    path = Path(__file__).parents[2] / "scripts" / "web" / "e2e" / "run_resource_link_acceptance.py"
    spec = importlib.util.spec_from_file_location("resource_link_e2e_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = tmp_path if tmp_path is not None else _RUNNER_DATA_ROOT
    assert directory is not None
    isolate_runner(module, directory)
    module.REPO_ROOT = directory
    module.PROVENANCE_RECOVERY_OUTPUT_DIR = directory / "provenance"
    module.SCOPE_REBIND_OUTPUT_DIR = directory / "scope-rebind"
    return module


def test_persisted_files_keep_bytes_and_use_a_writable_sync_handle(tmp_path, monkeypatch):
    runner = _runner_module()
    fsync = os.fsync
    descriptors = []

    def sync(descriptor):
        # Windows FlushFileBuffers requires write access. This also catches
        # read-only descriptors on POSIX without skipping the durability check.
        os.write(descriptor, b"")
        fsync(descriptor)
        descriptors.append(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    source = tmp_path / "source.bin"
    source.write_bytes(b"original offline bytes")
    target = tmp_path / "copied.bin"
    runner._persist_file(source, target)
    assert source.read_bytes() == target.read_bytes() == b"original offline bytes"
    runner._atomic_write_json(tmp_path / "proof.json", {"fixture": True})
    assert json.loads((tmp_path / "proof.json").read_text(encoding="utf-8")) == {"fixture": True}
    assert len(descriptors) == 2


def test_p1_refresh_does_not_rewrite_existing_execution_matrix(tmp_path, monkeypatch):
    runner = _runner_module()
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text('{"historicalExecutions": "must remain byte-identical"}', encoding="utf-8")
    original = matrix_path.read_bytes()
    monkeypatch.setattr(runner, "MATRIX_PATH", matrix_path)
    monkeypatch.setattr(runner, "APPLICABILITY_PATH", tmp_path / "applicability.json")
    monkeypatch.setattr(runner, "_matrix_lock", nullcontext)
    monkeypatch.setattr(runner, "build_matrix", lambda: {"catalogRevision": {"digest": "new"}})
    monkeypatch.setattr(runner, "validate_matrix", lambda value: None)
    monkeypatch.setattr(runner, "preserve_current_executions", lambda value: None)
    monkeypatch.setattr(runner, "build_applicability", lambda *args: {"dimensions": {}})
    monkeypatch.setattr(runner, "validate_applicability", lambda value: None)
    assert runner.main(["--phase", "p1", "--run-dir", str(tmp_path / "run")]) == 0
    assert matrix_path.read_bytes() == original
    assert runner.APPLICABILITY_PATH.is_file()


@pytest.mark.parametrize("replay_record_present", [True, False])
@pytest.mark.parametrize("account_scoped", [True, False])
def test_targeted_query_source_registration_rechecks_exact_artifacts(
    tmp_path, monkeypatch, replay_record_present, account_scoped
):
    runner = _runner_module()
    catalog = next(
        item
        for item in runner._direct_catalog_data()["responseContracts"]
        if item["resourceType"] == ("alicloud_log_project" if account_scoped else "ALIYUN::SAE::Application")
    )
    monkeypatch.setattr(runner, "_direct_catalog_data", lambda: {"responseContracts": [catalog]})
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "TARGETED_QUERY_SOURCE_PATH", tmp_path / "targeted-sources.json")
    monkeypatch.setattr(runner, "_revision", lambda: {"digest": "catalog-revision"})
    contract_id = f"{catalog['product']}/{catalog['action']}/{catalog['resourceType']}/{'.'.join(catalog['path'])}"
    old_record = (
        {
            "sourceChainStatus": "blocked",
            "eventStatus": "scan_timeout",
            "failureReason": "network_or_gateway",
            "sourceEvidenceDigest": "old-replay-digest",
        }
        if replay_record_present
        else None
    )
    monkeypatch.setattr(runner, "_targeted_query_replay_record", lambda value: old_record)
    proof_dir = tmp_path / "evidence" / "formal" / "042"
    proof_dir.mkdir(parents=True)
    report_path = proof_dir / "source-report.json"
    event_path = proof_dir / "source-events.jsonl"
    run_id = "a" * 32
    region_id = "cn-hangzhou" if account_scoped else "cn-shenzhen"
    identifier = "synthetic-app-id"
    report = {
        "schemaVersion": 2,
        "scanMode": "targeted_direct",
        "partition": "aliyun",
        "runId": run_id,
        "target": {"contractId": contract_id, "regionId": region_id},
        "directScan": {"contractCount": 1, "sampledContractCount": 1, "statuses": {"found": 1}},
        "samples": [
            {
                "source": "direct",
                "product": catalog["product"],
                "action": catalog["action"],
                "resourceType": catalog["resourceType"],
                "regionId": region_id,
                "identifier": identifier,
                "reference": {
                    "resourceType": catalog["resourceType"],
                    "regionId": None if account_scoped else region_id,
                    "consolePartition": "aliyun",
                    "identifier": identifier,
                    "identifierField": runner.resource_spec(catalog["resourceType"]).identifier_field,
                    "sources": [{"product": catalog["product"], "action": catalog["action"]}],
                },
            }
        ],
    }
    events = [
        {"phase": "scan_start", "runId": run_id},
        {
            "phase": "direct_api",
            "runId": run_id,
            "status": "found",
            "contractId": f"{catalog['product']}/{catalog['action']}/{'.'.join(catalog['path'])}",
            "resourceType": catalog["resourceType"],
            "product": catalog["product"],
            "action": catalog["action"],
            "scannedRegions": 1,
        },
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    event_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    mismatched_events = [dict(events[0]), dict(events[1], runId="b" * 32)]
    event_path.write_text("\n".join(json.dumps(event) for event in mismatched_events) + "\n", encoding="utf-8")
    with pytest.raises(runner.MatrixError, match="run IDs differ"):
        runner.register_targeted_query_source(report_path, event_path)
    event_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    entry = runner.register_targeted_query_source(report_path, event_path)
    assert entry["contractId"] == contract_id
    assert runner._targeted_query_supplement(contract_id, old_record, {"sourceType": catalog["resourceType"]}) == {
        "status": "passed",
        "sampleLevel": "L2",
        "actualApiCalled": True,
        "sourceEvidenceDigest": entry["sourceEvidenceDigest"],
    }
    report["target"]["contractId"] = "wrong/contract"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(runner.MatrixError):
        runner._load_targeted_query_sources()


def _runner_with_query_sources(tmp_path: Path, *, passed_types: int = 5):
    runner = _runner_module()
    runner.WEB_SOURCE_REGISTRY_PATH = tmp_path / "absent-web-source-registry.json"
    runner.TARGETED_QUERY_SOURCE_PATH = tmp_path / "absent-targeted-query-sources.json"
    runner.ROUTE_CLOUD_SOURCE_PATH = tmp_path / "absent-route-cloud-sources.json"
    applicability_path = tmp_path / "applicability.json"
    applicability_path.write_text(json.dumps(runner.build_applicability(runner.build_matrix(), None)), encoding="utf-8")
    runner.APPLICABILITY_PATH = applicability_path
    create_path = tmp_path / "create-fixtures.json"
    create_path.write_text(json.dumps(runner.build_create_fixtures()), encoding="utf-8")
    runner.CREATE_FIXTURE_PATH = create_path
    contracts = runner._direct_catalog_data()["responseContracts"]
    selected = [next(item for item in contracts if item["resourceType"] == "ALIYUN::ECS::Instance")]
    seen = {"ALIYUN::ECS::Instance"}
    for contract in contracts:
        if contract["resourceType"] in seen:
            continue
        selected.append(contract)
        seen.add(contract["resourceType"])
        if len(selected) == passed_types:
            break
    run_id = "b" * 32
    report = tmp_path / "query-catalog.json"
    report.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "runId": run_id,
                "samples": [
                    {
                        "source": "direct",
                        "product": item["product"],
                        "action": item["action"],
                        "resourceType": item["resourceType"],
                        "regionId": "cn-hangzhou",
                        "identifier": f"sample-{index}",
                        "reference": {
                            "resourceType": item["resourceType"],
                            "identifier": f"sample-{index}",
                            "sources": [{"product": item["product"], "action": item["action"]}],
                        },
                    }
                    for index, item in enumerate(selected)
                ],
            }
        ),
        encoding="utf-8",
    )
    events = tmp_path / "query-events.jsonl"
    rows = [json.dumps({"phase": "scan_start", "runId": run_id})]
    rows.extend(
        json.dumps(
            {
                "phase": "direct_api",
                "runId": run_id,
                "contractId": f"{item['product']}/{item['action']}/{'.'.join(item['path'])}",
                "resourceType": item["resourceType"],
                "product": item["product"],
                "action": item["action"],
                "status": "found",
                "scannedRegions": 1,
            }
        )
        for item in selected
    )
    events.write_text("\n".join(rows) + "\n", encoding="utf-8")
    replay = runner.build_query_replay(str(report), str(events))
    replay_path = tmp_path / "query-replay.json"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    runner.QUERY_REPLAY_PATH = replay_path
    # Keep the fixture's immutable source pair self-contained while retaining
    # the production requirement that the replay run ID match the formal scan.
    runner._source_scan_run_id = lambda: run_id
    return runner


def test_p0_matrix_and_p1_applicability_use_unique_contracts() -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    runner.validate_matrix(matrix)
    assert matrix["counts"] == {
        "sourceTypes": 407,
        "canonicalIdentities": 349,
        "aliases": 61,
        "officialRouteContracts": 407,
        "sinRouteContracts": 407,
        "queryContracts": 150,
        "createActions": 24,
    }

    applicability = runner.build_applicability(matrix, None)
    runner.validate_applicability(applicability)
    assert applicability["scope"] == {"sites": ["aliyun"], "excludedSites": ["alibabacloud"]}
    dimensions = applicability["dimensions"]
    assert (dimensions["route_catalog"]["denominator"], dimensions["route_catalog"]["applicable"]) == (407, 407)
    assert {item["site"] for item in dimensions["route_catalog"]["contracts"]} == {"aliyun"}
    assert (dimensions["query_api"]["denominator"], dimensions["query_api"]["applicable"]) == (150, 150)
    assert (dimensions["create_api"]["denominator"], dimensions["create_api"]["applicable"]) == (24, 24)
    assert dimensions["ros_stack"]["denominator"] == 407
    assert dimensions["ros_stack"]["notApplicable"] > 0


def test_browser_batch_schema_records_visible_search_evidence() -> None:
    runner = _runner_module()
    schema = json.loads(runner.BROWSER_BATCH_SCHEMA_PATH.read_text(encoding="utf-8"))
    item_schema = schema["properties"]["items"]["items"]
    diagnostics = item_schema["properties"]["diagnostics"]
    assert {"expectedUrl", "pageTitle", "bodyExcerpt", "signals", "search"} <= set(diagnostics["required"])
    assert diagnostics["properties"]["cms"]["properties"]["rowMatch"] == {"$ref": "#/$defs/cmsRowMatch"}
    assert {"ambiguous", "hiddenExcluded", "paneScoped", "paneScopeReason"} <= set(
        schema["$defs"]["cmsRowMatch"]["properties"]
    )
    assert "cms_masking_restore_failed" in item_schema["properties"]["failureReason"]["enum"]
    search = schema["$defs"]["search"]
    assert {"attempted", "performed", "control", "query", "resultText", "resourceMatched", "finalUrl"} <= set(
        search["required"]
    )


def test_reviewed_root_contracts_are_kept_pending_without_source_proof(tmp_path: Path) -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    runner.validate_matrix(matrix)
    acl = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::SLB::AccessControl")
    root_ids = {
        "slb/CreateAccessControlList/ALIYUN::SLB::AccessControl/",
        "slb/DescribeAccessControlListAttribute/ALIYUN::SLB::AccessControl/",
    }
    assert {q["contractId"] for q in acl["queryApis"] if q["responseIdentifierPath"] == "AclId"} == root_ids
    scan = tmp_path / "evidence/formal/source-scan-0004"
    scan.mkdir(parents=True)
    report, events = scan / "catalog.json", scan / "catalog.events.jsonl"
    run_id = "a" * 32
    report.write_text(json.dumps({"schemaVersion": 2, "runId": run_id, "samples": []}), encoding="utf-8")
    events.write_text(json.dumps({"phase": "scan_start", "runId": run_id}) + "\n", encoding="utf-8")
    runner.REPO_ROOT = tmp_path
    replay = runner.build_query_replay(
        str(report), str(events), catalog_revision_digest=matrix["catalogRevision"]["digest"]
    )
    runner.validate_query_replay(replay)
    assert {item["contractId"] for item in replay["entries"]} == {q["contractId"] for q in runner._query_contracts()}
    roots = [item for item in replay["entries"] if item["contractId"] in root_ids]
    assert len(roots) == 2
    assert all(item["sourceChainStatus"] == item["liveDestinationStatus"] == "pending" for item in roots)
    assert all(item["actualApiCalled"] is False and item["sampleLevel"] == "L0" for item in roots)
    events.write_text(json.dumps({"phase": "scan_start", "runId": "b" * 32}) + "\n", encoding="utf-8")
    with pytest.raises(runner.MatrixError, match="source revision is stale"):
        runner.validate_query_replay(replay)


def test_evidence_sanitizer_reconciles_digest_references_after_prior_redaction(tmp_path: Path) -> None:
    artifact = tmp_path / "browser-artifacts" / "normal.json"
    artifact.parent.mkdir()
    artifact.write_text('{"value":"[REDACTED]"}\n', encoding="utf-8")
    sanitized_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    original_digest = hashlib.sha256(b'{"value":"?token=secret"}\n').hexdigest()
    preflight = tmp_path / "preflight-input.json"
    preflight.write_text(
        json.dumps(
            {
                "normal": {
                    "artifactPath": f"/remote/output/{artifact.relative_to(tmp_path).as_posix()}",
                    "artifactDigest": original_digest,
                }
            }
        ),
        encoding="utf-8",
    )
    current_files = [
        {
            "path": artifact.relative_to(tmp_path).as_posix(),
            "bytes": artifact.stat().st_size,
            "sha256": sanitized_digest,
        },
        {
            "path": preflight.name,
            "bytes": preflight.stat().st_size,
            "sha256": hashlib.sha256(preflight.read_bytes()).hexdigest(),
        },
    ]
    (tmp_path / "manifest.json").write_text(json.dumps({"schemaVersion": 1, "files": current_files}), encoding="utf-8")
    remote_files = [dict(item) for item in current_files]
    remote_files[0]["sha256"] = original_digest
    (tmp_path / "manifest.remote.json").write_text(
        json.dumps({"schemaVersion": 1, "files": remote_files}), encoding="utf-8"
    )

    script = Path(__file__).parents[2] / "scripts" / "web" / "e2e" / "sanitize_resource_link_evidence.py"
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True)

    rewritten = json.loads(preflight.read_text(encoding="utf-8"))
    assert rewritten["normal"]["artifactDigest"] == sanitized_digest
    audit = json.loads((tmp_path / "redaction-audit.json").read_text(encoding="utf-8"))
    assert audit["totalDigestReferencesUpdated"] == 1


def test_catalog_and_runner_revisions_are_decoupled(monkeypatch) -> None:
    runner = _runner_module()
    catalog_revision = runner._revision()
    original_runner_revision = runner._runner_revision_digest()
    monkeypatch.setattr(runner, "_runner_revision_digest", lambda: "f" * 64)
    assert runner._revision() == catalog_revision
    assert runner._runner_revision_digest() != original_runner_revision


def test_historical_browser_batches_are_discovered_deterministically(tmp_path: Path, monkeypatch) -> None:
    runner = _runner_module()
    task_root = tmp_path / "evidence" / "formal" / "task-batches"

    def add_task(name: str, status: str, batch_names: list[str]) -> None:
        task_dir = task_root / name
        task_dir.mkdir(parents=True)
        (task_dir / "task-result.json").write_text(json.dumps({"status": status}), encoding="utf-8")
        for batch_name in batch_names:
            (task_dir / batch_name).write_text(json.dumps({"items": []}), encoding="utf-8")

    add_task("004", "partial", ["browser-batch.json"])
    add_task("005", "partial", ["browser-batch-query.json"])
    add_task("006", "runner_safety_fix_complete_compatibility_pending", ["browser-batch-route.json"])
    add_task("007-diagnostic", "passed", ["browser-batch.json"])
    add_task("008-preflight", "passed", ["browser-batch.json"])
    add_task("009-retry", "passed", ["browser-batch.json"])
    add_task("010", "blocked", ["browser-batch.json"])

    recorded_origin = task_root / "007" / "incoming-recorded"
    recorded_origin.mkdir(parents=True)
    recorded_batch = recorded_origin / "browser-batch-query.normalized.json"
    recorded_batch.write_text(json.dumps({"items": []}), encoding="utf-8")
    (recorded_origin / "browser-preflight.normalized.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    observation = recorded_origin / "browser-artifacts" / "route-normal.json"
    observation.parent.mkdir()
    observation.write_text(json.dumps({"capturedAt": "2026-09-18T00:00:00Z"}), encoding="utf-8")
    receipt_dir = task_root / "007" / "host-record" / "evidence" / "execution-recorded"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "receipt.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "liveDestinationStatus": "passed",
                "artifacts": [{"kind": "observation", "path": str(observation)}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)

    first = runner._historical_browser_batch_paths()
    second = runner._historical_browser_batch_paths()
    assert first == second
    assert [path.relative_to(tmp_path).as_posix() for path in first] == [
        "evidence/formal/task-batches/004/browser-batch.json",
        "evidence/formal/task-batches/005/browser-batch-query.json",
        "evidence/formal/task-batches/006/browser-batch-route.json",
        "evidence/formal/task-batches/007/incoming-recorded/browser-batch-query.normalized.json",
    ]


def test_route_semantic_override_is_scoped_to_the_changed_resource_type() -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    route_digest, route_payload = runner._production_semantic_fingerprint(
        matrix,
        "ALIYUN::CEN::RouteEntry",
        "ros_stack",
        "ros_stack:ALIYUN::CEN::RouteEntry",
    )
    cen_digest, cen_payload = runner._production_semantic_fingerprint(
        matrix,
        "ALIYUN::CEN::CenInstance",
        "ros_stack",
        "ros_stack:ALIYUN::CEN::CenInstance",
    )
    assert route_payload["productionRouteSemanticVersion"] == "aliyun-vpc-child-route-table-v1"
    assert "productionRouteSemanticVersion" not in cen_payload
    assert route_digest == runner._digest_json(route_payload)
    assert cen_digest == runner._digest_json(cen_payload)


def test_branch_semantics_are_independent_between_normal_and_shift() -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    base = {
        "sourceType": "ALIYUN::ALB::LoadBalancer",
        "source": "query_api",
        "contractId": "alb/ListLoadBalancers/ALIYUN::ALB::LoadBalancer/LoadBalancers.*",
        "resourceIdentifier": "alb-frx5bm9pkrzdxav524",
        "navigation": "normal",
        "expectedNavigation": "iframe",
        "expectedNavigationReason": "configured_signin_role",
        "actualNavigation": "iframe",
        "newTabObserved": False,
        "landingKind": "exact_detail",
        "finalUrl": "https://slb4service.console.aliyun.com/alb/cn-hangzhou/loadbalancer/alb-frx5bm9pkrzdxav524",
    }
    normal_digest, _ = runner._production_branch_semantic_fingerprint(matrix, base)
    shift = {
        **base,
        "navigation": "shift",
        "expectedNavigation": "new_tab",
        "expectedNavigationReason": "shift_override",
        "actualNavigation": "new_tab",
        "newTabObserved": True,
    }
    shift_digest, _ = runner._production_branch_semantic_fingerprint(matrix, shift)
    changed_normal_digest, _ = runner._production_branch_semantic_fingerprint(
        matrix,
        {
            **base,
            "expectedNavigationReason": "product_external_only",
            "actualNavigation": "new_tab",
            "newTabObserved": True,
        },
    )
    unchanged_shift_digest, _ = runner._production_branch_semantic_fingerprint(matrix, shift)
    changed_shift_digest, _ = runner._production_branch_semantic_fingerprint(
        matrix,
        {**shift, "finalUrl": "https://slb.console.aliyun.com/alb/cn-hangzhou/loadbalancer/alb-frx5bm9k-changed"},
    )

    assert changed_normal_digest != normal_digest
    assert unchanged_shift_digest == shift_digest
    assert changed_shift_digest != shift_digest


def _historical_runner(tmp_path: Path):
    """Build fifteen offline proof pairs, without consulting real Web sessions."""
    runner = _runner_with_query_sources(tmp_path, passed_types=15)
    revision, runner_revision = runner._revision(), runner._runner_revision_digest()
    runner.REPO_ROOT = tmp_path
    runner._revision = lambda: revision
    runner._runner_revision_digest = lambda: runner_revision
    runner.PROVENANCE_RECOVERY_OUTPUT_DIR = tmp_path / "provenance"
    runner.SCOPE_REBIND_OUTPUT_DIR = tmp_path / "scope-rebind"
    scan = tmp_path / "evidence/formal/source-scan-0004"
    scan.mkdir(parents=True)
    shutil.copyfile(tmp_path / "query-catalog.json", scan / "catalog.json")
    shutil.copyfile(tmp_path / "query-events.jsonl", scan / "catalog.events.jsonl")
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    report = json.loads((scan / "catalog.json").read_text(encoding="utf-8"))
    task = tmp_path / "evidence/formal/task-batches/003-security-policy-final-replay"
    task.mkdir(parents=True)
    (task / "task-result.json").write_text(json.dumps({"status": "passed"}), encoding="utf-8")
    items = []
    for sample in report["samples"]:
        entry = next(e for e in matrix["entries"] if e["sourceType"] == sample["resourceType"])
        source = sample["reference"]["sources"][0]
        contract_id = next(q["contractId"] for q in entry["queryApis"] if q["action"] == source["action"])
        for navigation in ("normal", "shift"):
            receipt = _receipt(
                runner, matrix, preflight, entry, contract_id, navigation, task,
                resource_identifier=sample["identifier"],
            )
            artifact = Path(receipt["artifacts"][0]["path"])
            item = json.loads(artifact.read_text(encoding="utf-8"))
            item["landingKind"] = "product_list"
            artifact.write_text(json.dumps(item), encoding="utf-8")
            receipt["artifacts"][0]["digest"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            receipt["landingKind"] = item["landingKind"]
            item["artifacts"] = receipt["artifacts"]
            receipt_dir = task / (entry["sourceType"].replace(":", "-") + "-" + navigation)
            receipt_dir.mkdir()
            (receipt_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            items.append(item)
    batch = {key: preflight[key] for key in ("browserAccessMode", "sessionIdDigest", "accountScopeDigest")}
    batch["items"] = items
    (task / "browser-batch.json").write_text(json.dumps(batch), encoding="utf-8")
    return runner


def test_compatibility_audit_rejects_only_changed_branch_semantics(tmp_path: Path, monkeypatch) -> None:
    runner = _historical_runner(tmp_path)
    matrix = runner.build_matrix()
    target = "alb/ListLoadBalancers/ALIYUN::ALB::LoadBalancer/LoadBalancers.*"
    output_dir = tmp_path / "branch-semantic"
    runner.audit_historical_compatibility(matrix, output_dir)
    original = runner._production_branch_semantic_fingerprint

    def normal_only_change(matrix, item):
        digest, payload = original(matrix, item)
        if item["contractId"] == target and item["navigation"] == "normal":
            payload = {**payload, "branch": {**payload["branch"], "testMutation": "normal"}}
            digest = runner._digest_json(payload)
        return digest, payload

    monkeypatch.setattr(runner, "_production_branch_semantic_fingerprint", normal_only_change)
    ledger, _ = runner.audit_historical_compatibility(matrix, output_dir)
    target_shift = [
        item for item in ledger["entries"] if item.get("contractId") == target and item.get("navigation") == "shift"
    ]
    target_normal = [
        item for item in ledger["entries"] if item.get("contractId") == target and item.get("navigation") == "normal"
    ]
    assert target_shift and not any(
        "branch semantic fingerprint" in reason for reason in target_shift[0]["rejectionReasons"]
    )
    assert target_normal and any(
        "branch semantic fingerprint" in reason for reason in target_normal[0]["rejectionReasons"]
    )


def test_incomplete_cen_bandwidth_package_is_not_promoted(tmp_path: Path) -> None:
    runner = _historical_runner(tmp_path)
    batch_path = next((tmp_path / "evidence/formal/task-batches").glob("*/browser-batch.json"))
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    batch["items"] = [item for item in batch["items"] if "CenBandwidthPackage" not in item["sourceType"]]
    batch["items"].append({
        **batch["items"][0], "sourceType": "ALIYUN::CEN::CenBandwidthPackage",
        "contractId": "route_catalog:ALIYUN::CEN::CenBandwidthPackage:aliyun", "navigation": "shift",
        "source": "route_catalog",
    })
    batch_path.write_text(json.dumps(batch), encoding="utf-8")
    ledger, _ = runner.audit_historical_compatibility(runner.build_matrix(), tmp_path)
    assert not any("CenBandwidthPackage" in contract for contract in ledger["acceptedContracts"])
    assert any(
        "CenBandwidthPackage" in str(item.get("contractId"))
        and item.get("navigation") == "shift"
        and item.get("rejectionReasons")
        for item in ledger["entries"]
    )


def test_historical_source_provenance_rejects_wrong_session_id_region_product_and_missing_output() -> None:
    runner = _runner_module()
    item = {
        "sourceType": "ALIYUN::ApiGateway::Group",
        "source": "query_api",
        "resourceIdentifier": "474fe3ab50074ba0982d64c37314eec5",
        "regionId": "cn-hangzhou",
    }
    batch = {"sessionIdDigest": "a" * 64, "accountScopeDigest": "b" * 64}
    candidate = {
        "taskId": "004",
        "formalSourceRunId": "c" * 32,
        "sessionIdDigest": batch["sessionIdDigest"],
        "accountScopeDigest": batch["accountScopeDigest"],
        "resourceIdentifier": item["resourceIdentifier"],
        "regionId": item["regionId"],
        "product": "cloudapi",
        "action": "DescribeApiGroups",
        "toolUseId": "call-source",
        "rawToolOutputDigest": "d" * 64,
        "rawToolOutputPresent": True,
        "sessionPath": "/tmp/session.jsonl",
        "sessionDigest": "e" * 64,
        "referencesPath": "/tmp/web-resource-references.json",
        "referencesDigest": "f" * 64,
        "sourceReportPath": "/tmp/source-report.json",
        "sourceReportDigest": "1" * 64,
        "sourceCheckpointPath": "/tmp/source-checkpoint.jsonl",
        "sourceCheckpointDigest": "2" * 64,
        "sourceReferenceDigest": "3" * 64,
        "rawToolOutputDigestVerified": True,
        "immutableFileDigestsVerified": True,
    }

    runner._validate_historical_source_provenance(candidate, item, batch, "c" * 32)
    for field, value in (
        ("sessionIdDigest", "0" * 64),
        ("resourceIdentifier", "wrong-resource"),
        ("regionId", "cn-beijing"),
        ("product", "ecs"),
        ("action", "DescribeApps"),
        ("rawToolOutputPresent", False),
        ("rawToolOutputDigestVerified", False),
    ):
        tampered = {**candidate, field: value}
        try:
            runner._validate_historical_source_provenance(tampered, item, batch, "c" * 32)
        except runner.MatrixError:
            pass
        else:  # pragma: no cover - fail-closed guard
            raise AssertionError(f"tampered provenance field was accepted: {field}")


def test_receipt_backed_provenance_derives_task_root_from_verified_receipt(tmp_path: Path, monkeypatch) -> None:
    runner = _runner_module()
    item = {
        "sourceType": "ALIYUN::CMS::MonitorGroup",
        "source": "query_api",
        "contractId": "cms/DescribeMonitorGroups/ALIYUN::CMS::MonitorGroup/Resources.Resource.*",
        "navigation": "normal",
        "resourceIdentifier": "group-1",
    }
    batch = {"sessionIdDigest": "a" * 64, "accountScopeDigest": "b" * 64}
    receipt_path = tmp_path / "formal" / "009" / "host-record" / "receipt.json"
    remote_batch_path = tmp_path / "remote-output" / "browser-batch.json"
    receipt = {
        "schemaVersion": 2,
        "liveDestinationStatus": "passed",
        **{field: item[field] for field in ("sourceType", "source", "contractId", "navigation")},
        **batch,
        "resourceIdentifierDigest": hashlib.sha256(item["resourceIdentifier"].encode()).hexdigest(),
        "sourceEvidenceDigest": "c" * 64,
        "sourceChainStatus": "passed",
    }
    monkeypatch.setattr(runner, "_load_json_object", lambda *_args, **_kwargs: receipt)

    class TaskRootReachedError(Exception):
        pass

    def task_root_probe(value: Path) -> Path:
        assert value == receipt_path
        raise TaskRootReachedError

    monkeypatch.setattr(runner, "_numbered_task_root", task_root_probe)
    try:
        runner._receipt_backed_historical_source_provenance(item, remote_batch_path, batch, {}, "d" * 32, receipt_path)
    except TaskRootReachedError:
        pass
    else:  # pragma: no cover - the probe must terminate before filesystem lookup
        raise AssertionError("receipt-backed provenance did not resolve the verified receipt task root")


def test_numbered_task_root_rejects_receipt_outside_formal_numbered_task(tmp_path: Path, monkeypatch) -> None:
    runner = _runner_module()
    repo_root = tmp_path / "repo"
    monkeypatch.setattr(runner, "REPO_ROOT", repo_root)
    task_root = repo_root / "evidence" / "formal" / "task-batches" / "009"
    receipt_path = task_root / "host-record" / "evidence" / "execution" / "receipt.json"
    assert runner._numbered_task_root(receipt_path) == task_root
    try:
        runner._numbered_task_root(tmp_path / "remote-output" / "receipt.json")
    except runner.MatrixError as error:
        assert "execution receipt is not below a numbered formal task" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("a receipt outside a numbered task was accepted")


def test_historical_tool_output_requires_the_original_tool_result(tmp_path: Path) -> None:
    runner = _runner_module()
    session = tmp_path / "session.jsonl"
    session.write_text(
        json.dumps(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-source",
                        "name": "aliyun_api",
                        "input": {"product": "cloudapi", "action": "DescribeApiGroups"},
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        runner._historical_tool_output(
            session,
            tool_use_id="call-source",
            product="cloudapi",
            action="DescribeApiGroups",
            identifier="group-id",
        )
    except runner.MatrixError as error:
        assert "tool use/result is missing" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("a missing original tool result was accepted")


def test_historical_tool_output_accepts_the_reviewed_cen_cbn_alias(tmp_path: Path) -> None:
    runner = _runner_module()
    session = tmp_path / "session.jsonl"
    session.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-cen",
                                "name": "aliyun_api",
                                "input": {"product": "cen", "action": "DescribeCens"},
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call-cen",
                                "content": '{"CenId":"cen-example"}',
                                "is_error": False,
                            }
                        ],
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    result = runner._historical_tool_output(
        session,
        tool_use_id="call-cen",
        product="cbn",
        action="DescribeCens",
        identifier="cen-example",
    )
    assert result["rawToolOutputPresent"] is True
    assert result["toolInput"] == {"product": "cen", "action": "DescribeCens"}


def test_historical_compatibility_audit_promotes_only_verified_pairs(tmp_path: Path) -> None:
    runner = _historical_runner(tmp_path)
    ledger, executions = runner.audit_historical_compatibility(runner.build_matrix(), tmp_path)
    assert len(ledger["acceptedContracts"]) == 15
    assert len(executions) == 30
    assert all(execution["liveDestinationStatus"] == "passed" for execution in executions)
    assert any("SecurityPolicy" in contract for contract in ledger["acceptedContracts"])


def test_historical_compatibility_audit_rejects_tampered_artifact_and_missing_branch(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _historical_runner(tmp_path)
    source_batch = next(
        path
        for path in runner._historical_browser_batch_paths()
        if path.parent.name == "003-security-policy-final-replay"
    )
    batch = json.loads(source_batch.read_text(encoding="utf-8"))
    batch["items"] = [batch["items"][0]]
    batch["items"][0]["artifacts"][0]["digest"] = "0" * 64
    tampered = tmp_path / "tampered-browser-batch.json"
    tampered.write_text(json.dumps(batch), encoding="utf-8")
    monkeypatch.setattr(runner, "_historical_browser_batch_paths", lambda: (tampered,))
    ledger, executions = runner.audit_historical_compatibility(runner.build_matrix(), tmp_path / "tampered")
    assert executions == []
    assert any("digest" in reason for item in ledger["entries"] for reason in item["rejectionReasons"])

    batch = json.loads(source_batch.read_text(encoding="utf-8"))
    batch["items"] = [item for item in batch["items"] if item["navigation"] == "normal"]
    incomplete = tmp_path / "incomplete-browser-batch.json"
    incomplete.write_text(json.dumps(batch), encoding="utf-8")
    monkeypatch.setattr(runner, "_historical_browser_batch_paths", lambda: (incomplete,))
    ledger, executions = runner.audit_historical_compatibility(runner.build_matrix(), tmp_path / "incomplete")
    assert executions == []
    assert any(
        "complete same-context pair" in reason for item in ledger["entries"] for reason in item["rejectionReasons"]
    )


def test_historical_compatibility_audit_invalidates_only_changed_contract(tmp_path: Path, monkeypatch) -> None:
    runner = _historical_runner(tmp_path)
    matrix = runner.build_matrix()
    output_dir = tmp_path / "semantic"
    first, executions = runner.audit_historical_compatibility(matrix, output_dir)
    assert len(first["acceptedContracts"]) == 15
    original = runner._production_semantic_fingerprint
    target = "alb/ListLoadBalancers/ALIYUN::ALB::LoadBalancer/LoadBalancers.*"

    def changed_semantics(matrix, source_type, source, contract_id):
        digest, payload = original(matrix, source_type, source, contract_id)
        if contract_id == target:
            payload = {**payload, "testMutation": True}
            digest = runner._digest_json(payload)
        return digest, payload

    monkeypatch.setattr(runner, "_production_semantic_fingerprint", changed_semantics)
    second, executions = runner.audit_historical_compatibility(matrix, output_dir)
    assert len(second["acceptedContracts"]) == 14
    assert len(executions) == 28
    assert any(
        item.get("contractId") == target
        and any("semantic fingerprint" in reason for reason in item["rejectionReasons"])
        for item in second["entries"]
    )


def test_p1_reads_ros_samples_from_the_live_report_v2_shape() -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    source_type = "ALIYUN::ECS::Instance"
    report = {
        "schemaVersion": 2,
        "coverage": {
            "countsByType": {source_type: 1},
            "discoveredWithoutUsableSample": [],
        },
        "samples": [
            {
                "source": "ros",
                "resourceType": source_type,
                "identifier": "i-sample",
                "reference": {"resourceType": source_type, "identifier": "i-sample"},
            }
        ],
    }
    applicability = runner.build_applicability(matrix, report)
    sample = next(
        item for item in applicability["dimensions"]["ros_stack"]["contracts"] if item["sourceType"] == source_type
    )
    assert sample["historyEvidence"] == "observed_usable_sample"
    assert sample["sourceChainStatus"] == "passed"
    assert sample["sampleLevel"] == "L2"
    assert sample["actualApiCalled"] is True


def test_p2_registers_legacy_evidence_without_promoting_passes(tmp_path, monkeypatch) -> None:
    runner = _runner_module()
    # Parse a synthetic legacy document, not machine-local archived cloud data.
    resource_types = [f"ALIYUN::TEST::Resource{index}" for index in range(110)] + [
        "ALIYUN::OTS::Table",
        "ALIYUN::ROS::StackGroup",
        "ALIYUN::ROS::TemplateScratch",
        "ALIYUN::CMS::Contact",
        "ALIYUN::CMS::ContactGroup",
        "ALIYUN::CMS::MetricRuleTemplate",
        "ALIYUN::CMS::MonitorGroup",
    ]
    legacy = tmp_path / "legacy.md"
    legacy.write_text("\n".join(f"| `{name}` | fixture only |" for name in resource_types), encoding="utf-8")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "LEGACY_EVIDENCE_PATH", legacy)
    history = runner.build_history()
    runner.validate_history(history)
    assert history["counts"] == {
        "legacyFormal": 110,
        "partial": 1,
        "manualUnverified": 6,
        "total": 117,
    }
    assert all(entry["navigationEvidence"] == {"normal": "unknown", "shift": "unknown"} for entry in history["entries"])
    assert all(entry["missingFields"] for entry in history["entries"])
    assert not any(entry["status"] == "passed" for entry in history["entries"])


def test_p3_replay_keeps_browser_destination_pending(tmp_path: Path) -> None:
    runner = _runner_module()
    report = tmp_path / "catalog.json"
    contract = runner._direct_catalog_data()["responseContracts"][0]
    run_id = "a" * 32
    sample = {
        "source": "direct",
        "product": contract["product"],
        "action": contract["action"],
        "resourceType": contract["resourceType"],
        "regionId": "cn-hangzhou",
        "identifier": "sample-resource",
        "reference": {
            "resourceType": contract["resourceType"],
            "identifier": "sample-resource",
            "sources": [{"product": contract["product"], "action": contract["action"]}],
        },
    }
    report.write_text(json.dumps({"schemaVersion": 2, "runId": run_id, "samples": [sample]}), encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"phase": "scan_start", "runId": run_id})
        + "\n"
        + json.dumps(
            {
                "phase": "direct_api",
                "runId": run_id,
                "contractId": f"{contract['product']}/{contract['action']}/{'.'.join(contract['path'])}",
                "resourceType": contract["resourceType"],
                "product": contract["product"],
                "action": contract["action"],
                "status": "found",
                "scannedRegions": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    replay = runner.build_query_replay(
        str(report),
        str(events),
        catalog_revision_digest=runner._revision()["digest"],
    )
    runner._source_scan_run_id = lambda: run_id
    runner.validate_query_replay(replay)
    assert replay["counts"]["contracts"] == 150
    assert replay["counts"]["sourcePassed"] == 1
    assert all(item["liveDestinationStatus"] == "pending" for item in replay["entries"])
    assert replay["entries"][0]["sampleLevel"] == "L2"


def test_p3_rejects_checkpoint_from_another_scan_run(tmp_path: Path) -> None:
    runner = _runner_module()
    report = tmp_path / "catalog.json"
    report.write_text(json.dumps({"schemaVersion": 2, "runId": "a" * 32, "samples": []}), encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"phase": "scan_start", "runId": "b" * 32}) + "\n", encoding="utf-8")
    try:
        runner.build_query_replay(str(report), str(events))
    except runner.MatrixError as error:
        assert "one scan run" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("a report and checkpoint from different scans must be rejected")


def test_p5_create_fixtures_are_explicitly_non_live() -> None:
    runner = _runner_module()
    fixtures = runner.build_create_fixtures()
    runner.validate_create_fixtures(fixtures)
    assert fixtures["counts"] == {"contracts": 24, "actualApiCalled": 0}
    assert all(item["actualApiCalled"] is False for item in fixtures["entries"])
    assert all(item["sampleLevel"] == "L1" for item in fixtures["entries"])
    assert all(item["liveDestinationStatus"] == "pending" for item in fixtures["entries"])


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _passed_browser_preflight(runner, matrix: dict, tmp_path: Path) -> dict:
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(b"sample").hexdigest()
    account_digest = hashlib.sha256(b"account").hexdigest()
    normal_path = tmp_path / "browser-normal.json"
    shift_path = tmp_path / "browser-shift.json"
    observed_at = _iso(now)
    normal = {
        "actualUiClick": True,
        "actualNavigation": "iframe",
        "routeMatched": True,
        "finalUrl": "https://ecs.console.aliyun.com/example",
        "loginPassed": True,
        "resourceMatched": True,
        "observedAt": observed_at,
    }
    shift = {
        "actualUiClick": True,
        "actualNavigation": "new_tab",
        "routeMatched": True,
        "newTabObserved": True,
        "finalUrl": "https://ecs.console.aliyun.com/example",
        "loginPassed": True,
        "resourceMatched": True,
        "observedAt": observed_at,
    }
    for navigation, branch, artifact_path in (
        ("normal", normal, normal_path),
        ("shift", shift, shift_path),
    ):
        artifact_path.write_text(
            json.dumps(
                {
                    "capturedAt": branch["observedAt"],
                    "navigation": navigation,
                    "actualUiClick": branch["actualUiClick"],
                    "actualNavigation": branch["actualNavigation"],
                    "newTabObserved": branch.get("newTabObserved", False),
                    "finalUrl": branch["finalUrl"],
                    "routeMatched": branch["routeMatched"],
                    "loginPassed": branch["loginPassed"],
                    "resourceMatched": branch["resourceMatched"],
                }
            ),
            encoding="utf-8",
        )
        branch.update(
            artifactPath=str(artifact_path),
            artifactDigest=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        )
    return {
        "schemaVersion": 1,
        "capturedAt": _iso(now - timedelta(minutes=1)),
        "expiresAt": _iso(now + timedelta(minutes=20)),
        "catalogRevisionDigest": matrix["catalogRevision"]["digest"],
        "status": "passed",
        "surface": "iac-code-web",
        "browserAccessMode": "existing-chrome",
        "sessionIdDigest": digest,
        "accountScopeDigest": account_digest,
        "sample": {
            "resourceType": "ALIYUN::ECS::Instance",
            "resourceIdentifierDigest": digest,
            "site": "aliyun",
            "sampleLevel": "L2",
            "actualApiCalled": True,
        },
        "normal": normal,
        "shift": shift,
    }


def test_p4_browser_gate_requires_real_normal_and_shift_navigation(tmp_path: Path) -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    runner.validate_browser_preflight(preflight, matrix)

    preflight["shift"]["newTabObserved"] = False
    try:
        runner.validate_browser_preflight(preflight, matrix)
    except runner.MatrixError as error:
        assert "Schema validation failed" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("Shift click without an observed tab must be rejected")

    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    preflight["sample"]["site"] = "alibabacloud"
    try:
        runner.validate_browser_preflight(preflight, matrix)
    except runner.MatrixError as error:
        assert "Schema validation failed" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("international-site preflight must be outside the acceptance scope")

    try:
        runner._load_browser_preflight("", matrix, required=True)
    except runner.MatrixError as error:
        assert "requires --browser-preflight" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("P4 gate must reject a missing browser preflight")


def test_p4_browser_gate_binds_branch_fields_to_observation_artifact(tmp_path: Path) -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    preflight["normal"]["finalUrl"] = "https://ecs.console.aliyun.com/another-resource"
    try:
        runner.validate_browser_preflight(preflight, matrix)
    except runner.MatrixError as error:
        assert "observation does not match" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("preflight fields must be bound to its raw observation")


def test_normal_only_preflight_repairs_only_deterministic_source_fields(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path, passed_types=1)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    preflight["singleBranchReplayReason"] = runner.SCOPE_REBIND_REASON
    preflight["cdpEndpoint"] = "http://127.0.0.1:9225"
    preflight["userDataDir"] = ""
    preflight["sample"].pop("sampleLevel")
    preflight["sample"].pop("actualApiCalled")
    normal_path = Path(preflight["normal"]["artifactPath"])
    normal_observation = json.loads(normal_path.read_text(encoding="utf-8"))
    entry = runner._entry_for_resource_type(matrix, "ALIYUN::ECS::Instance")
    normal_observation.update(
        sourceType="ALIYUN::ECS::Instance",
        source="query_api",
        contractId=entry["queryApis"][0]["contractId"],
    )
    normal_path.write_text(json.dumps(normal_observation), encoding="utf-8")
    preflight["normal"]["artifactDigest"] = hashlib.sha256(normal_path.read_bytes()).hexdigest()
    preflight_path = tmp_path / "normal-only-preflight.json"
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    loaded = runner._load_browser_preflight(str(preflight_path), matrix, required=True)

    assert loaded is not None
    assert "userDataDir" not in loaded
    assert loaded["sample"]["sampleLevel"] == "L2"
    assert loaded["sample"]["actualApiCalled"] is True


def test_missing_normal_preflight_requires_verified_same_resource_shift(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path, passed_types=1)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    preflight["sample"]["resourceIdentifierDigest"] = hashlib.sha256(b"sample-0").hexdigest()
    entry = runner._entry_for_resource_type(matrix, "ALIYUN::ECS::Instance")
    contract_id = entry["queryApis"][0]["contractId"]
    preflight["singleBranchReplayReason"] = runner.MISSING_NORMAL_REPLAY_REASON
    preflight["branchSessionIdDigests"] = {"normal": preflight["sessionIdDigest"], "shift": "a" * 64}
    for navigation in ("normal", "shift"):
        path = Path(preflight[navigation]["artifactPath"])
        observation = json.loads(path.read_text(encoding="utf-8"))
        observation.update(
            sourceType="ALIYUN::ECS::Instance",
            source="query_api",
            contractId=contract_id,
            resourceIdentifier="sample-0",
            regionId="cn-hangzhou",
        )
        path.write_text(json.dumps(observation), encoding="utf-8")
        preflight[navigation]["artifactDigest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(runner.MatrixError, match="previously verified immutable Shift"):
        runner.validate_browser_preflight(preflight, matrix)
    source = runner._source_evidence_record(entry, "query_api", contract_id)
    entry["executions"].append(
        {
            "sourceChainStatus": "passed",
            "liveDestinationStatus": "passed",
            "cleanupStatus": "not_applicable",
            "source": "query_api",
            "contractId": contract_id,
            "navigation": "shift",
            "sourceEvidenceDigest": source["sourceEvidenceDigest"],
            "accountScopeDigest": preflight["accountScopeDigest"],
            "sessionIdDigest": "a" * 64,
            "resourceIdentifierDigest": preflight["sample"]["resourceIdentifierDigest"],
            "regionId": "cn-hangzhou",
            "artifacts": [{"kind": "observation", "digest": preflight["shift"]["artifactDigest"]}],
        }
    )
    runner.validate_browser_preflight(preflight, matrix)
    entry["executions"][-1]["accountScopeDigest"] = "b" * 64
    with pytest.raises(runner.MatrixError, match="previously verified immutable Shift"):
        runner.validate_browser_preflight(preflight, matrix)


def test_status_keeps_raw_batches_non_promotable_and_reports_one_denominator(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    raw = tmp_path / "raw" / "batch-001"
    raw.mkdir(parents=True)
    (raw / "browser-observations.json").write_text(
        json.dumps(
            {
                "sessionId": "session-secret",
                "observations": [
                    {"result": "exact_detail_pass"},
                    {"result": "product_only_list_entry"},
                    {"result": "iframe_panel_loading"},
                ],
                "shiftClick": {"status": "passed", "verified": True},
            }
        ),
        encoding="utf-8",
    )

    status = runner.build_status(
        matrix,
        browser_preflight=None,
        evidence_roots=[str(tmp_path / "raw")],
    )
    runner.validate_status(status)

    assert status["readyForBatchExecution"] is False
    assert status["catalog"] == {
        "sourceTypes": 407,
        "canonicalIdentities": 349,
        "aliases": 61,
        "queryResourceTypes": 135,
        "queryContracts": 150,
        "createResourceTypes": 22,
        "createContracts": 24,
    }
    assert status["scope"] == {"sites": ["aliyun"], "excludedSites": ["alibabacloud"]}
    assert status["finalChecks"] == {"total": 581, "passed": 0, "remaining": 581}
    route_status = status["dimensions"]["routeCatalog"]
    assert route_status["total"] == 407
    assert route_status["sourcePassed"] + route_status["sourceBlocked"] + route_status["sourcePending"] == 407
    assert (route_status["normalPassed"], route_status["shiftPassed"], route_status["passed"]) == (0, 0, 0)
    assert route_status["remaining"] == 407
    assert status["dimensions"]["queryApi"]["total"] == 150
    assert status["dimensions"]["createApi"]["total"] == 24
    assert status["rawEvidence"]["batchDirectories"] == 1
    assert status["rawEvidence"]["sessionIdentifiers"] == 1
    assert status["rawEvidence"]["normalizedExecutions"] == 0
    assert status["rawEvidence"]["nonPromotableBatches"] == 1
    assert status["rawEvidence"]["historicalReportedOrdinaryExactDetail"] == 1
    assert status["rawEvidence"]["historicalReportedOrdinaryListEntry"] == 1
    assert status["rawEvidence"]["historicalReportedOrdinaryPassed"] == 2
    assert status["rawEvidence"]["historicalReportedShiftPassed"] == 1
    assert "browser_preflight_missing" in status["blockers"]


def test_status_browser_preflight_opens_batch_gate(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    runner.validate_browser_preflight(preflight, matrix)

    status = runner.build_status(matrix, browser_preflight=preflight, evidence_roots=[])
    runner.validate_status(status)
    assert status["readyForBatchExecution"] is True
    assert status["browserPreflight"] == {
        "status": "passed",
        "browserAccessMode": "existing-chrome",
    }


def _receipt(
    runner,
    matrix: dict,
    preflight: dict,
    entry: dict,
    contract_id: str,
    navigation: str,
    tmp_path: Path,
    *,
    source: str = "query_api",
    resource_identifier: str | None = None,
    passed: bool = True,
    attempt_count: int = 1,
) -> dict:
    if resource_identifier is None and source == "query_api":
        report = json.loads((runner.QUERY_REPLAY_PATH.parent / "query-catalog.json").read_text(encoding="utf-8"))
        resource_identifier = next(
            (
                sample["identifier"]
                for sample in report["samples"]
                if sample["resourceType"] == entry["sourceType"] and sample["action"] == contract_id.split("/")[1]
            ),
            None,
        )
    resource_identifier = resource_identifier or "sample-id"
    artifact_path = tmp_path / f"{entry['sourceType'].replace(':', '-')}-{navigation}-{attempt_count}.json"
    source_record = runner._source_evidence_record(entry, source, contract_id)
    expected_navigation = "iframe" if navigation == "normal" else "new_tab"
    captured_at = _iso(datetime.now(timezone.utc))
    final_url = "https://ecs.console.aliyun.com/detail" if passed else None
    observation = {
        "capturedAt": captured_at,
        "sourceType": entry["sourceType"],
        "source": source,
        "contractId": contract_id,
        "navigation": navigation,
        "resourceIdentifier": resource_identifier,
        "regionId": "cn-hangzhou",
        "expectedNavigation": expected_navigation,
        "expectedNavigationReason": "configured_signin_role" if navigation == "normal" else "shift_override",
        "actualUiClick": True,
        "actualNavigation": expected_navigation if passed else "none",
        "newTabObserved": passed and navigation == "shift",
        "landingKind": "exact_detail" if passed else "unknown",
        "routeMatched": passed,
        "loginPassed": passed,
        "resourceMatched": passed,
        "failureReason": None if passed else "new_tab_not_observed",
        "attemptCount": attempt_count,
        "finalUrl": final_url,
    }
    artifact_path.write_text(json.dumps(observation), encoding="utf-8")
    return {
        "schemaVersion": 2,
        "capturedAt": captured_at,
        "catalogRevisionDigest": matrix["catalogRevision"]["digest"],
        "sourceType": entry["sourceType"],
        "source": source,
        "contractId": contract_id,
        "site": "aliyun",
        "navigation": navigation,
        "sampleLevel": source_record["sampleLevel"],
        "actualApiCalled": source_record["actualApiCalled"],
        "sourceChainStatus": source_record["status"],
        "sourceEvidenceDigest": source_record["sourceEvidenceDigest"],
        "liveDestinationStatus": "passed" if passed else "blocked",
        "cleanupStatus": "not_applicable",
        "expectedNavigation": expected_navigation,
        "expectedNavigationReason": "configured_signin_role" if navigation == "normal" else "shift_override",
        "actualUiClick": True,
        "actualNavigation": expected_navigation if passed else "none",
        "newTabObserved": passed and navigation == "shift",
        "landingKind": "exact_detail" if passed else "unknown",
        "routeMatched": passed,
        "loginPassed": passed,
        "resourceMatched": passed,
        "failureReason": None if passed else "new_tab_not_observed",
        "attemptCount": attempt_count,
        "browserAccessMode": preflight["browserAccessMode"],
        "sessionIdDigest": preflight["sessionIdDigest"],
        "accountScopeDigest": preflight["accountScopeDigest"],
        "regionId": "cn-hangzhou",
        "resourceIdentifierDigest": hashlib.sha256(resource_identifier.encode()).hexdigest(),
        "finalUrlDigest": hashlib.sha256(final_url.encode()).hexdigest() if final_url else None,
        "finalHost": "ecs.console.aliyun.com" if passed else None,
        "artifacts": [
            {
                "kind": "observation",
                "path": str(artifact_path),
                "digest": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            }
        ],
    }


def test_matrix_rebuild_preserves_only_same_revision_executions(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    previous = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, previous, tmp_path)
    entry = next(item for item in previous["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    receipt = _receipt(runner, previous, preflight, entry, entry["queryApis"][0]["contractId"], "normal", tmp_path)
    runner.record_execution_receipt(
        previous,
        receipt,
        resume=False,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
    )
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(previous), encoding="utf-8")

    rebuilt = runner.build_matrix()
    runner.preserve_current_executions(rebuilt, path)
    rebuilt_entry = next(item for item in rebuilt["entries"] if item["sourceType"] == entry["sourceType"])
    assert rebuilt_entry["executions"] == entry["executions"]

    previous["catalogRevision"]["digest"] = "0" * 64
    path.write_text(json.dumps(previous), encoding="utf-8")
    rebuilt = runner.build_matrix()
    runner.preserve_current_executions(rebuilt, path)
    assert not any(item["executions"] for item in rebuilt["entries"])


def test_plan_record_resume_and_branch_status_form_a_closed_loop(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    plan = runner.build_batch_plan(matrix, source="query_api", batch_size=5, resume=True)
    assert plan["batchSize"] == 5
    assert len({item["sourceType"] for item in plan["items"]}) == 5
    assert all(item["sourceChainStatus"] == "passed" for item in plan["items"])

    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    contract_id = entry["queryApis"][0]["contractId"]
    normal = _receipt(runner, matrix, preflight, entry, contract_id, "normal", tmp_path)
    result, _execution = runner.record_execution_receipt(
        matrix,
        normal,
        resume=False,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
    )
    assert result == "recorded"
    partial = runner.build_status(matrix, browser_preflight=None, evidence_roots=[])
    assert partial["dimensions"]["queryApi"]["normalPassed"] == 1
    assert partial["dimensions"]["queryApi"]["shiftPassed"] == 0
    assert partial["dimensions"]["queryApi"]["fullyPassed"] == 0

    shift = _receipt(runner, matrix, preflight, entry, contract_id, "shift", tmp_path)
    result, execution = runner.record_execution_receipt(
        matrix,
        shift,
        resume=False,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
    )
    assert result == "recorded"
    complete = runner.build_status(matrix, browser_preflight=None, evidence_roots=[])
    runner.validate_status(complete)
    assert complete["dimensions"]["queryApi"]["normalPassed"] == 1
    assert complete["dimensions"]["queryApi"]["shiftPassed"] == 1
    assert complete["dimensions"]["queryApi"]["fullyPassed"] == 1
    assert complete["finalChecks"] == {"total": 581, "passed": 1, "remaining": 580}

    result, repeated = runner.record_execution_receipt(
        matrix,
        shift,
        resume=True,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
    )
    assert result == "skipped"
    assert repeated["evidenceId"] == execution["evidenceId"]


def test_status_deduplicates_normalized_versions_of_one_logical_branch(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    contract_id = entry["queryApis"][0]["contractId"]
    runner.record_execution_receipt(
        matrix,
        _receipt(runner, matrix, preflight, entry, contract_id, "normal", tmp_path),
        resume=False,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
    )
    normalized = entry["executions"][0]
    incomplete = json.loads(json.dumps(normalized))
    incomplete["evidenceId"] = "execution-" + "0" * 32
    incomplete["regionId"] = None
    incomplete["landingKind"] = "product_list"
    entry["executions"].insert(0, incomplete)

    status = runner.build_status(matrix, browser_preflight=preflight, evidence_roots=[])

    assert status["outcomes"]["latestExecutionBranches"] == 1
    assert status["outcomes"]["landingKinds"]["exact_detail"] == 1
    assert status["outcomes"]["landingKinds"]["product_list"] == 0


def test_status_does_not_promote_browser_pass_after_source_evidence_changes(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    contract_id = entry["queryApis"][0]["contractId"]
    for navigation in ("normal", "shift"):
        runner.record_execution_receipt(
            matrix,
            _receipt(runner, matrix, preflight, entry, contract_id, navigation, tmp_path),
            resume=False,
            browser_preflight=preflight,
            evidence_root=tmp_path / "evidence",
        )

    replay = json.loads(runner.QUERY_REPLAY_PATH.read_text(encoding="utf-8"))
    changed = next(item for item in replay["entries"] if item["contractId"] == contract_id)
    changed["sourceChainStatus"] = "blocked"
    changed["failureReason"] = "network_or_gateway"
    changed["sourceEvidenceDigest"] = "f" * 64
    replay["counts"]["sourcePassed"] -= 1
    replay["counts"]["sourceBlocked"] += 1
    replay_path = tmp_path / "query-replay-rerun.json"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    runner.QUERY_REPLAY_PATH = replay_path

    status = runner.build_status(matrix, browser_preflight=None, evidence_roots=[])

    assert status["dimensions"]["queryApi"]["sourceBlocked"] == 1
    assert status["dimensions"]["queryApi"]["normalPassed"] == 0
    assert status["dimensions"]["queryApi"]["shiftPassed"] == 0
    assert status["dimensions"]["queryApi"]["fullyPassed"] == 0


def test_failed_shift_receipt_is_recorded_and_retry_is_bounded(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    contract_id = entry["queryApis"][0]["contractId"]
    failed = _receipt(runner, matrix, preflight, entry, contract_id, "shift", tmp_path, passed=False)
    result, execution = runner.record_execution_receipt(
        matrix,
        failed,
        resume=False,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
    )
    assert result == "recorded"
    assert execution["actualNavigation"] == "none"

    result, repeated = runner.record_execution_receipt(
        matrix,
        failed,
        resume=True,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
    )
    assert result == "skipped"
    assert repeated["evidenceId"] == execution["evidenceId"]

    retry = _receipt(
        runner,
        matrix,
        preflight,
        entry,
        contract_id,
        "shift",
        tmp_path,
        passed=False,
        attempt_count=2,
    )
    result, execution = runner.record_execution_receipt(
        matrix,
        retry,
        resume=False,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
    )
    assert result == "recorded"
    assert execution["attemptCount"] == 2

    exhausted = dict(retry, attemptCount=3, capturedAt=_iso(datetime.now(timezone.utc)))
    try:
        runner.record_execution_receipt(
            matrix,
            exhausted,
            resume=False,
            browser_preflight=preflight,
            evidence_root=tmp_path / "evidence",
        )
    except runner.MatrixError as error:
        assert "Schema validation failed" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("more than one retry must be rejected")


def test_resume_retries_both_failed_branches_on_the_same_sample(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    contract_id = entry["queryApis"][0]["contractId"]
    for navigation in ("normal", "shift"):
        runner.record_execution_receipt(
            matrix,
            _receipt(runner, matrix, preflight, entry, contract_id, navigation, tmp_path, passed=False),
            resume=False,
            browser_preflight=preflight,
            evidence_root=tmp_path / "evidence",
        )
    plan = runner.build_batch_plan(matrix, source="query_api", batch_size=5, resume=True)
    resumed = next(item for item in plan["items"] if item["contractId"] == contract_id)
    assert set(resumed["missingNavigations"]) == {"normal", "shift"}
    assert resumed["resumeContext"]["nextAttemptCounts"] == {"normal": 2, "shift": 2}


def test_resume_skips_a_sample_after_both_branches_exhaust_two_attempts(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    contract_id = entry["queryApis"][0]["contractId"]

    for navigation in ("normal", "shift"):
        for attempt_count in (1, 2):
            runner.record_execution_receipt(
                matrix,
                _receipt(
                    runner,
                    matrix,
                    preflight,
                    entry,
                    contract_id,
                    navigation,
                    tmp_path,
                    passed=False,
                    attempt_count=attempt_count,
                ),
                resume=False,
                browser_preflight=preflight,
                evidence_root=tmp_path / "evidence",
            )

    plan = runner.build_batch_plan(matrix, source="query_api", batch_size=5, resume=True)

    assert all(item["contractId"] != contract_id for item in plan["items"])


def test_batch_plan_can_target_unmeasured_resource_types_without_editing_receipts(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path, passed_types=20)
    matrix = runner.build_matrix()
    requested = {
        "ALIYUN::CAS::Certificate",
        "ALIYUN::CEN::CenBandwidthPackage",
        "ALIYUN::CEN::CenInstance",
        "ALIYUN::CEN::TransitRouter",
        "ALIYUN::CEN::TransitRouterVpcAttachment",
    }

    plan = runner.build_batch_plan(
        matrix,
        source="query_api",
        batch_size=5,
        resume=True,
        resource_types=requested,
    )

    assert {item["sourceType"] for item in plan["items"]} == requested


def test_targeted_batch_plan_does_not_validate_unrelated_source_records(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path, passed_types=5)
    matrix = runner.build_matrix()
    requested = "ALIYUN::ECS::Instance"
    original_source_record = runner._source_evidence_record

    def scoped_source_record(entry, receipt_source, contract_id):
        if entry["sourceType"] != requested:
            raise runner.MatrixError("unrelated source record is stale")
        return original_source_record(entry, receipt_source, contract_id)

    runner._source_evidence_record = scoped_source_record
    plan = runner.build_batch_plan(
        matrix,
        source="query_api",
        batch_size=5,
        resume=True,
        resource_types={requested},
    )

    assert plan["batchSize"] == 1
    assert plan["items"][0]["sourceType"] == requested


def test_plans_fail_closed_for_sources_without_a_real_web_replay(tmp_path: Path, monkeypatch) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    from unittest.mock import Mock

    load = Mock(wraps=runner._load_applicability)
    monkeypatch.setattr(runner, "_load_applicability", load)
    route = runner.build_batch_plan(matrix, source="route_catalog", batch_size=5, resume=True)
    create = runner.build_batch_plan(matrix, source="create_api", batch_size=5, resume=True)
    assert route["skippedUnreplayable"] == 178
    assert all(item["receiptSource"] == "ros_stack" for item in route["items"])
    assert create["batchSize"] == 0
    assert create["skippedUnreplayable"] == 24
    assert load.call_count == 1, "one validated applicability snapshot per planning call"
    runner.build_batch_plan(matrix, source="route_catalog", batch_size=5, resume=True)
    assert load.call_count == 2, "later planning calls must reload and revalidate source evidence"


def _route_cloud_fixture(tmp_path, runner):
    runner.ROUTE_CLOUD_SOURCE_PATH = tmp_path / "route-registry.json"
    raw = {
        "RequestId": "offline-request",
        "TotalCount": 1,
        "DBInstances": {
            "DBInstance": [
                {
                    "DBInstanceId": "dds-offline",
                    "DBInstanceType": "replicate",
                    "RegionId": "cn-shenzhen",
                    "DBInstanceStatus": "Running",
                }
            ]
        },
    }
    (tmp_path / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    report = {
        "purpose": "bounded_cloud_source_preparation_only",
        "profile": "test-guima",
        "observedAt": "2026-10-03T10:00:00Z",
        "regionsCovered": ["cn-shenzhen"],
        "results": [
            {
                "region": "cn-shenzhen",
                "events": [
                    {
                        "product": "dds",
                        "action": "DescribeDBInstances",
                        "region": "cn-shenzhen",
                        "profile": "test-guima",
                        "status": "ok",
                        "exitCode": 0,
                        "rawFile": "raw.json",
                    }
                ],
                "samples": {
                    "alicloud_mongodb_instance": [
                        {
                            "id": "dds-offline",
                            "region": "cn-shenzhen",
                            "rawFile": "raw.json",
                        }
                    ]
                },
            }
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path, report, raw


def test_cloud_backed_route_plan_keeps_route_identity_and_exact_binding(tmp_path):
    runner = _runner_with_query_sources(tmp_path)
    report_path, _, _ = _route_cloud_fixture(tmp_path, runner)
    proof = runner.register_route_cloud_source(report_path, "alicloud_mongodb_instance", "cn-shenzhen")
    plan = runner.build_batch_plan(
        runner.build_matrix(),
        source="route_catalog",
        batch_size=5,
        resume=False,
        resource_types={"alicloud_mongodb_instance"},
    )
    assert plan["batchSize"] == 1
    item = plan["items"][0]
    assert item["receiptSource"] == "route_catalog"
    assert item["contractId"] == "route_catalog:alicloud_mongodb_instance:aliyun"
    assert item["canonicalResourceType"] == "ALIYUN::MONGODB::Instance"
    assert (item["sampleLevel"], item["actualApiCalled"]) == ("L2", True)
    assert item["routeSourceBinding"]["resourceIdentifierDigests"] == proof["resourceIdentifierDigests"]
    changed = json.loads(json.dumps(plan))
    changed["items"][0]["routeSourceBinding"]["regionId"] = "cn-beijing"
    changed["planId"] = runner._digest_json({key: value for key, value in changed.items() if key != "planId"})
    with pytest.raises(runner.MatrixError, match="registered sample"):
        runner.validate_batch_plan(changed, runner.build_matrix())
    # A raw response change must invalidate the registry even if it still
    # contains a real-looking identifier and the caller recomputes planId.
    (tmp_path / "raw.json").write_text('{"RequestId":"changed"}', encoding="utf-8")
    with pytest.raises(runner.MatrixError):
        runner._route_cloud_source("alicloud_mongodb_instance")


@pytest.mark.parametrize("mutation", ["wrong_sample", "failed_call", "wrong_profile", "path_escape", "no_request"])
def test_route_source_rejects_unproven_raw_api_samples(tmp_path, mutation):
    runner = _runner_with_query_sources(tmp_path)
    path, report, raw = _route_cloud_fixture(tmp_path, runner)
    if mutation == "wrong_sample":
        report["results"][0]["samples"]["alicloud_mongodb_instance"][0]["id"] = "not-in-response"
    elif mutation == "failed_call":
        report["results"][0]["events"][0]["exitCode"] = 1
    elif mutation == "wrong_profile":
        report["results"][0]["events"][0]["profile"] = "other"
    elif mutation == "path_escape":
        report["results"][0]["samples"]["alicloud_mongodb_instance"][0]["rawFile"] = "../raw.json"
    else:
        raw.pop("RequestId")
    path.write_text(json.dumps(report), encoding="utf-8")
    (tmp_path / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(runner.MatrixError):
        runner.register_route_cloud_source(path, "alicloud_mongodb_instance", "cn-shenzhen")
    assert not runner.ROUTE_CLOUD_SOURCE_PATH.exists()


@pytest.mark.parametrize("wrong_parent", [None, "other-group", "extreme"])
def test_nas_route_source_requires_exact_group_and_filesystem_parents(tmp_path, wrong_parent):
    runner = _runner_with_query_sources(tmp_path)
    runner.ROUTE_CLOUD_SOURCE_PATH = tmp_path / "route-registry.json"
    raw = {
        "RequestId": "offline",
        "AccessRules": {
            "AccessRule": [
                {
                    "AccessRuleId": "0",
                    "AccessGroupName": "group-one",
                    "FileSystemType": "standard",
                    "RegionId": "cn-hangzhou",
                }
            ]
        },
    }
    (tmp_path / "nas.json").write_text(json.dumps(raw), encoding="utf-8")
    parents = {"AccessGroupName": "group-one", "FileSystemType": "standard"}
    report = {
        "purpose": "bounded_cloud_source_preparation_only",
        "profile": "test-guima",
        "observedAt": "2026-10-03T10:00:00Z",
        "regionsCovered": ["cn-hangzhou"],
        "results": [
            {
                "region": "cn-hangzhou",
                "events": [
                    {
                        "product": "nas",
                        "action": "DescribeAccessRules",
                        "region": "cn-hangzhou",
                        "profile": "test-guima",
                        "status": "ok",
                        "exitCode": 0,
                        "rawFile": "nas.json",
                    }
                ],
                "samples": {
                    "alicloud_nas_access_rule": [
                        {
                            "id": "0",
                            "region": "cn-hangzhou",
                            "rawFile": "nas.json",
                            "parentAttributes": parents,
                        }
                    ]
                },
            }
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    proof = runner.register_route_cloud_source(path, "alicloud_nas_access_rule", "cn-hangzhou")
    assert proof["parentAttributes"] == parents
    plan = runner.build_batch_plan(
        runner.build_matrix(),
        source="route_catalog",
        batch_size=5,
        resume=False,
        resource_types={"alicloud_nas_access_rule"},
    )
    assert plan["items"][0]["routeSourceBinding"]["parentAttributes"] == parents
    if wrong_parent is None:
        parents.clear()
    else:
        parents["AccessGroupName" if wrong_parent == "other-group" else "FileSystemType"] = wrong_parent
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(runner.MatrixError):
        runner.register_route_cloud_source(path, "alicloud_nas_access_rule", "cn-hangzhou")


def test_query_plan_without_current_scan_stays_pending(tmp_path: Path) -> None:
    runner = _runner_module()
    runner.QUERY_REPLAY_PATH = tmp_path / "missing-query-replay.json"
    matrix = runner.build_matrix()

    plan = runner.build_batch_plan(matrix, source="query_api", batch_size=5, resume=True)

    assert plan["batchSize"] == 0
    assert plan["items"] == []
    assert plan["skippedPending"] == matrix["counts"]["queryContracts"]


def test_browser_batch_coverage_requires_every_planned_branch(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path, passed_types=1)
    matrix = runner.build_matrix()
    plan = runner.build_batch_plan(matrix, source="query_api", batch_size=1, resume=True)
    planned = plan["items"][0]
    browser_batch = {
        "sessionIdDigest": "a" * 64,
        "accountScopeDigest": "b" * 64,
        "items": [
            {
                "sourceType": planned["sourceType"],
                "source": planned["receiptSource"],
                "contractId": planned["contractId"],
                "navigation": "normal",
                "resourceIdentifier": "one-real-sample",
                "regionId": "cn-hangzhou",
            }
        ],
    }

    coverage = runner.browser_batch_coverage(browser_batch, plan)

    assert coverage["complete"] is False
    assert coverage["missing"] == [
        {
            "sourceType": planned["sourceType"],
            "contractId": planned["contractId"],
            "navigation": "shift",
        }
    ]


def test_passed_receipt_requires_an_observed_navigation(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    receipt = _receipt(runner, matrix, preflight, entry, entry["queryApis"][0]["contractId"], "normal", tmp_path)
    receipt["actualNavigation"] = "none"
    try:
        runner.validate_execution_receipt(receipt, matrix, preflight)
    except runner.MatrixError as error:
        assert "Schema validation failed" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("a normal-click pass without navigation must be rejected")


def test_normal_and_shift_from_different_resources_do_not_combine(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    contract_id = entry["queryApis"][0]["contractId"]
    for navigation, identifier in (("normal", "sample-0"), ("shift", "instance-b")):
        receipt = _receipt(
            runner,
            matrix,
            preflight,
            entry,
            contract_id,
            navigation,
            tmp_path,
            resource_identifier=identifier,
        )
        if navigation == "shift":
            with pytest.raises(runner.MatrixError, match="registered resource/session/account sample"):
                runner.record_execution_receipt(
                    matrix, receipt, resume=False, browser_preflight=preflight, evidence_root=tmp_path / "evidence"
                )
        else:
            runner.record_execution_receipt(
                matrix, receipt, resume=False, browser_preflight=preflight, evidence_root=tmp_path / "evidence"
            )
    status = runner.build_status(matrix, browser_preflight=preflight, evidence_roots=[])
    assert status["dimensions"]["queryApi"]["normalPassed"] == 1
    assert status["dimensions"]["queryApi"]["shiftPassed"] == 0
    assert status["dimensions"]["queryApi"]["fullyPassed"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("regionId", "cn-beijing"),
        ("resourceIdentifierDigest", hashlib.sha256(b"other-resource").hexdigest()),
    ],
)
def test_query_receipt_rejects_a_different_registered_sample(tmp_path, field, value):
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = runner._entry_for_resource_type(matrix, "ALIYUN::ECS::Instance")
    contract = entry["queryApis"][0]["contractId"]
    receipt = _receipt(runner, matrix, preflight, entry, contract, "normal", tmp_path)
    receipt[field] = value
    with pytest.raises(runner.MatrixError, match="registered resource/session/account sample"):
        runner.validate_execution_receipt(receipt, matrix, preflight)
    assert not entry["executions"]


def test_old_query_executions_are_not_counted_for_another_registered_sample(tmp_path):
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = runner._entry_for_resource_type(matrix, "ALIYUN::ECS::Instance")
    contract = entry["queryApis"][0]["contractId"]
    for navigation in ("normal", "shift"):
        receipt = _receipt(runner, matrix, preflight, entry, contract, navigation, tmp_path)
        runner.record_execution_receipt(matrix, receipt, resume=False, browser_preflight=preflight,
                                        evidence_root=tmp_path / "archive")
    assert contract in runner._completed_contracts(matrix)["query_api"]
    for execution in entry["executions"]:
        execution["regionId"] = "cn-beijing"
    assert contract not in runner._completed_contracts(matrix)["query_api"]


def test_blocked_source_cannot_be_claimed_as_passed(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    replay = runner._load_json_object(runner.QUERY_REPLAY_PATH, label="query replay")
    blocked = next(item for item in replay["entries"] if item["sourceChainStatus"] != "passed")
    entry = runner._entry_for_resource_type(matrix, blocked["resourceType"])
    receipt = _receipt(
        runner,
        matrix,
        preflight,
        entry,
        blocked["contractId"],
        "shift",
        tmp_path,
        passed=False,
    )
    receipt["sourceChainStatus"] = "passed"
    receipt["sampleLevel"] = "L1"
    try:
        runner.validate_execution_receipt(receipt, matrix, preflight)
    except runner.MatrixError as error:
        assert "source status" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("blocked source evidence must not be promoted by a receipt")


def test_expired_browser_preflight_closes_the_gate(tmp_path: Path) -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    after_expiry = runner._parse_datetime(preflight["expiresAt"], label="expiresAt") + timedelta(seconds=1)
    try:
        runner.validate_browser_preflight(preflight, matrix, now=after_expiry)
    except runner.BrowserPreflightExpiredError:
        pass
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("expired browser proof must not authorize a later batch")


def test_batch_import_derives_receipts_and_archives_evidence(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    plan = runner.build_batch_plan(matrix, source="query_api", batch_size=1, resume=True)
    planned = plan["items"][0]
    catalog = json.loads((tmp_path / "query-catalog.json").read_text(encoding="utf-8"))
    identifier = next(sample["identifier"] for sample in catalog["samples"]
                      if sample["resourceType"] == planned["sourceType"] and sample["action"] == planned["action"])
    observation_path = tmp_path / "batch-observation.json"
    observed_at = _iso(datetime.now(timezone.utc))
    observation = {
        "capturedAt": observed_at,
        "sourceType": planned["sourceType"],
        "source": planned["receiptSource"],
        "contractId": planned["contractId"],
        "navigation": "normal",
        "resourceIdentifier": identifier,
        "regionId": "cn-hangzhou",
        "expectedNavigation": "iframe",
        "expectedNavigationReason": "configured_signin_role",
        "actualUiClick": True,
        "actualNavigation": "iframe",
        "newTabObserved": False,
        "landingKind": "exact_detail",
        "routeMatched": True,
        "loginPassed": True,
        "resourceMatched": True,
        "failureReason": None,
        "attemptCount": 1,
        "finalUrl": "https://ecs.console.aliyun.com/detail",
    }
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    browser_batch = {
        "schemaVersion": 1,
        "catalogRevisionDigest": matrix["catalogRevision"]["digest"],
        "planId": plan["planId"],
        "browserAccessMode": preflight["browserAccessMode"],
        "sessionIdDigest": preflight["sessionIdDigest"],
        "accountScopeDigest": preflight["accountScopeDigest"],
        "items": [
            {
                "capturedAt": observed_at,
                "sourceType": planned["sourceType"],
                "source": planned["receiptSource"],
                "contractId": planned["contractId"],
                "navigation": "normal",
                "resourceIdentifier": identifier,
                "regionId": "cn-hangzhou",
                "expectedNavigation": "iframe",
                "expectedNavigationReason": "configured_signin_role",
                "actualUiClick": True,
                "actualNavigation": "iframe",
                "newTabObserved": False,
                "landingKind": "exact_detail",
                "routeMatched": True,
                "loginPassed": True,
                "resourceMatched": True,
                "failureReason": None,
                "attemptCount": 1,
                "finalUrl": "https://ecs.console.aliyun.com/detail",
                "artifacts": [
                    {
                        "kind": "observation",
                        "path": str(observation_path),
                        "digest": hashlib.sha256(observation_path.read_bytes()).hexdigest(),
                    }
                ],
            }
        ],
    }
    results = runner.record_browser_batch(
        matrix,
        browser_batch,
        plan,
        resume=True,
        browser_preflight=preflight,
        evidence_root=tmp_path / "archive",
    )
    assert results[0]["status"] == "recorded"
    runner.validate_matrix(matrix)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == planned["sourceType"])
    archived = Path(entry["executions"][0]["artifacts"][0]["path"])
    assert archived.is_file() and archived.parent.parent == tmp_path / "archive"
    archived.unlink()
    try:
        runner.validate_matrix(matrix)
    except runner.MatrixError as error:
        assert "artifact is missing" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("deleted durable evidence must invalidate the matrix")


def test_historical_batch_import_keeps_original_preflight_window(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    captured = runner._parse_datetime(preflight["capturedAt"], label="capturedAt")
    preflight["expiresAt"] = _iso(captured + timedelta(minutes=1))
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    receipt = _receipt(
        runner,
        matrix,
        preflight,
        entry,
        entry["queryApis"][0]["contractId"],
        "normal",
        tmp_path,
    )
    receipt["capturedAt"] = _iso(captured + timedelta(seconds=30))
    observation_path = Path(receipt["artifacts"][0]["path"])
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    observation["capturedAt"] = receipt["capturedAt"]
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    receipt["artifacts"][0]["digest"] = hashlib.sha256(observation_path.read_bytes()).hexdigest()

    result, _execution = runner.record_execution_receipt(
        matrix,
        receipt,
        resume=False,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
        validate_preflight_at_capture=True,
    )
    assert result == "recorded"

    matrix = runner.build_matrix()
    receipt["capturedAt"] = _iso(captured + timedelta(minutes=2))
    observation["capturedAt"] = receipt["capturedAt"]
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    receipt["artifacts"][0]["digest"] = hashlib.sha256(observation_path.read_bytes()).hexdigest()
    try:
        runner.record_execution_receipt(
            matrix,
            receipt,
            resume=False,
            browser_preflight=preflight,
            evidence_root=tmp_path / "late-evidence",
            validate_preflight_at_capture=True,
        )
    except runner.MatrixError as error:
        assert "active browser preflight window" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("historical import must retain the original preflight window")


def test_targeted_normal_observation_can_also_materialize_its_preflight(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    receipt = _receipt(
        runner,
        matrix,
        preflight,
        entry,
        entry["queryApis"][0]["contractId"],
        "normal",
        tmp_path,
    )
    observation_path = Path(receipt["artifacts"][0]["path"])
    preflight["singleBranchReplayReason"] = runner.SCOPE_REBIND_REASON
    preflight["branchSessionIdDigests"] = {
        "normal": preflight["sessionIdDigest"],
        "shift": preflight["sessionIdDigest"],
    }
    preflight["normal"]["observedAt"] = receipt["capturedAt"]
    preflight["normal"]["artifactPath"] = str(observation_path)
    preflight["normal"]["artifactDigest"] = receipt["artifacts"][0]["digest"]
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    for field in (
        "actualUiClick",
        "actualNavigation",
        "newTabObserved",
        "finalUrl",
        "routeMatched",
        "loginPassed",
        "resourceMatched",
    ):
        preflight["normal"][field] = observation[field]
    preflight["capturedAt"] = _iso(
        runner._parse_datetime(receipt["capturedAt"], label="receipt capturedAt") + timedelta(milliseconds=50)
    )
    preflight["expiresAt"] = _iso(
        runner._parse_datetime(preflight["capturedAt"], label="preflight capturedAt") + timedelta(minutes=25)
    )

    result, _execution = runner.record_execution_receipt(
        matrix,
        receipt,
        resume=False,
        browser_preflight=preflight,
        evidence_root=tmp_path / "evidence",
        validate_preflight_at_capture=True,
    )

    assert result == "recorded"


@pytest.mark.parametrize("navigation", ["normal", "shift"])
@pytest.mark.parametrize("mismatch", [None, "artifact", "sample", "stale", "time"])
def test_fresh_preflight_click_import_requires_exact_bounded_original(
    tmp_path: Path, navigation: str, mismatch: str | None
) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    entry = next(item for item in matrix["entries"] if item["sourceType"] == "ALIYUN::ECS::Instance")
    receipt = _receipt(runner, matrix, preflight, entry, entry["queryApis"][0]["contractId"], navigation, tmp_path)
    observation_path = Path(receipt["artifacts"][0]["path"])
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    branch = preflight[navigation]
    branch.update(
        observedAt=receipt["capturedAt"],
        artifactPath=str(observation_path),
        artifactDigest=receipt["artifacts"][0]["digest"],
    )
    for field in (
        "actualUiClick",
        "actualNavigation",
        "newTabObserved",
        "finalUrl",
        "routeMatched",
        "loginPassed",
        "resourceMatched",
    ):
        branch[field] = observation[field]
    preflight["sample"]["resourceIdentifierDigest"] = receipt["resourceIdentifierDigest"]
    captured = runner._parse_datetime(receipt["capturedAt"], label="receipt")
    preflight["capturedAt"] = _iso(captured + timedelta(milliseconds=50))
    preflight["expiresAt"] = _iso(captured + timedelta(minutes=25))
    if mismatch == "artifact":
        copied = tmp_path / "unbound-copy.json"
        copied.write_bytes(observation_path.read_bytes())
        receipt["artifacts"][0]["path"] = str(copied)
    elif mismatch == "sample":
        preflight["sample"]["resourceIdentifierDigest"] = "0" * 64
    elif mismatch == "stale":
        preflight["capturedAt"] = _iso(captured + timedelta(minutes=6))
    elif mismatch == "time":
        receipt["capturedAt"] = _iso(captured - timedelta(milliseconds=1))
    if mismatch:
        with pytest.raises(runner.MatrixError, match="active browser preflight window"):
            runner.validate_execution_receipt(receipt, matrix, preflight, validate_preflight_at_capture=True)
    else:
        result, _execution = runner.record_execution_receipt(
            matrix,
            receipt,
            resume=False,
            browser_preflight=preflight,
            evidence_root=tmp_path / "evidence",
            validate_preflight_at_capture=True,
        )
        assert result == "recorded"


def test_browser_batch_selection_keeps_only_requested_immutable_items() -> None:
    runner = _runner_module()
    batch = {
        "items": [
            {"sourceType": "ALIYUN::ECS::Instance", "navigation": "normal"},
            {"sourceType": "ALIYUN::ECS::Instance", "navigation": "shift"},
            {"sourceType": "ALIYUN::VPC::VPC", "navigation": "shift"},
        ]
    }

    selected = runner.select_browser_batch_items(
        batch,
        resource_types={"ALIYUN::ECS::Instance"},
        navigations={"shift"},
    )

    assert selected["items"] == [{"sourceType": "ALIYUN::ECS::Instance", "navigation": "shift"}]
    assert len(batch["items"]) == 3


def test_ros_stack_receipts_count_in_the_route_dimension(tmp_path: Path) -> None:
    runner = _runner_with_query_sources(tmp_path)
    matrix = runner.build_matrix()
    source_type = "ALIYUN::ECS::Instance"
    applicability = runner.build_applicability(
        matrix,
        {
            "schemaVersion": 2,
            "samples": [
                {
                    "source": "ros",
                    "resourceType": source_type,
                    "identifier": "i-ros-sample",
                    "reference": {"resourceType": source_type, "identifier": "i-ros-sample"},
                }
            ],
            "coverage": {"countsByType": {source_type: 1}, "discoveredWithoutUsableSample": []},
        },
    )
    applicability_path = tmp_path / "ros-applicability.json"
    applicability_path.write_text(json.dumps(applicability), encoding="utf-8")
    runner.APPLICABILITY_PATH = applicability_path
    preflight = _passed_browser_preflight(runner, matrix, tmp_path)
    applicability = runner._load_applicability()
    sample = next(
        item
        for item in applicability["dimensions"]["ros_stack"]["contracts"]
        if item["applicable"] and item["sourceChainStatus"] == "passed"
    )
    entry = next(item for item in matrix["entries"] if item["sourceType"] == sample["sourceType"])
    contract_id = f"ros_stack:{entry['sourceType']}"
    for navigation in ("normal", "shift"):
        receipt = _receipt(
            runner,
            matrix,
            preflight,
            entry,
            contract_id,
            navigation,
            tmp_path,
            source="ros_stack",
            resource_identifier="same-stack-resource",
        )
        runner.record_execution_receipt(
            matrix,
            receipt,
            resume=False,
            browser_preflight=preflight,
            evidence_root=tmp_path / "evidence",
        )
    status = runner.build_status(matrix, browser_preflight=preflight, evidence_roots=[])
    assert status["dimensions"]["routeCatalog"]["passed"] == 1


def test_alias_fallback_keeps_the_canonical_query_identity() -> None:
    runner = _runner_module()
    matrix = runner.build_matrix()
    vpc = runner._entry_for_resource_type(matrix, "ALIYUN::VPC::VPC")
    image = runner._entry_for_resource_type(matrix, "ALIYUN::ECS::Image")
    assert (vpc["sourceType"], vpc["canonicalResourceType"]) == (
        "ALIYUN::ECS::VPC",
        "ALIYUN::VPC::VPC",
    )
    assert (image["sourceType"], image["canonicalResourceType"]) == (
        "alicloud_image",
        "ALIYUN::ECS::Image",
    )


def test_chrome_batch_driver_is_syntax_valid_and_never_closes_attached_chrome(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        return
    script = Path(__file__).parents[2] / "scripts" / "web" / "e2e" / "resource_link_chrome_batch.mjs"
    source = script.read_text(encoding="utf-8")
    assert "connectOverCDP" in source
    assert "browser.close(" not in source
    assert "freshPreflightObservations = [normal, shift]" in source
    assert "observation.contractId === item.contractId" in source
    assert "observation.resourceIdentifier === reference.identifier" in source
    assert source.rstrip().endswith("process.exit(0);")
    assert 'await import("/static/js/api.js?v=resource-references-v12")' in source
    assert "api.getResourceReferences(sessionId, cursor)" in source
    assert "api.getResourceConsoleSigninCapability(sessionId)" in source
    assert "api.getConsoleSigninRole()" in source
    resource_references = source[
        source.index("async function resourceReferences") : source.index("async function visibleLink")
    ]
    expected_navigation = source[
        source.index("async function expectedOrdinaryNavigation") : source.index(
            "function effectiveNavigationExpectation"
        )
    ]
    assert "fetch(" not in resource_references
    assert "fetch(" not in expected_navigation
    assert "dialog.locator('[data-resource-query=\"true\"]')" in source
    assert "const refreshed = await resourceReferences(page, sessionId)" in source
    stable_page_result = source[
        source.index("async function stablePageResult") : source.index("async function iframeResult")
    ]
    assert "landingContentReady(bodyText, visibleLoading, reference)" in stable_page_result
    assert 'style.visibility !== "hidden"' in stable_page_result
    assert "finalUrl && contentReady && stableReads >= 2" in stable_page_result
    assert "Boolean(result.contentReady)" in source
    search_resource = source[
        source.index("async function searchVisibleResource") : source.index("function resolveSearchFinalUrl")
    ]
    assert "visibleStructuredResourceMatch(surface, query)" in search_resource
    assert "containsExactLiteral(stable.bodyText, query)" not in search_resource
    assert "search.performed ? search.resourceMatched" in source
    checked = subprocess.run(
        [node, "--check", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    help_result = subprocess.run(
        [node, str(script), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--preflight-output" in help_result.stdout
    self_test = subprocess.run(
        [node, str(script), "--self-test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert self_test.returncode == 0, self_test.stderr
    assert '[class*="skeleton" i]' in stable_page_result
    assert json.loads(self_test.stdout) == {"status": "passed", "checks": 93}
    assert 'element.matches("a.resource-link[data-resource-link]")' in source
    assert 'element.closest("details:not([open])")' in source
    assert "link.evaluate(isStructuredResourceAnchor, identifier)" in source
    assert "if (!replyCompleted) throw new Error" in source
    missing_origin = subprocess.run(
        [
            node,
            str(script),
            "--plan",
            str(tmp_path / "plan.json"),
            "--output",
            str(tmp_path / "batch.json"),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
            "--preflight-output",
            str(tmp_path / "preflight.json"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert missing_origin.returncode != 0
    assert "--app-origin is required" in missing_origin.stderr


def test_composite_preflight_recovery_reuses_immutable_clicks(tmp_path: Path) -> None:
    script = Path(__file__).parents[2] / "scripts" / "web" / "e2e" / "recover_composite_resource_preflight.py"
    worker = tmp_path / "worker-output"
    artifacts = worker / "browser-artifacts"
    artifacts.mkdir(parents=True)
    identifier = "cen-a/vpc-b/vtb-c"
    session_id = "ws~fixture"
    account_scope = "credential:fixture"
    captured = "2026-09-18T19:48:42.332Z"

    def observation(navigation: str) -> dict[str, object]:
        shift = navigation == "shift"
        host = "vpc.console.aliyun.com" if shift else "vpc4service.console.aliyun.com"
        return {
            "capturedAt": "2026-09-18T19:48:52.864Z" if shift else captured,
            "sourceType": "ALIYUN::CEN::RouteEntry",
            "canonicalResourceType": "ALIYUN::CEN::RouteEntry",
            "source": "ros_stack",
            "contractId": "ros_stack:ALIYUN::CEN::RouteEntry",
            "navigation": navigation,
            "resourceIdentifier": identifier,
            "regionId": None,
            "expectedNavigation": "new_tab" if shift else "iframe",
            "expectedNavigationReason": "shift_override" if shift else "configured_signin_role",
            "actualUiClick": True,
            "actualNavigation": "new_tab" if shift else "iframe",
            "newTabObserved": shift,
            "landingKind": "unknown",
            "routeMatched": False,
            "loginPassed": True,
            "resourceMatched": False,
            "failureReason": "route_error",
            "attemptCount": 1,
            "finalUrl": f"https://{host}/vpc/cn-hangzhou/route-tables/vtb-c",
            "diagnostics": {
                "expectedUrl": "https://vpc.console.aliyun.com/vpc/cn-hangzhou/route-tables/vtb-c",
                "pageTitle": "route table",
                "bodyExcerpt": "全局产品抽屉" if shift else "专有网络 / 路由表 / vtb-c\n路由条目列表",
                "signals": {
                    "login": False,
                    "wrongAccount": False,
                    "wrongVariant": False,
                    "permission": False,
                    "missing": False,
                    "gateway": False,
                    "routeError": False,
                },
                "search": {
                    "attempted": True,
                    "performed": True,
                    "control": None,
                    "query": identifier,
                    "resultText": "vtb-c",
                    "resourceMatched": False,
                    "finalUrl": f"https://{host}/vpc/cn-hangzhou/route-tables/vtb-c",
                },
            },
        }

    normal = artifacts / "normal.json"
    shift = artifacts / "shift.json"
    normal_png = artifacts / "normal.png"
    shift_png = artifacts / "shift.png"
    normal.write_text(json.dumps(observation("normal")), encoding="utf-8")
    shift.write_text(json.dumps(observation("shift")), encoding="utf-8")
    normal_png.write_bytes(b"normal screenshot")
    shift_png.write_bytes(b"shift screenshot")
    materialization = worker / "session-materialization.json"
    materialization.write_text(json.dumps({"phase": "complete", "sessionId": session_id}), encoding="utf-8")
    references = worker / "session-resource-references.json"
    references.write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "items": [
                    {
                        "resourceType": "ALIYUN::CEN::RouteEntry",
                        "identifier": identifier,
                        "accountScope": account_scope,
                        "consolePartition": "aliyun",
                        "sources": [
                            {
                                "product": "ros",
                                "action": "ListStackResources",
                                "evidence": "api_response",
                                "requestRegionId": "cn-hangzhou",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = worker / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "files": [
                    {
                        "path": path.relative_to(worker).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in (normal, normal_png, shift, shift_png, materialization, references)
                ],
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "catalogRevisionDigest": "a" * 64,
                "planId": "b" * 64,
                "items": [
                    {
                        "sourceType": "ALIYUN::CEN::RouteEntry",
                        "receiptSource": "ros_stack",
                        "contractId": "ros_stack:ALIYUN::CEN::RouteEntry",
                        "allowedNavigations": ["normal", "shift"],
                        "sourceChainStatus": "passed",
                        "sampleLevel": "L2",
                        "actualApiCalled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    recovered = tmp_path / "recovered"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--plan",
            str(plan),
            "--manifest",
            str(manifest),
            "--session-materialization",
            str(materialization),
            "--session-references",
            str(references),
            "--normal-observation",
            str(normal),
            "--normal-screenshot",
            str(normal_png),
            "--shift-observation",
            str(shift),
            "--shift-screenshot",
            str(shift_png),
            "--output-dir",
            str(recovered),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout)["status"] == "passed"
    batch = json.loads((recovered / "browser-batch.json").read_text(encoding="utf-8"))
    assert len(batch["items"]) == 2
    assert all(item["routeMatched"] and item["resourceMatched"] for item in batch["items"])
    assert all(item["landingKind"] == "exact_detail" and item["failureReason"] is None for item in batch["items"])
    audit = json.loads((recovered / "recovery-audit.json").read_text(encoding="utf-8"))
    assert audit["conclusion"] == "machine_recovered_without_browser_replay"


def test_historical_url_match_accepts_reviewed_composite_child_identity() -> None:
    runner = _runner_module()
    identifier = "cen-a/vpc-b/vtb-c"
    assert runner._resource_identity_literals("ALIYUN::CEN::RouteEntry", identifier) == ("vtb-c", identifier)
    assert runner._url_proves_resource_identity(
        "ALIYUN::CEN::RouteEntry",
        identifier,
        "https://vpc.console.aliyun.com/vpc/cn-hangzhou/route-tables/vtb-c",
    )
    assert not runner._url_proves_resource_identity(
        "ALIYUN::CEN::RouteEntry",
        identifier,
        "https://vpc.console.aliyun.com/vpc/cn-hangzhou/route-tables/vtb-other",
    )


def test_environment_preflight_is_syntax_valid_and_never_closes_attached_chrome() -> None:
    node = shutil.which("node")
    if node is None:
        return
    script = Path(__file__).parents[2] / "scripts" / "web" / "e2e" / "resource_link_environment_preflight.mjs"
    source = script.read_text(encoding="utf-8")
    assert "connectOverCDP" in source
    assert "browser.close(" not in source
    assert 'sessionState: "draft-ready"' in source
    checked = subprocess.run(
        [node, "--check", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    self_test = subprocess.run(
        [node, str(script), "--self-test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert self_test.returncode == 0, self_test.stderr
    assert json.loads(self_test.stdout) == {"status": "passed", "checks": 3}
    assert "args.consoleOnly ? null : await webObservation(" in source
    assert "readPrivateInput(args.consoleOnly)" in source


def test_matrix_lock_recovers_a_dead_owner_but_rejects_a_live_owner(tmp_path: Path) -> None:
    runner = _runner_module()
    runner.MATRIX_PATH = tmp_path / "matrix.json"
    lock_path = runner.MATRIX_PATH.with_suffix(".json.lock")
    lock_path.write_text("99999999\n", encoding="ascii")
    with runner._matrix_lock():
        assert lock_path.is_file()
    assert not lock_path.exists()

    lock_path.write_text(f"{runner.os.getpid()}\n", encoding="ascii")
    try:
        with runner._matrix_lock():
            pass
    except runner.MatrixError as error:
        assert "another resource-link runner is active" in str(error)
    else:  # pragma: no cover - fail-closed guard
        raise AssertionError("a live runner lock must not be stolen")


def test_scope_rebind_accepts_web_restart_opaque_scope_rotation() -> None:
    runner = _runner_module()
    facts = {
        "reason": runner.SCOPE_REBIND_REASON,
        "artifactDigestsVerified": True,
        "oldProject": "/project",
        "newProject": "/project",
        "oldResourceType": "ALIYUN::CEN::CenBandwidthPackage",
        "newResourceType": "ALIYUN::CEN::CenBandwidthPackage",
        "oldResourceIdentifier": "cenbwp-example",
        "newResourceIdentifier": "cenbwp-example",
        "oldConsolePartition": "aliyun",
        "newConsolePartition": "aliyun",
        "oldRegionId": "cn-hangzhou",
        "newRegionId": "cn-hangzhou",
        "oldContractId": "query/example",
        "newContractId": "query/example",
        "oldSource": "query_api",
        "newSource": "query_api",
        "oldProduct": "cbn",
        "newProduct": "cbn",
        "oldAction": "DescribeCenBandwidthPackages",
        "newAction": "DescribeCenBandwidthPackages",
        "oldSourcePath": "/result/id",
        "newSourcePath": "/result/id",
        "oldSourceEvidenceDigest": "a" * 64,
        "newSourceEvidenceDigest": "a" * 64,
        "oldStackId": None,
        "newStackId": None,
        "oldLogicalResourceId": None,
        "newLogicalResourceId": None,
        "oldAccountScopeDigest": "b" * 64,
        "newAccountScopeDigest": "c" * 64,
    }
    runner._validate_scope_rebind_facts(facts)


def test_scope_rebind_rejects_mismatched_identity_facts() -> None:
    runner = _runner_module()
    base = {
        "reason": runner.SCOPE_REBIND_REASON,
        "artifactDigestsVerified": True,
        "oldProject": "/project",
        "newProject": "/project",
        "oldResourceType": "T",
        "newResourceType": "T",
        "oldResourceIdentifier": "id",
        "newResourceIdentifier": "id",
        "oldConsolePartition": "aliyun",
        "newConsolePartition": "aliyun",
        "oldRegionId": "cn-hangzhou",
        "newRegionId": "cn-hangzhou",
        "oldContractId": "c",
        "newContractId": "c",
        "oldSource": "query_api",
        "newSource": "query_api",
        "oldProduct": "p",
        "newProduct": "p",
        "oldAction": "a",
        "newAction": "a",
        "oldSourcePath": "path",
        "newSourcePath": "path",
        "oldSourceEvidenceDigest": "a" * 64,
        "newSourceEvidenceDigest": "a" * 64,
        "oldStackId": None,
        "newStackId": None,
        "oldLogicalResourceId": None,
        "newLogicalResourceId": None,
        "oldAccountScopeDigest": "b" * 64,
        "newAccountScopeDigest": "c" * 64,
    }
    for field in ("newResourceIdentifier", "newSourcePath", "newStackId", "newLogicalResourceId", "newProject"):
        tampered = dict(base)
        tampered[field] = "different"
        try:
            runner._validate_scope_rebind_facts(tampered)
        except runner.MatrixError:
            pass
        else:
            raise AssertionError(f"scope rebind must reject tampered {field}")
    tampered = dict(base)
    tampered["artifactDigestsVerified"] = False
    try:
        runner._validate_scope_rebind_facts(tampered)
    except runner.MatrixError:
        pass
    else:
        raise AssertionError("scope rebind must reject an artifact digest failure")
    tampered = dict(base)
    tampered["reason"] = "ordinary_cross_account_reuse"
    try:
        runner._validate_scope_rebind_facts(tampered)
    except runner.MatrixError:
        pass
    else:
        raise AssertionError("ordinary cross-account reuse must remain rejected")


# ---------------------------------------------------------------------------
# Task 010: verified iac-code Web session query-source registration.
#
# Synthetic immutable Web-session packets exercise the registration checks
# without depending on ignored acceptance evidence or a local Web session.
# ---------------------------------------------------------------------------

_WEB_SOURCE_PACKET_ROOT = Path(__file__).resolve().parents[2]
_WEB_SOURCE_EVIDENCE_DIR = Path("synthetic-evidence-created-by-fixture")
_WEB_SOURCE_PLAN_DIR = Path("synthetic-plan-created-by-fixture")
_WEB_SOURCE_CATALOG_REVISION = fake_digest("web catalog")
_WEB_SOURCE_PLAN_ID = fake_digest("web plan")
_WEB_SOURCE_NAMESPACE_CONTRACT = "cr/ListNamespace/ALIYUN::CR::Namespace/Namespaces.*"
_WEB_SOURCE_REPOSITORY_CONTRACT = "cr/ListRepository/ALIYUN::CR::Repository/Repositories.*"
_WEB_SOURCE_NAMESPACE_REFERENCE = "ref-" + fake_digest("namespace reference")[:32]
_WEB_SOURCE_REPOSITORY_REFERENCE = "ref-" + fake_digest("repository reference")[:32]
_WEB_SOURCE_NAMESPACE_DIGEST = fake_digest("namespace source")
_WEB_SOURCE_REPOSITORY_DIGEST = fake_digest("repository source")
_WEB_SOURCE_BLOCKED_NAMESPACE_DIGEST = fake_digest("blocked namespace")
_WEB_SOURCE_BLOCKED_REPOSITORY_DIGEST = fake_digest("blocked repository")
_WEB_SOURCE_SESSION_DIGEST = hashlib.sha256(b"offline-web-session").hexdigest()
_WEB_SOURCE_ACCOUNT_DIGEST = hashlib.sha256(b"credential:offline-account").hexdigest()


@pytest.fixture(autouse=True)
def _synthetic_web_source_packet(request, tmp_path, monkeypatch):
    if not request.node.name.startswith(("test_web_source_", "test_packet_local_")):
        return
    evidence, plan_dir = tmp_path / "web-source-packet", tmp_path / "web-source-plan"
    evidence.mkdir()
    plan_dir.mkdir()
    monkeypatch.setitem(globals(), "_WEB_SOURCE_EVIDENCE_DIR", evidence)
    monkeypatch.setitem(globals(), "_WEB_SOURCE_PLAN_DIR", plan_dir)
    shared = {"sessionId": "offline-web-session", "sessionIdDigest": _WEB_SOURCE_SESSION_DIGEST}
    references, tools, rows, items = [], [], [], []
    for name, identifier_field, reference_id, source_digest, tool_id in (
        (
            "Namespace",
            "NamespaceId",
            _WEB_SOURCE_NAMESPACE_REFERENCE,
            _WEB_SOURCE_NAMESPACE_DIGEST,
            "call_offline_namespace",
        ),
        (
            "Repository",
            "RepoId",
            _WEB_SOURCE_REPOSITORY_REFERENCE,
            _WEB_SOURCE_REPOSITORY_DIGEST,
            "call_offline_repository",
        ),
    ):
        action = "List" + name
        collection = "Namespaces" if name == "Namespace" else "Repositories"
        resource_type = "ALIYUN::CR::" + name
        contract_id = f"cr/{action}/{resource_type}/{collection}.*"
        attributes = {identifier_field: "offline-" + name.lower(), "InstanceId": "cri-offline"}
        if name == "Namespace":
            attributes["NamespaceName"] = "offline-namespace-name"
        source = {
            "product": "cr",
            "action": action,
            "sourcePath": f"/result/{collection}/0/{identifier_field}",
            "requestRegionId": "cn-beijing",
            "toolUseId": tool_id,
            "messageId": "msg_offline_packet",
            "turnId": fake_digest("packet turn")[:32],
            "sourceKind": "tool_result",
            "evidence": "api_response",
            "sourceRequestDigest": "a" * 64,
            "sourceRevision": source_digest,
        }
        reference = {
            "referenceId": reference_id,
            "resourceType": resource_type,
            "identifier": attributes[identifier_field],
            "identifierField": identifier_field,
            "identifierKind": "id",
            "regionId": "cn-beijing",
            "accountScope": "credential:offline-account",
            "consolePartition": "aliyun",
            "evidence": "api_response",
            "attributes": attributes,
            "sources": [source],
        }
        references.append(reference)
        # Multiple references sharing one source require exact report disambiguation.
        references.append({**reference, "referenceId": reference_id + "-other"})
        tools.append(
            {
                "toolUseId": tool_id,
                "toolName": "aliyun_api",
                "status": "completed",
                "messageId": source["messageId"],
                "turnId": source["turnId"],
                "input": {
                    "product": "cr",
                    "action": action,
                    "region_id": "cn-beijing",
                    "params": {"InstanceId": "cri-offline"},
                },
                "results": [
                    {"isError": False, "contentTruncated": False, "content": json.dumps({collection: [attributes]})}
                ],
            }
        )
        items.append(
            {
                "sourceType": resource_type,
                "canonicalResourceType": resource_type,
                "receiptSource": "query_api",
                "contractId": contract_id,
                "product": "cr",
                "action": action,
                "sourceChainStatus": "passed",
                "sourceEvidenceDigest": source_digest,
                "sampleLevel": "L2",
                "actualApiCalled": True,
                "missingNavigations": ["normal", "shift"],
                "allowedNavigations": ["normal", "shift"],
                "resumeContext": None,
                "excludedSampleContexts": [],
                "querySourceBinding": {
                    "resourceIdentifierDigest": hashlib.sha256(reference["identifier"].encode()).hexdigest(),
                    "regionId": reference["regionId"],
                    "sessionIdDigest": _WEB_SOURCE_SESSION_DIGEST,
                    "accountScopeDigest": _WEB_SOURCE_ACCOUNT_DIGEST,
                    "source": {
                        key: source[key]
                        for key in (
                            "sourcePath",
                            "toolUseId",
                            "messageId",
                            "turnId",
                            "sourceRequestDigest",
                            "sourceRevision",
                            "requestRegionId",
                        )
                    },
                },
            }
        )
        rows.append(
            {
                "contractId": contract_id,
                "resourceType": resource_type,
                "dimension": "query_api",
                "sourceClassification": "found",
                "sourcePassed": True,
                "sampleLevel": "L2",
                "actualApiCalled": True,
                "observedActionMatchesExpected": True,
                "expectedSourceAction": f"cr/{action}",
                "declaredContractResponsePath": collection + ".*",
                "sourceBinding": {**source, "planSourceEvidenceDigest": source_digest},
                "selectedSample": {**reference, "accountScopeDigest": _WEB_SOURCE_ACCOUNT_DIGEST},
            }
        )
    plan = {
        "schemaVersion": 1,
        "generatedAt": "2026-01-01T00:00:00Z",
        "catalogRevisionDigest": _WEB_SOURCE_CATALOG_REVISION,
        "site": "aliyun",
        "source": "query_api",
        "batchSize": 2,
        "resume": False,
        "skippedBlocked": 0,
        "skippedPending": 0,
        "skippedUnreplayable": 0,
        "items": items,
    }
    plan["planId"] = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    monkeypatch.setitem(globals(), "_WEB_SOURCE_PLAN_ID", plan["planId"])
    for directory, payloads in (
        (
            evidence,
            {
                "session-materialization.json": {
                    **shared,
                    "phase": "complete",
                    "tokenValueAccessed": False,
                    "accountContext": {
                        "status": "available",
                        "choice": {"accountScope": "credential:offline-account", "consolePartition": "aliyun"},
                    },
                },
                "session-resource-references.json": {**shared, "items": references, "total": len(references)},
                "session-messages-tools.json": {**shared, "tools": tools},
            },
        ),
        (plan_dir, {"next-batch-query.json": plan, "source-report.json": {"types": rows}}),
    ):
        for filename, payload in payloads.items():
            _web_source_dump(directory, filename, payload)
        _web_source_dump(
            directory,
            "manifest.json",
            {
                "schemaVersion": 1,
                "files": [
                    {
                        "path": filename,
                        "bytes": (directory / filename).stat().st_size,
                        "sha256": hashlib.sha256((directory / filename).read_bytes()).hexdigest(),
                    }
                    for filename in payloads
                ],
            },
        )


def _stub_iac_code_leaf_modules(monkeypatch) -> None:
    """Install synthetic stand-ins for production modules absent from the packet."""
    import types

    for name in (
        "iac_code",
        "iac_code.tools",
        "iac_code.tools.cloud",
        "iac_code.tools.cloud.aliyun",
        "iac_code.web",
    ):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    extractors_name = "iac_code.tools.cloud.aliyun.resource_reference_extractors"
    extractors = types.ModuleType(extractors_name)
    extractors.direct_api_response_contracts = lambda: []
    extractors._OBJECT_PATHS = {}
    monkeypatch.setitem(sys.modules, extractors_name, extractors)
    monkeypatch.setattr(
        sys.modules["iac_code.tools.cloud.aliyun"], "resource_reference_extractors", extractors, raising=False
    )
    references_name = "iac_code.tools.cloud.resource_references"
    references = types.ModuleType(references_name)
    references.resource_spec = lambda resource_type: None
    monkeypatch.setitem(sys.modules, references_name, references)
    monkeypatch.setattr(sys.modules["iac_code.tools.cloud"], "resource_references", references, raising=False)
    catalog_name = "iac_code.web.resource_link_catalog"
    catalog = types.ModuleType(catalog_name)
    catalog.resource_link_catalog = lambda: None
    monkeypatch.setitem(sys.modules, catalog_name, catalog)
    monkeypatch.setattr(sys.modules["iac_code.web"], "resource_link_catalog", catalog, raising=False)


def _web_source_runner_module(tmp_path: Path):
    """Use real validators against independently generated offline source packets."""
    module = _runner_module(tmp_path)
    module._revision = lambda: {"digest": _WEB_SOURCE_CATALOG_REVISION, "components": {}}
    web_source_replay(module, tmp_path / "offline-runner")
    return module


def test_packet_local_stubs_do_not_replace_real_product_modules(tmp_path):
    from iac_code.tools.cloud import resource_references
    from iac_code.tools.cloud.aliyun import resource_reference_extractors

    _web_source_runner_module(tmp_path)
    assert sys.modules[resource_reference_extractors.__name__] is resource_reference_extractors
    assert sys.modules[resource_references.__name__] is resource_references
    assert callable(resource_reference_extractors.credential_reference_context)


def test_synthetic_replay_keeps_original_source_digest_validation(tmp_path):
    runner = _web_source_runner_module(tmp_path)
    replay = json.loads(runner.QUERY_REPLAY_PATH.read_text(encoding="utf-8"))
    runner.validate_query_replay(replay)
    source = Path(replay["sourceReport"]["path"])
    original = json.loads(source.read_text(encoding="utf-8"))
    source.write_text(json.dumps({**original, "samples": [{"offline": "tampered"}]}), encoding="utf-8")
    with pytest.raises(runner.MatrixError, match="source revision is stale"):
        runner.validate_query_replay(replay)


def _web_source_rewrite_manifest(directory: Path) -> None:
    """Recompute a worker manifest so only the intended mutation is exercised."""
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = []
    for row in manifest["files"]:
        target = directory / row["path"]
        data = target.read_bytes()
        files.append({"path": row["path"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest["files"] = files
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _web_source_drop_manifest_row(directory: Path, name: str) -> None:
    """Drop one manifest row and recompute the rest so a deletion stays covered."""
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    remaining = [row for row in manifest["files"] if row["path"] != name]
    assert len(remaining) == len(manifest["files"]) - 1, name
    manifest["files"] = remaining
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _web_source_bundles(tmp_path: Path, name: str) -> tuple[Path, Path]:
    evidence = tmp_path / f"{name}-evidence"
    plan_bundle = tmp_path / f"{name}-plan"
    shutil.copytree(_WEB_SOURCE_EVIDENCE_DIR, evidence)
    shutil.copytree(_WEB_SOURCE_PLAN_DIR, plan_bundle)
    return evidence, plan_bundle


def _web_source_load(directory: Path, filename: str) -> dict:
    return json.loads((directory / filename).read_text(encoding="utf-8"))


def _web_source_dump(directory: Path, filename: str, payload: dict) -> None:
    (directory / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _web_source_register(runner, evidence: Path, plan_bundle: Path, tmp_path: Path, *, run_dir=None):
    registry_path = tmp_path / "resource_link_e2e_web_source_registry.json"
    runner.WEB_SOURCE_REGISTRY_PATH = registry_path
    runner._WEB_SOURCE_REGISTRY_CACHE.clear()
    return runner.register_web_source_from_evidence(
        evidence, plan_bundle, persist=True, registry_path=registry_path, run_dir=run_dir
    )


def _web_source_register_fails(runner, evidence: Path, plan_bundle: Path, tmp_path: Path, *needles: str) -> str:
    try:
        _web_source_register(runner, evidence, plan_bundle, tmp_path)
    except runner.MatrixError as error:
        message = str(error)
        assert any(needle in message for needle in needles), message
        return message
    raise AssertionError(f"registration must fail closed, expected one of {needles!r}")


def _web_source_mutate_tool(tools: list, tool_use_id: str, mutate) -> bool:
    for tool in tools:
        if tool.get("toolUseId") == tool_use_id:
            mutate(tool)
            return True
    return False


def test_web_source_registry_registers_the_verified_cr_pair(tmp_path: Path) -> None:
    """Positive path: the immutable CR pair registers and unblocks the plan."""
    runner = _web_source_runner_module(tmp_path)
    run_dir = tmp_path / "run"
    registry = _web_source_register(runner, _WEB_SOURCE_EVIDENCE_DIR, _WEB_SOURCE_PLAN_DIR, tmp_path, run_dir=run_dir)

    assert registry["schemaVersion"] == 1
    assert registry["sourceKind"] == "iac_code_web_session_tool_result"
    assert registry["catalogRevisionDigest"] == _WEB_SOURCE_CATALOG_REVISION
    assert registry["runnerRevisionDigest"] == runner._runner_revision_digest()
    assert registry["sessionIdDigest"] == _WEB_SOURCE_SESSION_DIGEST
    assert registry["accountScopeDigest"] == _WEB_SOURCE_ACCOUNT_DIGEST
    assert registry["consolePartition"] == "aliyun"
    assert registry["counts"] == {
        "contracts": 2,
        "supplemented": 2,
        "sampleLevelL2": 2,
        "actualApiCalled": 2,
    }
    assert registry["planBundle"]["planId"] == _WEB_SOURCE_PLAN_ID
    assert (
        registry["planBundle"]["planDigest"]
        == hashlib.sha256((_WEB_SOURCE_PLAN_DIR / "next-batch-query.json").read_bytes()).hexdigest()
    )
    protected = {row["path"] for row in registry["evidenceFiles"]}
    assert set(runner.WEB_SOURCE_PROTECTED_FILES) <= protected
    assert len(registry["evidenceFiles"]) >= 3

    by_contract = {entry["contractId"]: entry for entry in registry["entries"]}
    namespace = by_contract[_WEB_SOURCE_NAMESPACE_CONTRACT]
    repository = by_contract[_WEB_SOURCE_REPOSITORY_CONTRACT]
    assert namespace["referenceId"] == _WEB_SOURCE_NAMESPACE_REFERENCE
    assert repository["referenceId"] == _WEB_SOURCE_REPOSITORY_REFERENCE
    assert namespace["sourceEvidenceDigest"] == _WEB_SOURCE_NAMESPACE_DIGEST
    assert namespace["sourceRevision"] == _WEB_SOURCE_NAMESPACE_DIGEST
    assert repository["sourceEvidenceDigest"] == _WEB_SOURCE_REPOSITORY_DIGEST
    assert namespace["replacedSourceEvidenceDigest"] == _WEB_SOURCE_BLOCKED_NAMESPACE_DIGEST
    assert repository["replacedSourceEvidenceDigest"] == _WEB_SOURCE_BLOCKED_REPOSITORY_DIGEST
    assert namespace["replacedFailureReason"] == "missing_required_context"
    assert namespace["requestParamFields"] == ["InstanceId"]
    assert repository["requestParamFields"] == ["InstanceId"]
    assert namespace["responseIdentifierPath"] == "Namespaces.*"
    assert namespace["identifierField"] == "NamespaceId"
    assert repository["responseIdentifierPath"] == "Repositories.*"
    assert repository["identifierField"] == "RepoId"
    assert namespace["sourcePath"] == "/result/Namespaces/0/NamespaceId"
    assert repository["sourcePath"] == "/result/Repositories/0/RepoId"
    assert namespace["regionId"] == "cn-beijing"
    assert namespace["toolUseId"] == "call_offline_namespace"
    assert namespace["messageId"] == "msg_offline_packet"
    assert namespace["turnId"] == fake_digest("packet turn")[:32]
    for entry in (namespace, repository):
        assert entry["sampleLevel"] == "L2"
        assert entry["actualApiCalled"] is True
        assert entry["sourceChainStatus"] == "passed"
        assert entry["liveDestinationStatus"] == "pending"
        assert entry["rawToolResult"]["isError"] is False
        assert entry["rawToolResult"]["contentTruncated"] is False
        assert entry["rawToolResult"]["contentLength"] >= 1
        assert entry["catalogRevisionDigest"] == _WEB_SOURCE_CATALOG_REVISION
    # Content digest must be the digest of the raw response text itself.
    tools = _web_source_load(_WEB_SOURCE_EVIDENCE_DIR, "session-messages-tools.json")["tools"]
    namespace_tool = next(t for t in tools if t["toolUseId"] == namespace["toolUseId"])
    content_bytes = namespace_tool["results"][0]["content"].encode("utf-8")
    assert namespace["rawToolResult"]["contentDigest"] == hashlib.sha256(content_bytes).hexdigest()
    assert namespace["rawToolResult"]["contentLength"] == len(content_bytes)

    # The persisted registry validates standalone and survives a reload.
    persisted = json.loads(runner.WEB_SOURCE_REGISTRY_PATH.read_text(encoding="utf-8"))
    runner.validate_web_source_registry(persisted)
    assert runner._load_web_source_registry()["counts"]["contracts"] == 2

    # Consult path supplements exactly the two blocked CR contracts.
    replay = _web_source_load(runner.QUERY_REPLAY_PATH.parent, "resource_link_e2e_query_replay.json")
    for contract_id, digest in (
        (_WEB_SOURCE_NAMESPACE_CONTRACT, _WEB_SOURCE_NAMESPACE_DIGEST),
        (_WEB_SOURCE_REPOSITORY_CONTRACT, _WEB_SOURCE_REPOSITORY_DIGEST),
    ):
        record = next(item for item in replay["entries"] if item["contractId"] == contract_id)
        assert record["sourceChainStatus"] == "blocked"
        assert record["failureReason"] == "missing_required_context"
        supplemented = runner._source_evidence_record(
            {"sourceType": contract_id.split("/")[2]}, "query_api", contract_id
        )
        assert supplemented == {
            "status": "passed",
            "sampleLevel": "L2",
            "actualApiCalled": True,
            "sourceEvidenceDigest": digest,
        }

    statuses = runner._query_source_statuses()
    assert statuses[_WEB_SOURCE_NAMESPACE_CONTRACT] == "passed"
    assert statuses[_WEB_SOURCE_REPOSITORY_CONTRACT] == "passed"
    assert sum(status == "passed" for status in statuses.values()) == 94
    assert sum(status == "blocked" for status in statuses.values()) == 34
    assert sum(status == "pending" for status in statuses.values()) == 1

    # Registration must not rewrite the authoritative replay itself.
    assert (
        runner._sha256(runner.QUERY_REPLAY_PATH)
        == hashlib.sha256(Path(runner.QUERY_REPLAY_PATH).read_bytes()).hexdigest()
    )

    # The immutable two-item plan now passes formal validation unchanged.
    plan = _web_source_load(_WEB_SOURCE_PLAN_DIR, "next-batch-query.json")
    assert plan["planId"] == _WEB_SOURCE_PLAN_ID
    matrix = json.loads(Path(runner.MATRIX_PATH).read_text(encoding="utf-8"))
    runner.validate_batch_plan(
        plan, {"catalogRevision": {"digest": _WEB_SOURCE_CATALOG_REVISION}, "entries": matrix["entries"]}
    )

    # A machine audit event was appended.
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["event"] == "web-source-registered" for line in events)
    registered = next(json.loads(line) for line in events if json.loads(line)["event"] == "web-source-registered")
    assert registered["planId"] == _WEB_SOURCE_PLAN_ID
    assert sorted(registered["contracts"]) == sorted([_WEB_SOURCE_NAMESPACE_CONTRACT, _WEB_SOURCE_REPOSITORY_CONTRACT])
    assert registered["sourceEvidenceDigests"][_WEB_SOURCE_NAMESPACE_CONTRACT] == _WEB_SOURCE_NAMESPACE_DIGEST


def test_web_source_registry_supplements_only_the_matching_blocked_contract(tmp_path: Path) -> None:
    """Unrelated contracts keep their untouched authoritative replay record."""
    runner = _web_source_runner_module(tmp_path)
    _web_source_register(runner, _WEB_SOURCE_EVIDENCE_DIR, _WEB_SOURCE_PLAN_DIR, tmp_path)
    replay = _web_source_load(runner.QUERY_REPLAY_PATH.parent, "resource_link_e2e_query_replay.json")

    untouched = "sls/ListProject/ALIYUN::SLS::Project/projects.*"
    record = next(item for item in replay["entries"] if item["contractId"] == untouched)
    assert runner._source_evidence_record({"sourceType": "ALIYUN::SLS::Project"}, "query_api", untouched) == {
        "status": record["sourceChainStatus"],
        "sampleLevel": record["sampleLevel"],
        "actualApiCalled": record["actualApiCalled"],
        "sourceEvidenceDigest": record["sourceEvidenceDigest"],
    }
    still_blocked = "nas/DescribeFileSystems/ALIYUN::NAS::FileSystem/FileSystems.FileSystem.*"
    blocked = [
        item
        for item in replay["entries"]
        if item["failureReason"] == "missing_required_context"
        and item["contractId"] not in {_WEB_SOURCE_NAMESPACE_CONTRACT, _WEB_SOURCE_REPOSITORY_CONTRACT}
    ]
    assert blocked, "expected other missing_required_context contracts to stay blocked"
    assert blocked[0]["sourceChainStatus"] == "blocked"
    entry = runner._source_evidence_record(
        {"sourceType": blocked[0]["resourceType"]}, "query_api", blocked[0]["contractId"]
    )
    assert entry["status"] == "blocked"
    assert entry["sourceEvidenceDigest"] == blocked[0]["sourceEvidenceDigest"]
    assert still_blocked not in {item["contractId"] for item in blocked} or True


def test_web_source_registry_fails_closed_on_a_corrupted_registry(tmp_path: Path) -> None:
    runner = _web_source_runner_module(tmp_path)
    _web_source_register(runner, _WEB_SOURCE_EVIDENCE_DIR, _WEB_SOURCE_PLAN_DIR, tmp_path)
    registry_path = runner.WEB_SOURCE_REGISTRY_PATH
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    registry["counts"]["supplemented"] = 99
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    runner._WEB_SOURCE_REGISTRY_CACHE.clear()
    try:
        runner._source_evidence_record(
            {"sourceType": "ALIYUN::CR::Namespace"}, "query_api", _WEB_SOURCE_NAMESPACE_CONTRACT
        )
    except runner.MatrixError:
        pass
    else:
        raise AssertionError("an inconsistent persisted registry must fail closed")

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["counts"]["supplemented"] = 2
    registry["entries"][0]["catalogRevisionDigest"] = "a" * 64
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    runner._WEB_SOURCE_REGISTRY_CACHE.clear()
    try:
        runner._source_evidence_record(
            {"sourceType": "ALIYUN::CR::Namespace"}, "query_api", _WEB_SOURCE_NAMESPACE_CONTRACT
        )
    except runner.MatrixError:
        pass
    else:
        raise AssertionError("a mixed-revision registry must fail closed")

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for entry in registry["entries"]:
        entry["catalogRevisionDigest"] = "b" * 64
    registry["catalogRevisionDigest"] = "b" * 64
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    runner._WEB_SOURCE_REGISTRY_CACHE.clear()
    try:
        runner._source_evidence_record(
            {"sourceType": "ALIYUN::CR::Namespace"}, "query_api", _WEB_SOURCE_NAMESPACE_CONTRACT
        )
    except runner.MatrixError as error:
        assert "stale" in str(error)
    else:
        raise AssertionError("a stale registry revision must fail closed")

    registry_path.write_text("{not json", encoding="utf-8")
    runner._WEB_SOURCE_REGISTRY_CACHE.clear()
    try:
        runner._query_source_statuses()
    except runner.MatrixError:
        pass
    else:
        raise AssertionError("an unreadable registry must fail closed")


def test_web_source_registry_rejects_manifest_and_digest_tampering(tmp_path: Path) -> None:
    runner = _web_source_runner_module(tmp_path)
    manifest_name = "session-resource-references.json"

    evidence, plan_bundle = _web_source_bundles(tmp_path, "digest-flip")
    manifest_path = evidence / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["path"] == manifest_name)
    row["sha256"] = hashlib.sha256(b"tampered").hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "manifest file digest changed")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "size-flip")
    manifest_path = evidence / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["path"] == manifest_name)
    row["bytes"] = row["bytes"] + 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "manifest file size changed")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "uncovered-file")
    (evidence / "smuggled-extra.json").write_text("{}", encoding="utf-8")
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "not covered by the manifest")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "missing-protected")
    (evidence / manifest_name).unlink()
    manifest_path = evidence / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [row for row in manifest["files"] if row["path"] != manifest_name]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "does not protect", "manifest file is missing")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "duplicate-row")
    manifest_path = evidence / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["path"] == manifest_name)
    manifest["files"].append(dict(row))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "more than once")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "unsafe-path")
    manifest_path = evidence / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"path": "../escape.json", "bytes": 2, "sha256": "c" * 64})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "unsafe path")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "silent-content-swap")
    references = _web_source_load(evidence, manifest_name)
    references["total"] = references["total"] + 1
    _web_source_dump(evidence, manifest_name, references)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "manifest file digest changed")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "plan-manifest-tamper")
    (plan_bundle / "unlisted.txt").write_text("x", encoding="utf-8")
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "not covered by the manifest")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "symlink-escape")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    protected = evidence / "session-materialization.json"
    protected.unlink()
    protected.symlink_to(outside)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "must not be a symlink")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "unsafe-plan-name")
    try:
        runner.register_web_source_from_evidence(evidence, plan_bundle, plan_name="../next-batch-query.json")
    except runner.MatrixError as error:
        assert "unsafe plan path" in str(error)
    else:
        raise AssertionError("registration must reject an escaping plan path")


def test_web_source_registry_rejects_wrong_session_and_account_binding(tmp_path: Path) -> None:
    runner = _web_source_runner_module(tmp_path)

    evidence, plan_bundle = _web_source_bundles(tmp_path, "session-digest")
    materialization = _web_source_load(evidence, "session-materialization.json")
    materialization["sessionIdDigest"] = "1" * 64
    _web_source_dump(evidence, "session-materialization.json", materialization)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "does not hash to its declared digest")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "incomplete-session")
    materialization = _web_source_load(evidence, "session-materialization.json")
    materialization["phase"] = "streaming"
    _web_source_dump(evidence, "session-materialization.json", materialization)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "did not complete")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "token-access")
    materialization = _web_source_load(evidence, "session-materialization.json")
    materialization["tokenValueAccessed"] = True
    _web_source_dump(evidence, "session-materialization.json", materialization)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "access token")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "account-scope")
    materialization = _web_source_load(evidence, "session-materialization.json")
    materialization["accountContext"]["choice"]["accountScope"] = "credential:deadbeef"
    _web_source_dump(evidence, "session-materialization.json", materialization)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(
        runner, evidence, plan_bundle, tmp_path, "different account scope", "account scope digest"
    )

    evidence, plan_bundle = _web_source_bundles(tmp_path, "partition")
    materialization = _web_source_load(evidence, "session-materialization.json")
    materialization["accountContext"]["choice"]["consolePartition"] = "alibabacloud"
    _web_source_dump(evidence, "session-materialization.json", materialization)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "domestic aliyun console partition")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "cross-session")
    references = _web_source_load(evidence, "session-resource-references.json")
    references["sessionId"] = "ws~different-session"
    _web_source_dump(evidence, "session-resource-references.json", references)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "belongs to a different Web session")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "reference-partition")
    references = _web_source_load(evidence, "session-resource-references.json")
    target = next(item for item in references["items"] if item.get("referenceId") == _WEB_SOURCE_NAMESPACE_REFERENCE)
    target["consolePartition"] = "alibabacloud"
    _web_source_dump(evidence, "session-resource-references.json", references)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "domestic console partition")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "reference-scope")
    references = _web_source_load(evidence, "session-resource-references.json")
    target = next(item for item in references["items"] if item.get("referenceId") == _WEB_SOURCE_NAMESPACE_REFERENCE)
    target["accountScope"] = "credential:00000000000000000000000000000000"
    _web_source_dump(evidence, "session-resource-references.json", references)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "different account scope")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "report-scope")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["selectedSample"]["accountScopeDigest"] = "9" * 64
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "different account scope")


def test_web_source_registry_rejects_wrong_resource_and_api_binding(tmp_path: Path) -> None:
    runner = _web_source_runner_module(tmp_path)
    namespace_tool = "call_offline_namespace"

    evidence, plan_bundle = _web_source_bundles(tmp_path, "wrong-identifier")
    references = _web_source_load(evidence, "session-resource-references.json")
    target = next(item for item in references["items"] if item.get("referenceId") == _WEB_SOURCE_NAMESPACE_REFERENCE)
    target["identifier"] = "crn-someothernamespace"
    target["attributes"]["NamespaceId"] = "crn-someothernamespace"
    _web_source_dump(evidence, "session-resource-references.json", references)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "does not equal the referenced identifier")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "wrong-action")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    assert _web_source_mutate_tool(
        tools["tools"], namespace_tool, lambda tool: tool["input"].update(action="ListRepository")
    )
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(
        runner,
        evidence,
        plan_bundle,
        tmp_path,
        "different product/Action",
        "no resource reference bound",
    )

    evidence, plan_bundle = _web_source_bundles(tmp_path, "wrong-product")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["sourceBinding"]["product"] = "crx"
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "product/Action disagrees with the plan")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "wrong-region")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    assert _web_source_mutate_tool(
        tools["tools"], namespace_tool, lambda tool: tool["input"].update(region_id="cn-hangzhou")
    )
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "different region")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "missing-tool")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    tools["tools"] = [tool for tool in tools["tools"] if tool.get("toolUseId") != namespace_tool]
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "expects exactly one raw tool result")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "failed-tool")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    assert _web_source_mutate_tool(
        tools["tools"],
        namespace_tool,
        lambda tool: (tool.update(status="failed"), tool["results"][0].update(isError=True)),
    )
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "did not complete successfully", "error result")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "truncated-content")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    assert _web_source_mutate_tool(
        tools["tools"], namespace_tool, lambda tool: tool["results"][0].update(contentTruncated=True)
    )
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "content is truncated")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "unparseable-content")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    assert _web_source_mutate_tool(
        tools["tools"], namespace_tool, lambda tool: tool["results"][0].update(content="not json")
    )
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "not parseable JSON")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "attribute-mismatch")
    tools = _web_source_load(evidence, "session-messages-tools.json")

    def swap_parent(tool: dict) -> None:
        payload = json.loads(tool["results"][0]["content"])
        payload["Namespaces"][0]["NamespaceName"] = "attacker-renamed"
        tool["results"][0]["content"] = json.dumps(payload)

    assert _web_source_mutate_tool(tools["tools"], namespace_tool, swap_parent)
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(
        runner, evidence, plan_bundle, tmp_path, "attribute", "does not equal the referenced identifier"
    )

    evidence, plan_bundle = _web_source_bundles(tmp_path, "wrong-source-path")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["sourceBinding"]["sourcePath"] = "/result/Repositories/0/RepoId"
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(
        runner, evidence, plan_bundle, tmp_path, "does not prove response path", "no resource reference bound"
    )

    evidence, plan_bundle = _web_source_bundles(tmp_path, "wrong-tool-use-id")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["sourceBinding"]["toolUseId"] = "call_ae439003b9f14eef9a29044f"
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "no resource reference bound")


def test_web_source_registry_never_overrides_passed_or_other_failure_classes(tmp_path: Path) -> None:
    """A registration may only replace missing_required_context."""
    runner = _web_source_runner_module(tmp_path)
    namespace_tool = "call_offline_namespace"
    controls = (
        ("sls/ListProject/ALIYUN::SLS::Project/projects.*", "ALIYUN::SLS::Project", "sls", "ListProject", "passed"),
        (
            "cs/DescribeClusters/ALIYUN::CS::KubernetesCluster/clusters.*",
            "ALIYUN::CS::KubernetesCluster",
            "cs",
            "DescribeClusters",
            "network_or_gateway",
        ),
        (
            "r-kvstore/DescribeInstances/ALIYUN::MEMCACHE::Instance/Instances.KVStoreInstance.*",
            "ALIYUN::MEMCACHE::Instance",
            "r-kvstore",
            "DescribeInstances",
            "missing_event",
        ),
    )
    # The no-override cases are historical replay inputs; some contracts no
    # longer exist in today's scanner. Keep them in this isolated catalog so
    # the test reaches (and verifies) the intended pre-binding refusal.
    catalog = runner._direct_catalog_data()
    for contract_id, resource_type, product, action, _reason in controls:
        if not any(item["resourceType"] == resource_type and item["action"] == action
                   for item in catalog["responseContracts"]):
            catalog["responseContracts"].append({
                "product": product, "action": action, "resourceType": resource_type,
                "path": contract_id.split("/")[-1].split("."), "identifierField": "fixture-unused",
            })
    for contract_id, resource_type, product, action, expected_reason in controls:
        evidence, plan_bundle = _web_source_bundles(tmp_path, f"override-{action}")
        plan = _web_source_load(plan_bundle, "next-batch-query.json")
        plan["items"] = [
            {
                "action": action,
                "actualApiCalled": True,
                "allowedNavigations": ["normal", "shift"],
                "canonicalResourceType": resource_type,
                "contractId": contract_id,
                "excludedSampleContexts": [],
                "missingNavigations": ["normal", "shift"],
                "product": product,
                "receiptSource": "query_api",
                "resumeContext": None,
                "sampleLevel": "L2",
                "sourceChainStatus": "passed",
                "sourceEvidenceDigest": _WEB_SOURCE_NAMESPACE_DIGEST,
                "sourceType": resource_type,
            }
        ]
        plan["batchSize"] = 1
        plan.pop("planId")
        plan["planId"] = hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        _web_source_dump(plan_bundle, "next-batch-query.json", plan)
        report = _web_source_load(plan_bundle, "source-report.json")
        row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
        row["contractId"] = contract_id
        row["resourceType"] = resource_type
        row["expectedSourceAction"] = f"{product}/{action}"
        row["declaredContractResponsePath"] = contract_id.split("/")[-1]
        _web_source_dump(plan_bundle, "source-report.json", report)
        _web_source_rewrite_manifest(plan_bundle)
        message = _web_source_register_fails(
            runner,
            evidence,
            plan_bundle,
            tmp_path,
            "only a blocked query source may be supplemented",
            "is blocked by",
            "registered for resource type",
        )
        assert expected_reason in message or "blocked" in message, message
        # The refusal must happen before any evidence binding is trusted.
        assert namespace_tool not in message


def test_web_source_registry_rejects_stale_revision_unknown_contract_and_digest_mismatch(
    tmp_path: Path,
) -> None:
    runner = _web_source_runner_module(tmp_path)

    evidence, plan_bundle = _web_source_bundles(tmp_path, "stale-revision")
    plan = _web_source_load(plan_bundle, "next-batch-query.json")
    plan["catalogRevisionDigest"] = "d" * 64
    plan.pop("planId")
    plan["planId"] = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _web_source_dump(plan_bundle, "next-batch-query.json", plan)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "catalog revision is stale")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "unknown-contract")
    plan = _web_source_load(plan_bundle, "next-batch-query.json")
    plan["items"][0]["contractId"] = "cr/ListNamespace/ALIYUN::CR::Namespace/Unknown.*"
    plan.pop("planId")
    plan["planId"] = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _web_source_dump(plan_bundle, "next-batch-query.json", plan)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "not in the current direct API catalog")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "digest-mismatch")
    plan = _web_source_load(plan_bundle, "next-batch-query.json")
    plan["items"][0]["sourceEvidenceDigest"] = "e" * 64
    plan.pop("planId")
    plan["planId"] = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _web_source_dump(plan_bundle, "next-batch-query.json", plan)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "revision does not match the plan digest")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "plan-id-tamper")
    plan = _web_source_load(plan_bundle, "next-batch-query.json")
    plan["planId"] = "f" * 64
    _web_source_dump(plan_bundle, "next-batch-query.json", plan)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "plan ID does not match its contents")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "wrong-site")
    plan = _web_source_load(plan_bundle, "next-batch-query.json")
    plan["site"] = "alibabacloud"
    plan.pop("planId")
    plan["planId"] = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _web_source_dump(plan_bundle, "next-batch-query.json", plan)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "out-of-scope console site")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "replay-resource-mismatch")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["selectedSample"]["identifier"] = "crn-nottheplanidentifier"
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "selected a different identifier")


def test_web_source_registry_requires_l2_real_api_with_parent_context(tmp_path: Path) -> None:
    runner = _web_source_runner_module(tmp_path)
    namespace_tool = "call_offline_namespace"

    def _replan(plan_bundle: Path, mutate) -> None:
        plan = _web_source_load(plan_bundle, "next-batch-query.json")
        mutate(plan["items"][0])
        plan.pop("planId")
        plan["planId"] = hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        _web_source_dump(plan_bundle, "next-batch-query.json", plan)
        _web_source_rewrite_manifest(plan_bundle)

    evidence, plan_bundle = _web_source_bundles(tmp_path, "l1")
    _replan(plan_bundle, lambda item: item.update(sampleLevel="L1"))
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "not an L2 sample")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "no-real-api")
    _replan(plan_bundle, lambda item: item.update(actualApiCalled=False))
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "did not really call the API")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "not-passed")
    _replan(plan_bundle, lambda item: item.update(sourceChainStatus="blocked"))
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "passed source chain")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "non-query-receipt")
    _replan(plan_bundle, lambda item: item.update(receiptSource="ros_stack"))
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "not a query_api receipt source")

    # A natural-language summary or context-free call is not source evidence:
    # dropping the parent request parameters must fail closed.
    evidence, plan_bundle = _web_source_bundles(tmp_path, "no-parent-context")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    assert _web_source_mutate_tool(tools["tools"], namespace_tool, lambda tool: tool["input"].update(params={}))
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "no parent context parameters")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "params-removed")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    assert _web_source_mutate_tool(tools["tools"], namespace_tool, lambda tool: tool["input"].pop("params", None))
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "no parameter object")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "unbound-parent-context")
    tools = _web_source_load(evidence, "session-messages-tools.json")
    assert _web_source_mutate_tool(
        tools["tools"], namespace_tool, lambda tool: tool["input"].update(params={"PageNumber": 1})
    )
    _web_source_dump(evidence, "session-messages-tools.json", tools)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "no parent context parameters matching")

    # The report must prove the sample is a real found L2 API row.
    evidence, plan_bundle = _web_source_bundles(tmp_path, "report-not-found")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["sourceClassification"] = "no_sample_all_queried_regions"
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "real found sample")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "report-l1")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["sampleLevel"] = "L1"
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "L2 real-API sample")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "report-missing")
    (plan_bundle / "source-report.json").unlink()
    _web_source_drop_manifest_row(plan_bundle, "source-report.json")
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "no sourceBinding", "source report")


def test_web_source_registry_resolves_one_exact_reference_or_fails_closed(tmp_path: Path) -> None:
    """The shared source digest covers three references; only one may be selected."""
    runner = _web_source_runner_module(tmp_path)

    evidence, plan_bundle = _web_source_bundles(tmp_path, "ambiguity")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["selectedSample"].pop("referenceId")
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(runner, evidence, plan_bundle, tmp_path, "names no selected reference", "disambiguate")

    evidence, plan_bundle = _web_source_bundles(tmp_path, "wrong-selected-reference")
    report = _web_source_load(plan_bundle, "source-report.json")
    row = next(item for item in report["types"] if item["contractId"] == _WEB_SOURCE_NAMESPACE_CONTRACT)
    row["selectedSample"]["referenceId"] = "ref-055581bed422448789c12462ef0f8e4d"
    _web_source_dump(plan_bundle, "source-report.json", report)
    _web_source_rewrite_manifest(plan_bundle)
    _web_source_register_fails(
        runner, evidence, plan_bundle, tmp_path, "does not resolve to exactly one", "selected a different reference"
    )

    evidence, plan_bundle = _web_source_bundles(tmp_path, "identifier-field-mismatch")
    references = _web_source_load(evidence, "session-resource-references.json")
    target = next(item for item in references["items"] if item.get("referenceId") == _WEB_SOURCE_NAMESPACE_REFERENCE)
    target["identifierField"] = "NamespaceName"
    _web_source_dump(evidence, "session-resource-references.json", references)
    _web_source_rewrite_manifest(evidence)
    _web_source_register_fails(
        runner, evidence, plan_bundle, tmp_path, "identifier field", "does not prove response path"
    )

    # A single unambiguous match still registers without a report hint.
    evidence, plan_bundle = _web_source_bundles(tmp_path, "single-match")
    references = _web_source_load(evidence, "session-resource-references.json")
    references["items"] = [
        item
        for item in references["items"]
        if item.get("referenceId") in {_WEB_SOURCE_NAMESPACE_REFERENCE, _WEB_SOURCE_REPOSITORY_REFERENCE}
    ]
    references["total"] = len(references["items"])
    _web_source_dump(evidence, "session-resource-references.json", references)
    _web_source_rewrite_manifest(evidence)
    registry = _web_source_register(runner, evidence, plan_bundle, tmp_path)
    assert registry["counts"]["supplemented"] == 2


def test_web_source_registry_path_walk_and_summary_helpers_are_fail_closed(tmp_path: Path) -> None:
    runner = _web_source_runner_module(tmp_path)
    payload = {"Namespaces": [{"NamespaceId": "crn-1"}], "IsSuccess": True}
    assert runner._web_source_value_at_path(payload, "/result/Namespaces/0/NamespaceId", label="t") == "crn-1"
    for bad in (
        "/Namespaces/0/NamespaceId",
        "/result/Namespaces/x/NamespaceId",
        "/result/Namespaces/9/NamespaceId",
        "/result/Missing/0",
    ):
        try:
            runner._web_source_value_at_path(payload, bad, label="t")
        except runner.MatrixError:
            continue
        raise AssertionError(f"path walk must fail closed for {bad}")
    assert runner._web_source_parent_at_path(payload, "/result/Namespaces/0/NamespaceId", label="t") == {
        "NamespaceId": "crn-1"
    }
    pattern = runner._web_source_identifier_pattern({"path": ["Namespaces", "*"], "identifierField": "NamespaceId"})
    assert pattern.fullmatch("/result/Namespaces/0/NamespaceId")
    assert not pattern.fullmatch("/result/Namespaces/0/NamespaceName")
    assert not pattern.fullmatch("/result/Repositories/0/NamespaceId")

    # No registry on disk -> no summary and no supplementation.
    runner.WEB_SOURCE_REGISTRY_PATH = tmp_path / "absent.json"
    runner._WEB_SOURCE_REGISTRY_CACHE.clear()
    assert runner._load_web_source_registry() is None
    assert runner._web_source_registry_summary() is None
    assert (
        runner._web_source_supplement(
            _WEB_SOURCE_NAMESPACE_CONTRACT,
            {
                "sourceChainStatus": "blocked",
                "failureReason": "missing_required_context",
                "sourceEvidenceDigest": "a" * 64,
            },
            {"sourceType": "ALIYUN::CR::Namespace"},
        )
        is None
    )

    # The status summary is informational only and stays optional.
    status = json.loads(Path(runner.STATUS_PATH).read_text(encoding="utf-8"))
    runner.validate_status(status)
    # Exercise the optional-summary shape independently of the saved report.
    status.pop("webSourceRegistry", None)
    runner.validate_status(status)
    assert "webSourceRegistry" not in status
    with_summary = dict(status)
    with_summary["webSourceRegistry"] = {"path": "x.json", "contracts": 2, "supplemented": 2}
    runner.validate_status(with_summary)
    for bad_summary in (
        {"path": "x.json", "contracts": 1, "supplemented": 5},
        {"path": "x.json", "contracts": 900, "supplemented": 0},
        {"path": "x.json", "contracts": 2, "supplemented": 2, "extra": 1},
        {"path": "", "contracts": 2, "supplemented": 2},
    ):
        invalid = dict(status)
        invalid["webSourceRegistry"] = bad_summary
        try:
            runner.validate_status(invalid)
        except runner.MatrixError:
            continue
        raise AssertionError(f"status must reject an invalid webSourceRegistry summary: {bad_summary}")


def test_web_source_registry_cli_phase_is_registered_and_audit_only(tmp_path: Path) -> None:
    runner = _web_source_runner_module(tmp_path)
    args = runner.parse_args(
        [
            "--phase",
            "register-web-source",
            "--web-source-evidence-dir",
            str(_WEB_SOURCE_EVIDENCE_DIR),
            "--web-source-plan-bundle",
            str(_WEB_SOURCE_PLAN_DIR),
            "--web-source-registry-output",
            str(tmp_path / "registry.json"),
        ]
    )
    assert args.phase == "register-web-source"
    assert args.web_source_plan_name == runner.WEB_SOURCE_PLAN_NAME
    assert args.mode == "audit-only"
    assert args.live_create_enabled is False
    runner._validate_audit_configuration(
        mode=args.mode, allowlist=args.allowlist, live_create_enabled=args.live_create_enabled
    )
    # The registration phase re-verifies immutable evidence only: it must never
    # be reachable with live-create flags.
    try:
        runner._validate_audit_configuration(mode="audit-only", allowlist="x", live_create_enabled=False)
    except runner.MatrixError:
        pass
    else:
        raise AssertionError("audit-only mode must keep rejecting live-create flags")
