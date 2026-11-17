#!/usr/bin/env python3
"""Fail-closed queue guard for the resource-link worker.

The worker executes one pre-planned acceptance document at a time.  This guard
keeps dispatch, acceptance, counting, and queue advancement separate so a long
running supervisor cannot silently turn an acceptance batch into framework or
compatibility work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TASK_DIR = Path(__file__).resolve().parent / "luna-resource-link-tasks"
DEFAULT_SUMMARY = DEFAULT_TASK_DIR / "999_总览与验收汇总.md"
DEFAULT_LEASE_DIR = REPO_ROOT / "evidence" / "formal" / "supervision" / "qwen-batch-leases"
DEFAULT_BACKFILL_GATE = Path(__file__).resolve().parent / "resource_link_backfill_gate.json"
MAINLINE_ID = "aliyun-resource-link-e2e-cn-v1"
TASK_COUNT = 81
HISTORICAL_BACKFILL_TASK_IDS = frozenset(
    {"016", "036", "037", "041", "042", "043", "046", "047", "052", "053", "054"}
)
REQUIRED_WORKER_MODEL = "glm-5.3-prime"
REQUIRED_REASONING_EFFORT = "xhigh"
MAX_PROMPT_BYTES = 12_000
MAX_REPORT_BYTES = 64_000
MAX_REPORT_LINES = 200
EXECUTION_BUDGET_MARKERS = (
    "START_MODE: action_first",
    "ALLOW_STANDALONE_PLANNING_PHASE: false",
    "FIRST_OPERATION_AFTER_REQUIRED_READS: source_discovery_or_ui",
    "ENVIRONMENT_CHECK_MODE: on_failure",
    "MAX_ENV_DIAGNOSTICS_AFTER_FAILURE: 1",
    "MAX_DISCOVERY_PASSES_PER_CONTRACT: 1",
    "DISCOVERY_REGION_POLICY: source_hint_then_relevant_scope_once",
    "SHARED_ROUTE_INVENTORY_PER_BATCH: 1",
    "DISCOVERY_SCRIPT_MODE: one_task_local_concurrent",
    "MAX_DISCOVERY_SCRIPTS_PER_BATCH: 1",
    "DISCOVERY_CONCURRENCY_LIMIT: 8",
    "ALLOW_REPO_RESEARCH: false",
    "FINAL_SUMMARY_MAX_LINES: 2",
)
RESOURCE_TYPE_RE = re.compile(r"(?:ALIYUN::[A-Za-z0-9_]+::[A-Za-z0-9_]+|alicloud_[a-z0-9_]+)")
TASK_HEADING_RE = re.compile(r"^###\s+\d+\.\s+`([^`\r\n]+)`\s*$", re.MULTILINE)
TASK_ROW_RE = re.compile(r"^\| \[(\d{3})\]\(")
GLOBAL_METRIC_TERMS = ("sourcePassed", "normalPassed", "shiftPassed", "fullyPassed", "remaining")
ID_RE = re.compile(r"[0-9a-f-]{36}")
LEGACY_FIXED_THREAD_ID = "01a0b28a-0e34-7ce0-addf-eadc905c5da6"
REPORT_SCENARIO_RE = re.compile(
    r"^- \[([ xX])\] `([^`]+)` \| `([^`]+)` \| (iframe|open) \| (PASS|FAIL|SKIP) \| (.+?) \| (.+?)\s*$",
    re.MULTILINE,
)
FORBIDDEN_REPORT_TERMS = (
    "manifest.json",
    "redaction-audit",
    "task-result.json",
    "task-report.md",
    *GLOBAL_METRIC_TERMS,
)


class GuardError(RuntimeError):
    """Raised when a dispatch or delivery violates the fixed queue protocol."""


def _verify_workflow_owner() -> None:
    marker = REPO_ROOT / "evidence" / "resource-link-workflow-owner.json"
    if marker.exists():
        raise GuardError("resource-level workflow owns dispatch; legacy batch prepare/backfill is disabled")


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    path: Path
    resource_types: tuple[str, ...]
    output_dir: str
    contract_count: int
    digest: str


@dataclass(frozen=True)
class QueueState:
    tasks: dict[str, TaskSpec]
    statuses: dict[str, str]
    first_pending: str | None
    accepted: int
    pending: int
    digest: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GuardError(f"{label} is unreadable: {path}: {error}") from error
    if not isinstance(value, dict):
        raise GuardError(f"{label} must be a JSON object: {path}")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _task_spec(task_dir: Path, task_id: str) -> TaskSpec:
    path = task_dir / f"{task_id}_资源链接验收.md"
    if not path.is_file():
        raise GuardError(f"planned task document is missing: {path}")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    resources = tuple(value.strip() for value in TASK_HEADING_RE.findall(text))
    declared_match = re.search(r"^- 资源类型数：(\d+)\s*$", text, re.MULTILINE)
    contract_match = re.search(r"^- 本任务唯一 contract 数：(\d+)\s*$", text, re.MULTILINE)
    output_match = re.search(r"^- 输出目录：`([^`]+)`\s*$", text, re.MULTILINE)
    if not declared_match or not contract_match or not output_match:
        raise GuardError(f"planned task metadata is incomplete: {path}")
    if int(declared_match.group(1)) != len(resources):
        raise GuardError(f"planned task resource count disagrees with headings: {path}")
    malformed = [value for value in resources if RESOURCE_TYPE_RE.fullmatch(value) is None]
    if malformed:
        raise GuardError(f"planned task contains malformed resource types: {path}: {malformed}")
    if not 1 <= len(resources) <= 5:
        raise GuardError(f"planned task must contain between one and five resource types: {path}")
    if len(set(resources)) != len(resources):
        raise GuardError(f"planned task contains duplicate resource types: {path}")
    return TaskSpec(
        task_id=task_id,
        path=path,
        resource_types=resources,
        output_dir=output_match.group(1),
        contract_count=int(contract_match.group(1)),
        digest=_sha256_bytes(raw),
    )


def load_queue(task_dir: Path = DEFAULT_TASK_DIR, summary_path: Path = DEFAULT_SUMMARY) -> QueueState:
    if not task_dir.is_dir():
        raise GuardError("Legacy task records are archived; supply an explicit --task-dir and --summary for replay")
    tasks = {_id: _task_spec(task_dir, _id) for _id in (f"{value:03d}" for value in range(1, TASK_COUNT + 1))}
    try:
        summary = summary_path.read_text(encoding="utf-8")
    except OSError as error:
        raise GuardError(f"queue summary is unreadable: {summary_path}: {error}") from error
    statuses: dict[str, str] = {}
    for line in summary.splitlines():
        match = TASK_ROW_RE.match(line)
        if match is None:
            continue
        task_id = match.group(1)
        if task_id not in tasks:
            continue
        columns = [part.strip() for part in line.split("|")]
        if len(columns) < 7:
            raise GuardError(f"queue row has too few columns: {task_id}")
        status_column = columns[5]
        if status_column.startswith("accepted"):
            status = "accepted"
        elif status_column.startswith("pending"):
            status = "pending"
        else:
            raise GuardError(f"queue row has unsupported status for task {task_id}: {status_column}")
        if task_id in statuses:
            raise GuardError(f"queue summary repeats task {task_id}")
        statuses[task_id] = status
    missing = sorted(set(tasks) - set(statuses))
    if missing:
        raise GuardError(f"queue summary is missing tasks: {', '.join(missing)}")
    first_pending = next((task_id for task_id in tasks if statuses[task_id] == "pending"), None)
    if first_pending is not None:
        later_accepted = [task_id for task_id in tasks if task_id > first_pending and statuses[task_id] == "accepted"]
        if later_accepted:
            raise GuardError(f"queue is not monotonic; accepted tasks follow {first_pending}: {later_accepted}")
    digest_payload = {
        "tasks": [{"id": task_id, "digest": tasks[task_id].digest} for task_id in tasks],
        "statuses": statuses,
    }
    return QueueState(
        tasks=tasks,
        statuses=statuses,
        first_pending=first_pending,
        accepted=sum(value == "accepted" for value in statuses.values()),
        pending=sum(value == "pending" for value in statuses.values()),
        digest=_sha256_bytes(json.dumps(digest_payload, sort_keys=True).encode("utf-8")),
    )


def _active_leases(lease_dir: Path) -> list[Path]:
    active = []
    for path in sorted(lease_dir.glob("*.json")) if lease_dir.is_dir() else []:
        value = _load_json(path, label="dispatch lease")
        if value.get("state") in {"prepared", "bound", "delivered", "audited_unreplayable"}:
            active.append(path)
    return active


def validate_prompt(prompt: str, spec: TaskSpec) -> None:
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise GuardError(f"dispatch prompt exceeds {MAX_PROMPT_BYTES} bytes")
    if "MODE: resource_acceptance" not in prompt:
        raise GuardError("dispatch prompt must declare MODE: resource_acceptance")
    if f"TASK_ID: {spec.task_id}" not in prompt:
        raise GuardError(f"dispatch prompt must declare TASK_ID: {spec.task_id}")
    if f"OUTPUT_DIR: {spec.output_dir}" not in prompt:
        raise GuardError("dispatch prompt must declare the planned OUTPUT_DIR")
    if f"MODEL: {REQUIRED_WORKER_MODEL}" not in prompt:
        raise GuardError(f"dispatch prompt must declare MODEL: {REQUIRED_WORKER_MODEL}")
    if f"REASONING_EFFORT: {REQUIRED_REASONING_EFFORT}" not in prompt:
        raise GuardError(
            f"dispatch prompt must declare REASONING_EFFORT: {REQUIRED_REASONING_EFFORT}"
        )
    missing_budget = [value for value in EXECUTION_BUDGET_MARKERS if value not in prompt]
    if missing_budget:
        raise GuardError(f"dispatch prompt is missing fixed efficiency budget: {missing_budget}")
    prompt_resources = set(RESOURCE_TYPE_RE.findall(prompt))
    if prompt_resources != set(spec.resource_types):
        raise GuardError(
            "dispatch resource types must exactly match the planned task: "
            f"expected={list(spec.resource_types)!r}, actual={sorted(prompt_resources)!r}"
        )
    metrics = [term for term in GLOBAL_METRIC_TERMS if term in prompt]
    if metrics:
        raise GuardError(f"dispatch prompt must not ask Qwen to read or report global metrics: {metrics}")
    required = (
        "只执行真实测试并填写固定轻量场景报告",
        "只保留 runner 原始输出和 batch-report.md",
        "不得生成 manifest、脱敏审计、task-result.json 或 task-report.md",
        "不得读取或模仿其他批次的报告或模板",
        "不得修改生产代码、runner、schema、ledger 或调度器",
        "完成后结束当前会话，不领取下一任务",
    )
    missing = [value for value in required if value not in prompt]
    if missing:
        raise GuardError(f"dispatch prompt is missing fixed worker boundaries: {missing}")


def _verify_backfill_gate(task_id: str, gate_path: Path) -> None:
    if not gate_path.is_file():
        return
    gate = _load_json(gate_path, label="historical backfill gate")
    if gate.get("mainlineId") != MAINLINE_ID or gate.get("schemaVersion") != 1:
        raise GuardError("historical backfill gate has an invalid mainline or schema")
    before_task_id = gate.get("beforeTaskId")
    if not isinstance(before_task_id, str) or not re.fullmatch(r"\d{3}", before_task_id):
        raise GuardError("historical backfill gate has an invalid task boundary")
    if not isinstance(gate.get("complete"), bool):
        raise GuardError("historical backfill gate has an invalid completion value")
    if task_id >= before_task_id and not gate["complete"]:
        raise GuardError(f"historical backfill must be verified before dispatching task {task_id}")


def prepare_lease(
    *,
    task_id: str,
    prompt_path: Path,
    lease_dir: Path,
    task_dir: Path,
    summary_path: Path,
    backfill_gate_path: Path = DEFAULT_BACKFILL_GATE,
) -> tuple[Path, dict[str, Any]]:
    _verify_workflow_owner()
    queue = load_queue(task_dir, summary_path)
    if queue.first_pending is None:
        raise GuardError("the numbered queue is already complete")
    if task_id != queue.first_pending:
        raise GuardError(
            f"only the first pending task may be dispatched: expected {queue.first_pending}, got {task_id}"
        )
    _verify_backfill_gate(task_id, backfill_gate_path)
    active = _active_leases(lease_dir)
    if active:
        raise GuardError(f"another dispatch lease is still active: {active[0]}")
    try:
        prompt_bytes = prompt_path.read_bytes()
        prompt = prompt_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GuardError(f"dispatch prompt is unreadable: {prompt_path}: {error}") from error
    spec = queue.tasks[task_id]
    validate_prompt(prompt, spec)
    lease_path = lease_dir / f"{task_id}-{uuid.uuid4().hex[:12]}.json"
    lease = {
        "schemaVersion": 2,
        "mainlineId": MAINLINE_ID,
        "state": "prepared",
        "taskId": task_id,
        "preparedAt": _utc_now(),
        "queueDigest": queue.digest,
        "taskDocument": str(spec.path),
        "taskDocumentDigest": spec.digest,
        "resourceTypes": list(spec.resource_types),
        "contractCount": spec.contract_count,
        "outputDirectory": spec.output_dir,
        "promptDigest": _sha256_bytes(prompt_bytes),
        "workerModel": REQUIRED_WORKER_MODEL,
        "reasoningEffort": REQUIRED_REASONING_EFFORT,
        "executionBudget": {
            "startMode": "action_first",
            "allowStandalonePlanningPhase": False,
            "firstOperationAfterRequiredReads": "source_discovery_or_ui",
            "environmentCheckMode": "on_failure",
            "maxEnvironmentDiagnosticsAfterFailure": 1,
            "maxDiscoveryPassesPerContract": 1,
            "discoveryRegionPolicy": "source_hint_then_relevant_scope_once",
            "sharedRouteInventoryPerBatch": 1,
            "discoveryScriptMode": "one_task_local_concurrent",
            "maxDiscoveryScriptsPerBatch": 1,
            "discoveryConcurrencyLimit": 8,
            "allowRepositoryResearch": False,
            "finalSummaryMaxLines": 2,
        },
        "workerThreadId": None,
        "turnId": None,
    }
    _write_json_atomic(lease_path, lease)
    return lease_path, lease


def _regenerate_route_plan(spec: TaskSpec) -> dict[str, Any]:
    """Recheck an empty route plan against the current official source data."""
    runner = Path(__file__).with_name("run_resource_link_acceptance.py")
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "route-plan.json"
        command = [
            sys.executable,
            str(runner),
            "--phase", "plan",
            "--mode", "audit-only",
            "--site", "aliyun",
            "--source", "route_catalog",
            "--batch-size", str(len(spec.resource_types)),
        ]
        for resource_type in spec.resource_types:
            command.extend(("--resource-type", resource_type))
        command.extend(("--plan-output", str(output)))
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GuardError(f"official route plan regeneration failed: {error}") from error
        if result.returncode != 0 or not output.is_file():
            raise GuardError(
                "official route plan regeneration failed: "
                f"exit={result.returncode}, stderr={result.stderr[-500:]}"
            )
        return _load_json(output, label="regenerated official route plan")


def audit_unreplayable_lease(
    *,
    task_id: str,
    plan_path: Path,
    lease_dir: Path,
    task_dir: Path,
    summary_path: Path,
    backfill_gate_path: Path = DEFAULT_BACKFILL_GATE,
) -> tuple[Path, dict[str, Any]]:
    """Lock an all-unreplayable official plan without inventing a worker turn."""
    queue = load_queue(task_dir, summary_path)
    if task_id != queue.first_pending:
        raise GuardError(f"only the first pending task may be audited: expected {queue.first_pending}, got {task_id}")
    _verify_backfill_gate(task_id, backfill_gate_path)
    active = _active_leases(lease_dir)
    if active:
        raise GuardError(f"another dispatch lease is still active: {active[0]}")
    spec = queue.tasks[task_id]
    if spec.contract_count != len(spec.resource_types):
        raise GuardError("unreplayable audit requires one route contract per task resource type")
    expected_dir = (REPO_ROOT / spec.output_dir).resolve()
    resolved_plan = plan_path.resolve()
    if not resolved_plan.is_relative_to(expected_dir):
        raise GuardError("unreplayable audit plan must be inside the current task output directory")
    plan_bytes = resolved_plan.read_bytes()
    plan = _load_json(resolved_plan, label="official route plan")
    current = _regenerate_route_plan(spec)
    for value in (plan, current):
        if (
            value.get("schemaVersion") != 1
            or value.get("site") != "aliyun"
            or value.get("source") != "route_catalog"
            or value.get("resume") is not False
            or value.get("batchSize") != 0
            or value.get("items") != []
            or value.get("skippedUnreplayable") != spec.contract_count
            or value.get("skippedBlocked") != 0
            or value.get("skippedPending") != 0
        ):
            raise GuardError("official route plan is not wholly unreplayable for this task")
    stable_fields = (
        "schemaVersion", "catalogRevisionDigest", "site", "source", "batchSize",
        "resume", "skippedBlocked", "skippedPending", "skippedUnreplayable", "items",
    )
    if any(plan.get(field) != current.get(field) for field in stable_fields):
        raise GuardError("stored route plan disagrees with a fresh official regeneration")
    lease_path = lease_dir / f"{task_id}-unreplayable-{uuid.uuid4().hex[:12]}.json"
    lease = {
        "schemaVersion": 2,
        "mainlineId": MAINLINE_ID,
        "mode": "audited_unreplayable",
        "state": "audited_unreplayable",
        "taskId": task_id,
        "preparedAt": _utc_now(),
        "queueDigest": queue.digest,
        "taskDocument": str(spec.path),
        "taskDocumentDigest": spec.digest,
        "resourceTypes": list(spec.resource_types),
        "contractCount": spec.contract_count,
        "outputDirectory": spec.output_dir,
        "planPath": str(resolved_plan),
        "planDigest": _sha256_bytes(plan_bytes),
        "planId": plan.get("planId"),
        "workerThreadId": None,
        "turnId": None,
    }
    _write_json_atomic(lease_path, lease)
    return lease_path, lease


def prepare_backfill_lease(
    *,
    task_id: str,
    prompt_path: Path,
    backfill_task_dir: Path,
    lease_dir: Path,
    task_dir: Path,
    summary_path: Path,
) -> tuple[Path, dict[str, Any]]:
    """Lease one explicitly authorized historical gap without advancing the main queue."""
    _verify_workflow_owner()
    queue = load_queue(task_dir, summary_path)
    if task_id not in HISTORICAL_BACKFILL_TASK_IDS:
        raise GuardError(f"task is not in the authorized historical backfill set: {task_id}")
    if queue.statuses.get(task_id) != "accepted":
        raise GuardError(f"historical backfill requires an already accepted task: {task_id}")
    active = _active_leases(lease_dir)
    if active:
        raise GuardError(f"another dispatch lease is still active: {active[0]}")
    spec = _task_spec(backfill_task_dir, task_id)
    original = queue.tasks[task_id]
    if not set(spec.resource_types).issubset(original.resource_types):
        raise GuardError("historical backfill expands the original resource types")
    if spec.contract_count < 1 or spec.contract_count > original.contract_count:
        raise GuardError("historical backfill contract count is outside the original task")
    if not spec.output_dir.startswith(f"evidence/formal/task-batches/{task_id}/backfill-"):
        raise GuardError("historical backfill output must stay under the original task directory")
    try:
        prompt_bytes = prompt_path.read_bytes()
        prompt = prompt_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GuardError(f"dispatch prompt is unreadable: {prompt_path}: {error}") from error
    validate_prompt(prompt, spec)
    lease_path = lease_dir / f"{task_id}-backfill-{uuid.uuid4().hex[:12]}.json"
    lease = {
        "schemaVersion": 2,
        "mainlineId": MAINLINE_ID,
        "mode": "historical_backfill",
        "state": "prepared",
        "taskId": task_id,
        "preparedAt": _utc_now(),
        "queueDigest": queue.digest,
        "firstPendingAtPrepare": queue.first_pending,
        "taskDocument": str(spec.path),
        "taskDocumentDigest": spec.digest,
        "originalTaskDocument": str(original.path),
        "originalTaskDocumentDigest": original.digest,
        "resourceTypes": list(spec.resource_types),
        "contractCount": spec.contract_count,
        "outputDirectory": spec.output_dir,
        "promptDigest": _sha256_bytes(prompt_bytes),
        "workerModel": REQUIRED_WORKER_MODEL,
        "reasoningEffort": REQUIRED_REASONING_EFFORT,
        "workerThreadId": None,
        "turnId": None,
    }
    _write_json_atomic(lease_path, lease)
    return lease_path, lease


def bind_lease(
    lease_path: Path, thread_id: str, turn_id: str, model: str, effort: str,
    worker_backend: str = "bailian-local",
) -> dict[str, Any]:
    lease = _load_json(lease_path, label="dispatch lease")
    if (
        lease.get("state") != "prepared"
        or lease.get("workerThreadId") is not None
        or lease.get("turnId") is not None
    ):
        raise GuardError("only a prepared unbound lease may be bound")
    if ID_RE.fullmatch(thread_id) is None:
        raise GuardError("worker thread ID has an invalid shape")
    if thread_id == LEGACY_FIXED_THREAD_ID:
        raise GuardError("the retired fixed Qwen thread must not be reused; create a fresh worker thread")
    if ID_RE.fullmatch(turn_id) is None:
        raise GuardError("turn ID has an invalid shape")
    if worker_backend not in {"bailian-local", "macmini"}:
        raise GuardError("worker backend must be bailian-local or macmini")
    if model != REQUIRED_WORKER_MODEL:
        raise GuardError(
            f"worker model must be {REQUIRED_WORKER_MODEL}, got {model or '<empty>'}"
        )
    if effort != REQUIRED_REASONING_EFFORT:
        raise GuardError(
            "worker reasoning effort must be "
            f"{REQUIRED_REASONING_EFFORT}, got {effort or '<empty>'}"
        )
    for other_path in lease_path.parent.glob("*.json"):
        if other_path == lease_path:
            continue
        other = _load_json(other_path, label="dispatch lease")
        if other.get("workerThreadId") == thread_id:
            raise GuardError(f"worker thread is already bound to another lease: {other_path}")
    lease.update(
        state="bound",
        boundAt=_utc_now(),
        workerThreadId=thread_id,
        turnId=turn_id,
        workerModel=model,
        reasoningEffort=effort,
        workerBackend=worker_backend,
    )
    _write_json_atomic(lease_path, lease)
    return lease


def authorize_steer(lease_path: Path, message_path: Path) -> dict[str, Any]:
    lease = _load_json(lease_path, label="dispatch lease")
    if lease.get("state") != "bound" or not lease.get("turnId"):
        raise GuardError("only the active bound lease may receive a correction")
    try:
        message_bytes = message_path.read_bytes()
        message = message_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GuardError(f"correction message is unreadable: {message_path}: {error}") from error
    required = (
        "MODE: resource_acceptance_correction",
        f"TASK_ID: {lease['taskId']}",
        "只纠正当前任务的执行偏差",
        "不得扩大资源类型或 contract 范围",
        "不得修改生产代码、runner、schema、ledger 或调度器",
        "完成后立即填写现状报告并结束当前会话",
    )
    missing = [value for value in required if value not in message]
    if missing:
        raise GuardError(f"correction message is missing fixed boundaries: {missing}")
    message_resources = set(RESOURCE_TYPE_RE.findall(message))
    unexpected = message_resources - set(lease.get("resourceTypes") or [])
    if unexpected:
        raise GuardError(f"correction message expands the planned resource scope: {sorted(unexpected)}")
    metrics = [term for term in GLOBAL_METRIC_TERMS if term in message]
    if metrics:
        raise GuardError(f"correction message must not assign global metrics to Qwen: {metrics}")
    digest = _sha256_bytes(message_bytes)
    existing = lease.get("authorizedSteers") or []
    if existing and existing[-1].get("messageDigest") == digest:
        return lease
    if existing:
        raise GuardError("the active resource batch already used its single correction allowance")
    lease["authorizedSteers"] = [
        {
            "authorizedAt": _utc_now(),
            "messagePath": str(message_path),
            "messageDigest": digest,
        }
    ]
    _write_json_atomic(lease_path, lease)
    return lease


def _report_header(report: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*`?([^`\r\n]+?)`?\s*$", report, re.MULTILINE)
    if match is None:
        raise GuardError(f"batch report is missing {key}")
    return match.group(1).strip()


def receive_report(
    lease_path: Path, thread_id: str, turn_id: str, report_path: Path
) -> dict[str, Any]:
    lease = _load_json(lease_path, label="dispatch lease")
    legacy_thread = lease.get("schemaVersion") == 1 and lease.get("workerThreadId") is None
    thread_matches = lease.get("workerThreadId") == thread_id or (
        legacy_thread and thread_id == LEGACY_FIXED_THREAD_ID
    )
    if lease.get("state") != "bound" or lease.get("turnId") != turn_id or not thread_matches:
        raise GuardError("delivery does not match the active bound lease")
    try:
        report_bytes = report_path.read_bytes()
        report = report_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GuardError(f"batch report is unreadable: {report_path}: {error}") from error
    if len(report_bytes) > MAX_REPORT_BYTES:
        raise GuardError(f"batch report exceeds {MAX_REPORT_BYTES} bytes")
    if len(report.splitlines()) > MAX_REPORT_LINES:
        raise GuardError(f"batch report exceeds {MAX_REPORT_LINES} lines")
    if _report_header(report, "TASK_ID") != str(lease.get("taskId")):
        raise GuardError("batch report task ID does not match the dispatch lease")
    if _report_header(report, "QWEN_THREAD_ID") != thread_id:
        raise GuardError("batch report worker thread ID does not match the dispatch lease")
    conclusion = _report_header(report, "CONCLUSION")
    if conclusion not in {"PASS", "PARTIAL", "BLOCKED"}:
        raise GuardError("batch report conclusion must be PASS, PARTIAL, or BLOCKED")
    if not _report_header(report, "RAW_OUTPUT_DIR"):
        raise GuardError("batch report raw output directory must not be empty")
    forbidden = [term for term in FORBIDDEN_REPORT_TERMS if term in report]
    if forbidden:
        raise GuardError(f"batch report contains forbidden governance or global metric fields: {forbidden}")
    rows = REPORT_SCENARIO_RE.findall(report)
    if not rows:
        raise GuardError("batch report must contain fixed-format scenario rows")
    seen: set[tuple[str, str, str]] = set()
    pair_modes: dict[tuple[str, str], set[str]] = {}
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    resources: set[str] = set()
    for checkbox, resource_type, contract_id, mode, status, _source, _landing in rows:
        if RESOURCE_TYPE_RE.fullmatch(resource_type) is None:
            raise GuardError(f"batch report contains an invalid resource type: {resource_type}")
        key = (resource_type, contract_id, mode)
        if key in seen:
            raise GuardError(f"batch report repeats a scenario row: {key}")
        seen.add(key)
        pair_modes.setdefault((resource_type, contract_id), set()).add(mode)
        resources.add(resource_type)
        counts[status] += 1
        checked = checkbox.lower() == "x"
        if checked != (status == "PASS"):
            raise GuardError("only PASS scenario rows may be checked")
    expected_resources = set(lease.get("resourceTypes") or [])
    if resources != expected_resources:
        raise GuardError(
            "batch report resource types do not match the dispatch lease: "
            f"expected={sorted(expected_resources)!r}, actual={sorted(resources)!r}"
        )
    incomplete = [key for key, modes in pair_modes.items() if modes != {"iframe", "open"}]
    if incomplete:
        raise GuardError(f"batch report must include iframe and open for every contract: {incomplete}")
    expected_contract_count = int(lease.get("contractCount") or 0)
    if expected_contract_count and len(pair_modes) != expected_contract_count:
        raise GuardError(
            "batch report contract count does not match the dispatch lease: "
            f"expected={expected_contract_count}, actual={len(pair_modes)}"
        )
    for status, expected in counts.items():
        match = re.search(rf"^- {status}:\s*`?(\d+)`?\s*$", report, re.MULTILINE)
        if match is None or int(match.group(1)) != expected:
            raise GuardError(f"batch report {status} summary does not match scenario rows")
    if conclusion == "PASS" and (counts["FAIL"] or counts["SKIP"]):
        raise GuardError("a PASS batch report cannot contain FAIL or SKIP scenarios")
    if conclusion == "PARTIAL" and not (counts["FAIL"] or counts["SKIP"]):
        raise GuardError("a PARTIAL batch report must contain at least one FAIL or SKIP scenario")
    lease.update(
        state="delivered",
        deliveredAt=_utc_now(),
        workerThreadId=thread_id,
        reportPath=str(report_path),
        reportDigest=_sha256_bytes(report_bytes),
        reportConclusion=conclusion,
        reportScenarioCounts=counts,
    )
    _write_json_atomic(lease_path, lease)
    return lease


def close_lease(lease_path: Path, *, decision: str, task_dir: Path, summary_path: Path) -> dict[str, Any]:
    lease = _load_json(lease_path, label="dispatch lease")
    if lease.get("mode") == "audited_unreplayable":
        if lease.get("state") != "audited_unreplayable" or decision != "accepted_partial":
            raise GuardError("audited unreplayable tasks may only close as accepted_partial")
        queue = load_queue(task_dir, summary_path)
        task_id = str(lease.get("taskId"))
        if queue.statuses.get(task_id) != "accepted":
            raise GuardError("audited unreplayable task must first be recorded as accepted_partial in the summary")
        if queue.tasks[task_id].digest != lease.get("taskDocumentDigest"):
            raise GuardError("audited unreplayable task document changed after preparation")
        if _sha256_bytes(Path(str(lease.get("planPath"))).read_bytes()) != lease.get("planDigest"):
            raise GuardError("audited unreplayable plan changed after preparation")
        lease.update(state="completed", completedAt=_utc_now(), supervisorDecision=decision)
        _write_json_atomic(lease_path, lease)
        return lease
    if lease.get("mode") == "historical_backfill":
        if decision not in {"backfill_verified", "retry_current_task"}:
            raise GuardError("historical backfill must be verified or retried without changing mainline acceptance")
        if lease.get("state") == "delivered":
            pass
        elif lease.get("state") == "bound" and decision == "retry_current_task":
            pass
        else:
            raise GuardError("historical backfill is not ready to close")
        queue = load_queue(task_dir, summary_path)
        task_id = str(lease.get("taskId"))
        if queue.statuses.get(task_id) != "accepted":
            raise GuardError("historical backfill cannot change the original accepted queue row")
        if decision == "backfill_verified" and (lease.get("reportScenarioCounts") or {}).get("SKIP", 0):
            raise GuardError("historical backfill still has unexecuted scenarios")
        lease.update(
            state="completed" if decision == "backfill_verified" else "closed",
            completedAt=_utc_now(),
            supervisorDecision=decision,
        )
        _write_json_atomic(lease_path, lease)
        return lease
    if decision not in {"accepted", "accepted_partial", "retry_current_task", "blocked"}:
        raise GuardError("unsupported supervisor decision")
    if lease.get("state") == "delivered":
        pass
    elif lease.get("state") == "bound" and decision == "retry_current_task":
        # 执行遗漏或交付缺项时，允许对已绑定但未交付的 lease 以 retry 收口，
        # 其它决策仍要求先 delivered（fail-closed）。
        pass
    else:
        raise GuardError("only a delivered lease may be closed (or a bound lease as retry_current_task)")
    queue = load_queue(task_dir, summary_path)
    task_id = str(lease.get("taskId"))
    accepted = decision in {"accepted", "accepted_partial"}
    expected_status = "accepted" if accepted else "pending"
    if queue.statuses.get(task_id) != expected_status:
        raise GuardError(
            f"queue row must remain {expected_status} for supervisor decision {decision}: task {task_id}"
        )
    lease.update(
        state="completed" if accepted else "closed",
        completedAt=_utc_now(),
        supervisorDecision=decision,
    )
    _write_json_atomic(lease_path, lease)
    return lease


def cancel_lease(lease_path: Path, *, reason: str) -> dict[str, Any]:
    lease = _load_json(lease_path, label="dispatch lease")
    if (
        lease.get("state") != "prepared"
        or lease.get("workerThreadId") is not None
        or lease.get("turnId") is not None
    ):
        raise GuardError("only an unsent prepared lease may be cancelled")
    if not reason.strip():
        raise GuardError("cancellation reason must not be empty")
    lease.update(state="cancelled", cancelledAt=_utc_now(), cancellationReason=reason.strip())
    _write_json_atomic(lease_path, lease)
    return lease


def _queue_payload(queue: QueueState, lease_dir: Path) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mainlineId": MAINLINE_ID,
        "taskCount": len(queue.tasks),
        "accepted": queue.accepted,
        "pending": queue.pending,
        "firstPending": queue.first_pending,
        "queueDigest": queue.digest,
        "activeLeases": [str(path) for path in _active_leases(lease_dir)],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, default=DEFAULT_TASK_DIR)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--lease-dir", type=Path, default=DEFAULT_LEASE_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--task-id", required=True)
    prepare.add_argument("--prompt-file", required=True, type=Path)
    unreplayable = subparsers.add_parser("audit-unreplayable")
    unreplayable.add_argument("--task-id", required=True)
    unreplayable.add_argument("--plan-file", required=True, type=Path)
    backfill = subparsers.add_parser("prepare-backfill")
    backfill.add_argument("--task-id", required=True)
    backfill.add_argument("--prompt-file", required=True, type=Path)
    backfill.add_argument("--backfill-task-dir", required=True, type=Path)
    bind = subparsers.add_parser("bind")
    bind.add_argument("--lease-file", required=True, type=Path)
    bind.add_argument("--thread-id", required=True)
    bind.add_argument("--turn-id", required=True)
    bind.add_argument("--model", required=True)
    bind.add_argument("--effort", required=True)
    bind.add_argument("--worker-backend", choices=("bailian-local", "macmini"), default="bailian-local")
    receive = subparsers.add_parser("receive")
    receive.add_argument("--lease-file", required=True, type=Path)
    receive.add_argument("--thread-id", required=True)
    receive.add_argument("--turn-id", required=True)
    receive.add_argument("--report-file", required=True, type=Path)
    steer = subparsers.add_parser("authorize-steer")
    steer.add_argument("--lease-file", required=True, type=Path)
    steer.add_argument("--message-file", required=True, type=Path)
    close = subparsers.add_parser("close")
    close.add_argument("--lease-file", required=True, type=Path)
    close.add_argument(
        "--decision",
        required=True,
        choices=("accepted", "accepted_partial", "retry_current_task", "blocked", "backfill_verified"),
    )
    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--lease-file", required=True, type=Path)
    cancel.add_argument("--reason", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "status":
            payload = _queue_payload(load_queue(args.task_dir, args.summary), args.lease_dir)
        elif args.command == "prepare":
            lease_path, lease = prepare_lease(
                task_id=args.task_id,
                prompt_path=args.prompt_file,
                lease_dir=args.lease_dir,
                task_dir=args.task_dir,
                summary_path=args.summary,
            )
            payload = {"status": "prepared", "leasePath": str(lease_path), "lease": lease}
        elif args.command == "audit-unreplayable":
            lease_path, lease = audit_unreplayable_lease(
                task_id=args.task_id,
                plan_path=args.plan_file,
                lease_dir=args.lease_dir,
                task_dir=args.task_dir,
                summary_path=args.summary,
            )
            payload = {"status": "audited_unreplayable", "leasePath": str(lease_path), "lease": lease}
        elif args.command == "prepare-backfill":
            lease_path, lease = prepare_backfill_lease(
                task_id=args.task_id,
                prompt_path=args.prompt_file,
                backfill_task_dir=args.backfill_task_dir,
                lease_dir=args.lease_dir,
                task_dir=args.task_dir,
                summary_path=args.summary,
            )
            payload = {"status": "prepared", "leasePath": str(lease_path), "lease": lease}
        elif args.command == "bind":
            payload = {
                "status": "bound",
                "lease": bind_lease(
                    args.lease_file,
                    args.thread_id,
                    args.turn_id,
                    args.model,
                    args.effort,
                    args.worker_backend,
                ),
            }
        elif args.command == "receive":
            payload = {
                "status": "delivered",
                "lease": receive_report(
                    args.lease_file,
                    args.thread_id,
                    args.turn_id,
                    args.report_file,
                ),
            }
        elif args.command == "authorize-steer":
            payload = {
                "status": "authorized",
                "lease": authorize_steer(args.lease_file, args.message_file),
            }
        elif args.command == "close":
            lease = close_lease(
                args.lease_file,
                decision=args.decision,
                task_dir=args.task_dir,
                summary_path=args.summary,
            )
            payload = {
                "status": lease["state"],
                "lease": lease,
            }
        else:
            payload = {
                "status": "cancelled",
                "lease": cancel_lease(args.lease_file, reason=args.reason),
            }
    except GuardError as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
