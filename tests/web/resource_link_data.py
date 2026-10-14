"""Synthetic, temporary resource-link inputs; never import a user's run or PASS ledger."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "scripts/web/e2e/resource_link_workflow/catalog.example.json"


def fake_digest(label: str) -> str:
    return hashlib.sha256(("offline-fixture:" + label).encode()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def workflow_data(directory: Path) -> Path:
    """Keep public resource identities, with explicitly authored offline test outcomes."""
    catalog = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    catalog["plan_id"] = "offline-synthetic-catalog"
    resources = {item["id"]: item for item in catalog["resources"]}
    outcomes = {
        ("R0007", "QUERY"): {"iframe": "fail", "open": "fail"},
        ("R0007", "TF"): {"iframe": "untested", "open": "pass"},
        ("R0019", "QUERY"): {"iframe": "fail", "open": "untested"},
        ("R0078", "QUERY"): {"iframe": "untested", "open": "fail"},
        ("R0085", "QUERY"): {"iframe": "untested", "open": "fail"},
        ("R0024", "QUERY"): {"iframe": "untested", "open": "fail"},
        ("R0020", "ROS"): {"iframe": "pass", "open": "fail"},
        ("R0042", "ROS"): {"iframe": "fail", "open": "pass"},
        ("R0042", "QUERY"): {"iframe": "fail", "open": "pass"},
        ("R0043", "TF"): {"iframe": "fail", "open": "pass"},
        ("R0033", "TF"): {"iframe": "pass", "open": "pass"},
        ("R0029", "QUERY"): {"iframe": "untested", "open": "review"},
        ("R0041", "QUERY"): {"iframe": "untested", "open": "review"},
        ("R0067", "TF"): {"iframe": "untested", "open": "review"},
        ("R0083", "ROS"): {"iframe": "untested", "open": "review"},
        ("R0083", "QUERY"): {"iframe": "untested", "open": "review"},
    }
    for (resource_id, dimension), status in outcomes.items():
        resources[resource_id]["dimensions"][dimension]["contracts"][0]["historical"] = status
    groups = []
    for item in catalog["resources"]:
        if item["id"] == "R0076":
            continue  # Exercise a frozen contract absent from the report baseline.
        rows = []
        for dimension, source in item["dimensions"].items():
            if item["id"] == "R0021" and dimension != "TF":
                continue  # The historical key-pair row is intentionally TF-only.
            for contract in source["contracts"]:
                row = {
                    "contract_id": contract["id"],
                    "dimension": dimension,
                    "source_type": contract["source_type"],
                    "status": copy.deepcopy(contract["historical"]),
                    "reason": "Synthetic offline fixture; not real cloud acceptance.",
                }
                if item["id"] == "R0058" and dimension == "QUERY":
                    row.update(
                        contract_id="H000468", dimension="API 查询 · 近期", status={"iframe": "fail", "open": "pass"}
                    )
                rows.append(row)
        groups.append({**{k: item[k] for k in ("key", "service", "name")}, "rows": rows})
    write_json(directory / "catalog.json", catalog)
    write_json(
        directory / "historical-results.json",
        {
            "schema_version": 1,
            "as_of": "2000-01-01",
            "groups": groups,
        },
    )
    (directory / "resource-link-results.html").write_text(
        "<!doctype html><title>Synthetic offline fixture, not acceptance evidence</title>\n", encoding="utf-8"
    )
    return directory


def isolate_runner(runner, directory: Path) -> None:
    """Every mutable legacy input/output belongs to this test's temp directory."""
    # Keep the real code/schema bytes and relative-path revision hashes while
    # directing the runner's evidence discovery into a wholly temporary tree.
    source_root = runner.REPO_ROOT

    def source_copy(path: Path) -> Path:
        target = directory / path.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path != target:
            shutil.copyfile(path, target)
        return target

    for name in (
        "SCHEMA_PATH",
        "APPLICABILITY_SCHEMA_PATH",
        "HISTORY_SCHEMA_PATH",
        "QUERY_REPLAY_SCHEMA_PATH",
        "WEB_SOURCE_REGISTRY_SCHEMA_PATH",
        "CREATE_FIXTURE_SCHEMA_PATH",
        "STATUS_SCHEMA_PATH",
        "BROWSER_PREFLIGHT_SCHEMA_PATH",
        "EXECUTION_RECEIPT_SCHEMA_PATH",
        "BATCH_PLAN_SCHEMA_PATH",
        "BROWSER_BATCH_SCHEMA_PATH",
        "ROS_CATALOG_PATH",
        "DIRECT_CATALOG_PATH",
    ):
        setattr(runner, name, source_copy(getattr(runner, name)))
    for name in ("PRODUCTION_LINK_PATHS", "SOURCE_EXTRACTOR_PATHS", "FIXTURE_PATHS"):
        setattr(runner, name, tuple(source_copy(path) for path in getattr(runner, name)))
    for filename in (
        "resource_group_landing.mjs",
        "router_interface_landing.mjs",
        "slb_listener_landing.mjs",
        "sae_sag_child_landing.mjs",
    ):
        source_copy(runner.BROWSER_BATCH_DRIVER_PATH.with_name(filename))
    runner.BROWSER_BATCH_DRIVER_PATH = source_copy(runner.BROWSER_BATCH_DRIVER_PATH)
    runner.__file__ = str(source_copy(Path(runner.__file__)))
    runner.LEGACY_EVIDENCE_PATH = directory / "legacy-evidence-not-provided.md"
    runner.REPO_ROOT = directory
    runner.COMPATIBILITY_OUTPUT_DIR = directory / "compatibility"
    for name, filename in {
        "MATRIX_PATH": "resource_link_e2e_matrix.json",
        "APPLICABILITY_PATH": "resource_link_e2e_applicability.json",
        "HISTORY_PATH": "resource_link_e2e_history.json",
        "QUERY_REPLAY_PATH": "resource_link_e2e_query_replay.json",
        "TARGETED_QUERY_SOURCE_PATH": "resource_link_e2e_targeted_query_sources.json",
        "ROUTE_CLOUD_SOURCE_PATH": "resource_link_e2e_route_cloud_sources.json",
        "WEB_SOURCE_REGISTRY_PATH": "resource_link_e2e_web_source_registry.json",
        "CREATE_FIXTURE_PATH": "resource_link_e2e_create_fixtures.json",
        "STATUS_PATH": "resource_link_e2e_status.json",
    }.items():
        setattr(runner, name, directory / filename)


def web_source_replay(runner, directory: Path) -> None:
    """Author all replay branches from public contracts, never from a live export."""
    isolate_runner(runner, directory)
    # These cases exercise historical replay compatibility. Include the old SLS
    # identity explicitly instead of borrowing a machine's exported catalog.
    catalog = json.loads(runner.DIRECT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog["responseContracts"].append(
        {
            "product": "sls",
            "action": "ListProject",
            "resourceType": "ALIYUN::SLS::Project",
            "path": ["projects", "*"],
            "identifierField": "projectName",
        }
    )
    write_json(runner.DIRECT_CATALOG_PATH, catalog)
    contracts = runner._direct_catalog_data()["responseContracts"]
    required = [
        ("cr", "ListNamespace"),
        ("cr", "ListRepository"),
        ("sls", "ListProject"),
        ("nas", "DescribeFileSystems"),
        ("cs", "DescribeClusters"),
        ("r-kvstore", "DescribeInstances"),
    ]
    selected = [
        next(
            c
            for c in contracts
            if (c["product"], c["action"]) == pair
            and (pair != ("sls", "ListProject") or c["resourceType"] == "ALIYUN::SLS::Project")
        )
        for pair in required
    ]
    selected.extend(c for c in contracts if c not in selected)
    selected = selected[:129]
    assert len(selected) == 129
    blocked_ids = {0, 1, 3, 4, *range(6, 38)}
    scan = directory / "evidence/formal/source-scan-0004"
    source, events = scan / "catalog.json", scan / "catalog.events.jsonl"
    write_json(source, {"schemaVersion": 2, "runId": "a" * 32, "samples": []})
    events.write_text(json.dumps({"phase": "scan_start", "runId": "a" * 32}) + "\n", encoding="utf-8")
    matrix = runner.build_matrix()
    write_json(runner.MATRIX_PATH, matrix)
    write_json(runner.STATUS_PATH, runner.build_status(matrix, browser_preflight=None, evidence_roots=[]))
    entries = []
    for index, c in enumerate(selected):
        blocked, pending = index in blocked_ids, index == 5
        status = "pending" if pending else "blocked" if blocked else "passed"
        reason = "network_or_gateway" if index == 4 else "missing_required_context" if blocked else None
        contract_id = f"{c['product']}/{c['action']}/{c['resourceType']}/{'.'.join(c['path'])}"
        entries.append(
            {
                "contractId": contract_id,
                "resourceType": c["resourceType"],
                "product": c["product"],
                "action": c["action"],
                "eventStatus": "missing_event" if pending else reason if blocked else "found",
                "sampleLevel": "L2" if status == "passed" else "L0",
                "actualApiCalled": not pending,
                "sourceChainStatus": status,
                "liveDestinationStatus": "pending",
                "cleanupStatus": "not_applicable",
                "failureReason": "missing_event" if pending else reason,
                "sourceEvidence": {
                    "reportDigest": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "checkpointDigest": hashlib.sha256(events.read_bytes()).hexdigest(),
                    "scannedRegions": 1,
                    "sampleDigest": None,
                    "sampleIdentifierDigest": None,
                    "sampleRegionId": None,
                },
                "sourceEvidenceDigest": fake_digest(contract_id),
            }
        )
    for entry in entries:
        if entry["action"] == "ListNamespace":
            entry["sourceEvidenceDigest"] = fake_digest("blocked namespace")
        elif entry["action"] == "ListRepository":
            entry["sourceEvidenceDigest"] = fake_digest("blocked repository")
    write_json(
        runner.QUERY_REPLAY_PATH,
        {
            "schemaVersion": 1,
            "generatedAt": "2000-01-01T00:00:00Z",
            "catalogRevisionDigest": runner._revision()["digest"],
            "sourceReport": {"path": str(source), "schemaVersion": 2, "runId": "a" * 32},
            "checkpoint": {"path": str(events), "runId": "a" * 32},
            "counts": {
                "contracts": len(entries),
                "sourcePassed": sum(e["sourceChainStatus"] == "passed" for e in entries),
                "sourceBlocked": sum(e["sourceChainStatus"] == "blocked" for e in entries),
                "pendingBrowser": len(entries),
            },
            "entries": entries,
        },
    )
    runner.validate_query_replay(json.loads(runner.QUERY_REPLAY_PATH.read_text(encoding="utf-8")))
