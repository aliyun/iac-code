from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

THREAD_ID = "11111111-1111-4111-8111-111111111111"
TURN_ID = "22222222-2222-4222-8222-222222222222"
MODEL = "glm-5.3-prime"
EFFORT = "xhigh"


def _guard_module():
    path = Path(__file__).parents[2] / "scripts" / "web" / "e2e" / "qwen_batch_guard.py"
    spec = importlib.util.spec_from_file_location("qwen_batch_guard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # Offline queue fixtures must not inherit a live worktree's dispatch owner.
    module.REPO_ROOT = Path(__file__).parents[2] / "__offline_guard_fixture__"
    return module


def _resources(task_id: str) -> list[str]:
    return [f"ALIYUN::TEST::Resource{task_id}{index}" for index in range(1, 6)]


def _queue(tmp_path: Path, *, accepted: int = 10) -> tuple[Path, Path, Path]:
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    rows = []
    for number in range(1, 82):
        task_id = f"{number:03d}"
        resources = _resources(task_id)
        (task_dir / f"{task_id}_资源链接验收.md").write_text(
            "\n".join(
                [
                    f"# {task_id}",
                    f"- 输出目录：`evidence/formal/task-batches/{task_id}/`",
                    "- 资源类型数：5",
                    "- 本任务唯一 contract 数：5",
                    *[f"### {index}. `{resource}`" for index, resource in enumerate(resources, 1)],
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        status = "accepted（partial）" if number <= accepted else "pending"
        rows.append(f"| [{task_id}](./{task_id}_资源链接验收.md) | types | 5 | out | {status} | turn |")
    summary = task_dir / "999_总览与验收汇总.md"
    summary.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return task_dir, summary, tmp_path / "leases"


def _prompt(path: Path, task_id: str = "011") -> Path:
    path.write_text(
        "\n".join(
            [
                "MODE: resource_acceptance",
                f"TASK_ID: {task_id}",
                f"OUTPUT_DIR: evidence/formal/task-batches/{task_id}/",
                f"MODEL: {MODEL}",
                f"REASONING_EFFORT: {EFFORT}",
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
                *_resources(task_id),
                "只执行真实测试并填写固定轻量场景报告",
                "只保留 runner 原始输出和 batch-report.md",
                "不得生成 manifest、脱敏审计、task-result.json 或 task-report.md",
                "不得读取或模仿其他批次的报告或模板",
                "不得修改生产代码、runner、schema、ledger 或调度器",
                "完成后结束当前会话，不领取下一任务",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _report(
    path: Path,
    *,
    task_id: str = "011",
    thread_id: str = THREAD_ID,
    partial: bool = False,
) -> Path:
    rows: list[str] = []
    for index, resource in enumerate(_resources(task_id), 1):
        contract = f"contract-{index}"
        for mode in ("iframe", "open"):
            if partial and index == 5 and mode == "open":
                rows.append(f"- [ ] `{resource}` | `{contract}` | {mode} | SKIP | `no sample` | `未执行`")
            else:
                rows.append(f"- [x] `{resource}` | `{contract}` | {mode} | PASS | `sample` | `real landing`")
    path.write_text(
        "\n".join(
            [
                "# 资源链接批次报告",
                f"TASK_ID: `{task_id}`",
                f"QWEN_THREAD_ID: `{thread_id}`",
                f"CONCLUSION: `{'PARTIAL' if partial else 'PASS'}`",
                f"RAW_OUTPUT_DIR: `evidence/formal/task-batches/{task_id}/raw`",
                "",
                "## 测试场景",
                *rows,
                "",
                "## 待修项",
                "- 无" if not partial else "- contract-5 无样本",
                "",
                "## 汇总",
                f"- PASS: `{9 if partial else 10}`",
                "- FAIL: `0`",
                f"- SKIP: `{1 if partial else 0}`",
                "- 全局阻塞: 无",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _prepare(tmp_path: Path):
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path)
    lease_path, lease = guard.prepare_lease(
        task_id="011",
        prompt_path=_prompt(tmp_path / "prompt.txt"),
        lease_dir=lease_dir,
        task_dir=task_dir,
        summary_path=summary,
    )
    return guard, task_dir, summary, lease_dir, lease_path, lease


def test_queue_is_complete_monotonic_and_selects_first_pending(tmp_path: Path) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path)

    queue = guard.load_queue(task_dir, summary)

    assert len(queue.tasks) == 81
    assert queue.accepted == 10
    assert queue.pending == 71
    assert queue.first_pending == "011"
    assert len(queue.tasks["011"].resource_types) == 5
    assert guard._queue_payload(queue, lease_dir)["mainlineId"] == "aliyun-resource-link-e2e-cn-v1"


def test_prepare_rejects_out_of_order_and_global_metric_work(tmp_path: Path) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path)

    with pytest.raises(guard.GuardError, match="first pending"):
        guard.prepare_lease(
            task_id="012",
            prompt_path=_prompt(tmp_path / "wrong.txt", "012"),
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )

    prompt = _prompt(tmp_path / "metrics.txt")
    prompt.write_text(prompt.read_text(encoding="utf-8") + "remaining: 500\n", encoding="utf-8")
    with pytest.raises(guard.GuardError, match="global metrics"):
        guard.prepare_lease(
            task_id="011",
            prompt_path=prompt,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )


def test_prepare_rejects_missing_efficiency_budget(tmp_path: Path) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path)
    prompt = _prompt(tmp_path / "unbounded.txt")
    prompt.write_text(
        prompt.read_text(encoding="utf-8").replace(
            "DISCOVERY_REGION_POLICY: source_hint_then_relevant_scope_once\n", ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(guard.GuardError, match="fixed efficiency budget"):
        guard.prepare_lease(
            task_id="011",
            prompt_path=prompt,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )


def test_prepare_rejects_previous_worker_model(tmp_path: Path) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path)
    prompt = _prompt(tmp_path / "old-model.txt")
    prompt.write_text(
        prompt.read_text(encoding="utf-8").replace(f"MODEL: {MODEL}", "MODEL: qwen3.8-max-0902"),
        encoding="utf-8",
    )

    with pytest.raises(guard.GuardError, match="dispatch prompt must declare MODEL"):
        guard.prepare_lease(
            task_id="011",
            prompt_path=prompt,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )


def test_prepare_rejects_missing_action_first_start_gate(tmp_path: Path) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path)
    prompt = _prompt(tmp_path / "planning-first.txt")
    prompt.write_text(
        prompt.read_text(encoding="utf-8").replace(
            "FIRST_OPERATION_AFTER_REQUIRED_READS: source_discovery_or_ui\n", ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(guard.GuardError, match="fixed efficiency budget"):
        guard.prepare_lease(
            task_id="011",
            prompt_path=prompt,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )


def test_bind_requires_fresh_worker_thread_and_prevents_reuse(tmp_path: Path) -> None:
    guard, task_dir, summary, lease_dir, lease_path, _lease = _prepare(tmp_path)

    with pytest.raises(guard.GuardError, match="retired fixed Qwen thread"):
        guard.bind_lease(lease_path, guard.LEGACY_FIXED_THREAD_ID, TURN_ID, MODEL, EFFORT)

    bound = guard.bind_lease(lease_path, THREAD_ID, TURN_ID, MODEL, EFFORT)
    assert bound["workerBackend"] == "bailian-local"
    guard.receive_report(lease_path, THREAD_ID, TURN_ID, _report(tmp_path / "batch-report.md"))
    summary.write_text(
        summary.read_text(encoding="utf-8").replace(
            "| [011](./011_资源链接验收.md) | types | 5 | out | pending | turn |",
            "| [011](./011_资源链接验收.md) | types | 5 | out | accepted | turn |",
        ),
        encoding="utf-8",
    )
    guard.close_lease(lease_path, decision="accepted", task_dir=task_dir, summary_path=summary)
    next_path, _next = guard.prepare_lease(
        task_id="012",
        prompt_path=_prompt(tmp_path / "next-prompt.txt", "012"),
        lease_dir=lease_dir,
        task_dir=task_dir,
        summary_path=summary,
    )
    with pytest.raises(guard.GuardError, match="already bound"):
        guard.bind_lease(
            next_path,
            THREAD_ID,
            "33333333-3333-4333-8333-333333333333",
            MODEL,
            EFFORT,
        )


@pytest.mark.parametrize(
    ("model", "effort", "message"),
    [
        ("qwen3.8-max-0902", EFFORT, "worker model must be"),
        ("deepseek-v4-flash-0731", EFFORT, "worker model must be"),
        (MODEL, "high", "reasoning effort must be"),
    ],
)
def test_bind_rejects_wrong_worker_model_or_effort(tmp_path: Path, model: str, effort: str, message: str) -> None:
    guard, _task_dir, _summary, _lease_dir, lease_path, lease = _prepare(tmp_path)

    assert lease["workerModel"] == MODEL
    assert lease["reasoningEffort"] == EFFORT
    assert lease["executionBudget"] == {
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
    }
    with pytest.raises(guard.GuardError, match=message):
        guard.bind_lease(lease_path, THREAD_ID, TURN_ID, model, effort)


def test_receive_accepts_fixed_lightweight_report(tmp_path: Path) -> None:
    guard, _task_dir, _summary, _lease_dir, lease_path, _lease = _prepare(tmp_path)
    guard.bind_lease(lease_path, THREAD_ID, TURN_ID, MODEL, EFFORT)

    delivered = guard.receive_report(
        lease_path,
        THREAD_ID,
        TURN_ID,
        _report(tmp_path / "batch-report.md", partial=True),
    )

    assert delivered["state"] == "delivered"
    assert delivered["reportConclusion"] == "PARTIAL"
    assert delivered["reportScenarioCounts"] == {"PASS": 9, "FAIL": 0, "SKIP": 1}


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda text: text.replace("- [x]", "- [ ]", 1), "only PASS"),
        (lambda text: text.replace(" | open |", " | iframe |", 1), "repeats a scenario"),
        (lambda text: text + "manifest.json\n", "forbidden governance"),
        (lambda text: text.replace("Resource0115", "Resource0999"), "resource types do not match"),
        (
            lambda text: text.replace(
                "\n## 待修项",
                "\n- [x] `ALIYUN::TEST::Resource0111` | `extra-contract` | iframe | PASS | `sample` | `landing`"
                "\n- [x] `ALIYUN::TEST::Resource0111` | `extra-contract` | open | PASS | `sample` | `landing`"
                "\n\n## 待修项",
            ),
            "contract count",
        ),
    ],
)
def test_receive_rejects_invalid_or_governance_heavy_reports(tmp_path: Path, mutator, message: str) -> None:
    guard, _task_dir, _summary, _lease_dir, lease_path, _lease = _prepare(tmp_path)
    guard.bind_lease(lease_path, THREAD_ID, TURN_ID, MODEL, EFFORT)
    report = _report(tmp_path / "batch-report.md")
    report.write_text(mutator(report.read_text(encoding="utf-8")), encoding="utf-8")

    with pytest.raises(guard.GuardError, match=message):
        guard.receive_report(lease_path, THREAD_ID, TURN_ID, report)


def test_bound_lease_can_be_closed_only_as_retry_current_task(tmp_path: Path) -> None:
    guard, task_dir, summary, _lease_dir, lease_path, _lease = _prepare(tmp_path)
    guard.bind_lease(lease_path, THREAD_ID, TURN_ID, MODEL, EFFORT)

    # 执行遗漏/交付缺项：bound lease 只能以 retry_current_task 收口。
    closed = guard.close_lease(lease_path, decision="retry_current_task", task_dir=task_dir, summary_path=summary)
    assert closed["state"] == "closed"
    assert closed["supervisorDecision"] == "retry_current_task"

    # bound lease 不能被 accepted 类决策收口（fail-closed）。
    second = tmp_path / "second"
    second.mkdir()
    guard2, task_dir2, summary2, _lease_dir2, lease_path2, _lease2 = _prepare(second)
    guard2.bind_lease(lease_path2, THREAD_ID, "33333333-3333-4333-8333-333333333333", MODEL, EFFORT)
    with pytest.raises(guard2.GuardError, match=r"only a delivered lease may be closed \(or a bound"):
        guard2.close_lease(lease_path2, decision="accepted_partial", task_dir=task_dir2, summary_path=summary2)


def test_supervisor_must_accept_before_advancing(tmp_path: Path) -> None:
    guard, task_dir, summary, _lease_dir, lease_path, _lease = _prepare(tmp_path)
    guard.bind_lease(lease_path, THREAD_ID, TURN_ID, MODEL, EFFORT)
    guard.receive_report(lease_path, THREAD_ID, TURN_ID, _report(tmp_path / "batch-report.md"))

    with pytest.raises(guard.GuardError, match="must remain accepted"):
        guard.close_lease(lease_path, decision="accepted", task_dir=task_dir, summary_path=summary)


def test_unsent_dispatch_can_be_cancelled_and_reprepared(tmp_path: Path) -> None:
    guard, task_dir, summary, lease_dir, lease_path, _lease = _prepare(tmp_path)

    cancelled = guard.cancel_lease(lease_path, reason="new failed before a worker thread was created")
    assert cancelled["state"] == "cancelled"
    next_path, next_lease = guard.prepare_lease(
        task_id="011",
        prompt_path=_prompt(tmp_path / "second-prompt.txt"),
        lease_dir=lease_dir,
        task_dir=task_dir,
        summary_path=summary,
    )
    assert next_path != lease_path
    assert next_lease["state"] == "prepared"


def test_one_in_scope_correction_is_allowed(tmp_path: Path) -> None:
    guard, _task_dir, _summary, _lease_dir, lease_path, _lease = _prepare(tmp_path)
    guard.bind_lease(lease_path, THREAD_ID, TURN_ID, MODEL, EFFORT)
    correction = tmp_path / "correction.txt"
    correction.write_text(
        "\n".join(
            [
                "MODE: resource_acceptance_correction",
                "TASK_ID: 011",
                "只纠正当前任务的执行偏差",
                "请补做 ALIYUN::TEST::Resource0111 的 Shift 点击。",
                "不得扩大资源类型或 contract 范围",
                "不得修改生产代码、runner、schema、ledger 或调度器",
                "完成后立即填写现状报告并结束当前会话",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    authorized = guard.authorize_steer(lease_path, correction)
    assert len(authorized["authorizedSteers"]) == 1
    assert guard.authorize_steer(lease_path, correction)["authorizedSteers"] == authorized["authorizedSteers"]

    correction.write_text(
        correction.read_text(encoding="utf-8") + "再处理 ALIYUN::TEST::Resource0121\n",
        encoding="utf-8",
    )
    with pytest.raises(guard.GuardError, match="expands the planned resource scope"):
        guard.authorize_steer(lease_path, correction)


def test_legacy_bound_lease_can_deliver_once_but_cannot_be_newly_bound(tmp_path: Path) -> None:
    guard = _guard_module()
    lease_path = tmp_path / "leases" / "016-legacy.json"
    lease_path.parent.mkdir()
    legacy_turn = "44444444-4444-4444-8444-444444444444"
    lease_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "state": "bound",
                "taskId": "011",
                "resourceTypes": _resources("011"),
                "turnId": legacy_turn,
            }
        ),
        encoding="utf-8",
    )
    report = _report(tmp_path / "legacy-report.md", thread_id=guard.LEGACY_FIXED_THREAD_ID)

    delivered = guard.receive_report(
        lease_path,
        guard.LEGACY_FIXED_THREAD_ID,
        legacy_turn,
        report,
    )

    assert delivered["state"] == "delivered"
    assert delivered["workerThreadId"] == guard.LEGACY_FIXED_THREAD_ID


def _backfill_inputs(tmp_path: Path, *, task_id: str = "036") -> tuple[Path, Path, Path]:
    backfill_dir = tmp_path / "backfill-tasks"
    backfill_dir.mkdir()
    resource = _resources(task_id)[0]
    output_dir = f"evidence/formal/task-batches/{task_id}/backfill-unit-test/"
    (backfill_dir / f"{task_id}_资源链接验收.md").write_text(
        "\n".join(
            [
                f"# {task_id} 历史补测",
                f"- 输出目录：`{output_dir}`",
                "- 资源类型数：1",
                "- 本任务唯一 contract 数：1",
                f"### 1. `{resource}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    prompt = _prompt(tmp_path / "backfill-prompt.txt", task_id)
    original_prompt = prompt.read_text(encoding="utf-8")
    for other_resource in _resources(task_id)[1:]:
        original_prompt = original_prompt.replace(f"{other_resource}\n", "")
    original_prompt = original_prompt.replace(
        f"OUTPUT_DIR: evidence/formal/task-batches/{task_id}/",
        f"OUTPUT_DIR: {output_dir}",
    )
    prompt.write_text(original_prompt, encoding="utf-8")
    report = tmp_path / "backfill-report.md"
    report.write_text(
        "\n".join(
            [
                "# 资源链接批次报告",
                f"TASK_ID: `{task_id}`",
                f"QWEN_THREAD_ID: `{THREAD_ID}`",
                "CONCLUSION: `PASS`",
                f"RAW_OUTPUT_DIR: `{output_dir}`",
                "## 测试场景",
                f"- [x] `{resource}` | `contract-1` | iframe | PASS | `sample` | `landing`",
                f"- [x] `{resource}` | `contract-1` | open | PASS | `sample` | `landing`",
                "## 汇总",
                "- PASS: `2`",
                "- FAIL: `0`",
                "- SKIP: `0`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return backfill_dir, prompt, report


def test_historical_backfill_reuses_unique_lease_without_advancing_queue(tmp_path: Path) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path, accepted=40)
    backfill_dir, prompt, report = _backfill_inputs(tmp_path)
    before = guard.load_queue(task_dir, summary)

    lease_path, lease = guard.prepare_backfill_lease(
        task_id="036",
        prompt_path=prompt,
        backfill_task_dir=backfill_dir,
        lease_dir=lease_dir,
        task_dir=task_dir,
        summary_path=summary,
    )
    assert lease["mode"] == "historical_backfill"
    assert guard._queue_payload(before, lease_dir)["activeLeases"] == [str(lease_path)]
    with pytest.raises(guard.GuardError, match="another dispatch lease"):
        guard.prepare_lease(
            task_id="041",
            prompt_path=_prompt(tmp_path / "main-prompt.txt", "041"),
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )
    guard.bind_lease(lease_path, THREAD_ID, TURN_ID, MODEL, EFFORT)
    guard.receive_report(lease_path, THREAD_ID, TURN_ID, report)
    closed = guard.close_lease(lease_path, decision="backfill_verified", task_dir=task_dir, summary_path=summary)
    assert closed["state"] == "completed"
    after = guard.load_queue(task_dir, summary)
    assert (after.accepted, after.first_pending, after.digest) == (
        before.accepted,
        before.first_pending,
        before.digest,
    )


def test_historical_backfill_rejects_unapproved_or_unexecuted_work(tmp_path: Path) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path, accepted=40)
    backfill_dir, prompt, report = _backfill_inputs(tmp_path)
    with pytest.raises(guard.GuardError, match="authorized historical backfill"):
        guard.prepare_backfill_lease(
            task_id="035",
            prompt_path=prompt,
            backfill_task_dir=backfill_dir,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )
    lease_path, _lease = guard.prepare_backfill_lease(
        task_id="036",
        prompt_path=prompt,
        backfill_task_dir=backfill_dir,
        lease_dir=lease_dir,
        task_dir=task_dir,
        summary_path=summary,
    )
    guard.bind_lease(lease_path, THREAD_ID, TURN_ID, MODEL, EFFORT)
    report.write_text(
        report.read_text(encoding="utf-8")
        .replace("CONCLUSION: `PASS`", "CONCLUSION: `PARTIAL`")
        .replace("| open | PASS", "| open | SKIP")
        .replace("- PASS: `2`", "- PASS: `1`")
        .replace("- SKIP: `0`", "- SKIP: `1`"),
        encoding="utf-8",
    )
    # One normal PASS and one unexecuted Shift must remain retryable, not verified.
    report.write_text(
        report.read_text(encoding="utf-8").replace(
            "- [x] `ALIYUN::TEST::Resource0361` | `contract-1` | open | SKIP",
            "- [ ] `ALIYUN::TEST::Resource0361` | `contract-1` | open | SKIP",
        ),
        encoding="utf-8",
    )
    guard.receive_report(lease_path, THREAD_ID, TURN_ID, report)
    with pytest.raises(guard.GuardError, match="unexecuted scenarios"):
        guard.close_lease(lease_path, decision="backfill_verified", task_dir=task_dir, summary_path=summary)


def test_main_queue_cannot_pass_incomplete_historical_backfill_gate(tmp_path: Path) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path, accepted=54)
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "mainlineId": guard.MAINLINE_ID,
                "beforeTaskId": "055",
                "complete": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(guard.GuardError, match="historical backfill must be verified"):
        guard.prepare_lease(
            task_id="055",
            prompt_path=_prompt(tmp_path / "pending-prompt.txt", "055"),
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
            backfill_gate_path=gate_path,
        )
    assert not lease_dir.exists()


def _empty_route_plan(task_dir: Path, task_id: str = "011") -> tuple[Path, dict[str, object]]:
    output = task_dir.parent / "evidence" / "formal" / "task-batches" / task_id
    output.mkdir(parents=True)
    plan = {
        "schemaVersion": 1,
        "generatedAt": "2026-09-29T00:00:00Z",
        "catalogRevisionDigest": "catalog-digest",
        "site": "aliyun",
        "source": "route_catalog",
        "batchSize": 0,
        "resume": False,
        "skippedBlocked": 0,
        "skippedPending": 0,
        "skippedUnreplayable": 5,
        "items": [],
        "planId": "plan-id",
    }
    path = output / "plan-route-audit.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path, plan


def test_audit_unreplayable_requires_fresh_exact_empty_plan_and_partial_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path)
    plan_path, plan = _empty_route_plan(task_dir)
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard, "_regenerate_route_plan", lambda _spec: dict(plan))

    lease_path, lease = guard.audit_unreplayable_lease(
        task_id="011",
        plan_path=plan_path,
        lease_dir=lease_dir,
        task_dir=task_dir,
        summary_path=summary,
    )
    assert lease["state"] == "audited_unreplayable"
    assert lease["workerThreadId"] is None
    assert guard._queue_payload(guard.load_queue(task_dir, summary), lease_dir)["activeLeases"] == [str(lease_path)]
    with pytest.raises(guard.GuardError, match="recorded as accepted_partial"):
        guard.close_lease(lease_path, decision="accepted_partial", task_dir=task_dir, summary_path=summary)
    summary.write_text(
        summary.read_text(encoding="utf-8").replace(
            "| [011](./011_资源链接验收.md) | types | 5 | out | pending | turn |",
            "| [011](./011_资源链接验收.md) | types | 5 | out | accepted（partial） | turn |",
        ),
        encoding="utf-8",
    )
    with pytest.raises(guard.GuardError, match="only close as accepted_partial"):
        guard.close_lease(lease_path, decision="accepted", task_dir=task_dir, summary_path=summary)
    closed = guard.close_lease(lease_path, decision="accepted_partial", task_dir=task_dir, summary_path=summary)
    assert closed["state"] == "completed"
    assert guard.load_queue(task_dir, summary).first_pending == "012"


def test_audit_unreplayable_rejects_nonmatching_or_partial_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path)
    plan_path, plan = _empty_route_plan(task_dir)
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard, "_regenerate_route_plan", lambda _spec: dict(plan))
    with pytest.raises(guard.GuardError, match="first pending"):
        guard.audit_unreplayable_lease(
            task_id="012",
            plan_path=plan_path,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )
    plan["skippedUnreplayable"] = 4
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(guard.GuardError, match="wholly unreplayable"):
        guard.audit_unreplayable_lease(
            task_id="011",
            plan_path=plan_path,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )
    plan["skippedUnreplayable"] = 5
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(guard, "_regenerate_route_plan", lambda _spec: {**plan, "catalogRevisionDigest": "changed"})
    with pytest.raises(guard.GuardError, match="disagrees"):
        guard.audit_unreplayable_lease(
            task_id="011",
            plan_path=plan_path,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
        )
    assert not lease_dir.exists()


def test_audit_unreplayable_honors_historical_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _guard_module()
    task_dir, summary, lease_dir = _queue(tmp_path, accepted=54)
    plan_path, plan = _empty_route_plan(task_dir, "055")
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard, "_regenerate_route_plan", lambda _spec: dict(plan))
    gate = tmp_path / "gate.json"
    gate.write_text(
        json.dumps({"schemaVersion": 1, "mainlineId": guard.MAINLINE_ID, "beforeTaskId": "055", "complete": False}),
        encoding="utf-8",
    )
    with pytest.raises(guard.GuardError, match="historical backfill must be verified"):
        guard.audit_unreplayable_lease(
            task_id="055",
            plan_path=plan_path,
            lease_dir=lease_dir,
            task_dir=task_dir,
            summary_path=summary,
            backfill_gate_path=gate,
        )
    assert not lease_dir.exists()
