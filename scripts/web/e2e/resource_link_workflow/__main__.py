"""Repository entrypoint: uv run python -m scripts.web.e2e.resource_link_workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import worker_hosts
from .files import WorkflowError, contained, digest, json_write, read_json
from .runtime import Runtime
from .server import reconcile, serve
from .store import Store


def call(root: Path, operation: str, arguments: dict[str, Any] | None = None) -> Any:
    descriptor = read_json(root / "server.json")
    request = urllib.request.Request(
        descriptor["origin"] + "/control",
        method="POST",
        data=json.dumps({"operation": operation, "arguments": arguments or {}}).encode(),
        headers={"Authorization": "Bearer " + descriptor["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def ensure(root: Path, port: int) -> None:
    try:
        call(root, "status")
        return
    except urllib.error.HTTPError as error:
        # An HTTP rejection proves that a listener responded. It is not a
        # connection failure and must never cause a second Server launch.
        raise WorkflowError(
            "Existing Server rejected status; preserve the same instance and follow OPS guide"
        ) from error
    except (OSError, ValueError):
        pass
    Store(root).check()  # Missing/corrupt state is never silently replaced.
    with (root / "server.log").open("ab") as log:
        os.chmod(root / "server.log", 0o600)
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.web.e2e.resource_link_workflow",
                "--state",
                str(root),
                "serve",
                "--port",
                str(port),
            ],
            cwd=Path(__file__).resolve().parents[4],
            stdout=log,
            stderr=log,
            start_new_session=os.name != "nt",
        )
    # Startup-only bounded wait, not model-turn polling.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            call(root, "status")
            return
        except urllib.error.HTTPError as error:
            raise WorkflowError(
                "Existing Server rejected status during startup; do not launch another instance"
            ) from error
        except (OSError, ValueError):
            time.sleep(0.2)
    raise WorkflowError("Server startup failed; preserve state and follow OPS guide")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Reusable, program-owned resource-link workflow")
    result.add_argument("--state", type=Path, default=Path("evidence/resource-link-workflow"))
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--slots", type=int, default=4)
    init.add_argument("--base-port", type=int, default=16100)
    server = commands.add_parser("serve")
    server.add_argument("--port", type=int, default=16099)
    for name in ("status", "next", "pause", "resume", "check", "claim"):
        commands.add_parser(name)
    hold = commands.add_parser("review-only")
    hold.add_argument("--reason", required=True)
    commands.add_parser("dispatch-enable")
    slot_recover = commands.add_parser("slot-recover")
    slot_recover.add_argument("slot", type=int)
    execute = commands.add_parser("execute")
    execute.add_argument("action_id")
    launch = commands.add_parser("launch")
    launch.add_argument("job_id")
    configure = commands.add_parser("configure")
    configure.add_argument("file", type=Path)
    provision = commands.add_parser("provision")
    provision.add_argument("--from-config", type=Path, required=True)
    cloud_source = commands.add_parser("cloud-source-adopt")
    cloud_source.add_argument("--cli-config", type=Path, required=True)
    cloud_source.add_argument("--account", required=True)
    cloud_source.add_argument("--region", required=True)
    authorization = commands.add_parser("authorize-creation")
    authorization.add_argument("--proposal", type=Path, required=True)
    authorization.add_argument("--evidence", required=True)
    authorization.add_argument("--max-budget-cents", type=int, required=True)
    budget = commands.add_parser("amend-budget")
    budget.add_argument("plan_id")
    budget.add_argument("--budget-cents", type=int, required=True)
    budget.add_argument("--evidence", required=True)
    hourly = commands.add_parser("register-hourly-fee")
    hourly.add_argument("plan_id")
    hourly.add_argument("--evidence", required=True)
    samples = commands.add_parser("authorize-temporary-samples")
    samples.add_argument("task_id")
    samples.add_argument("--evidence", required=True)
    cleanup_review = commands.add_parser("inspect-cleanup")
    cleanup_review.add_argument("job_id")
    cleanup_review.add_argument("--dimension", choices=("ROS", "TF", "CREATE"), required=True)
    navigation = commands.add_parser("queue-navigation")
    navigation.add_argument("job_id")
    navigation.add_argument("--evidence", required=True)
    cleanup_identity = commands.add_parser("authorize-cleanup-identity")
    cleanup_identity.add_argument("job_id")
    cleanup_identity.add_argument("--dimension", choices=("ROS", "TF", "CREATE"), required=True)
    cleanup_identity.add_argument("--evidence", required=True)
    cleanup_queue = commands.add_parser("queue-cleanup")
    cleanup_queue.add_argument("job_id")
    cleanup_queue.add_argument("--dimension", choices=("ROS", "TF", "CREATE"), required=True)
    cleanup_queue.add_argument("--evidence", required=True)
    cleanup_queue.add_argument("--retry-job", help="Exact terminal cleanup job; fresh OPS review, no quota refill")
    cleanup_queue.add_argument(
        "--read-only", action="store_true", help="Original deletion succeeded; remaining reads only",
    )
    commands.add_parser("chrome-start")
    worker_host = commands.add_parser("worker-host-select")
    worker_host.add_argument("host", choices=("local", "macmini"))
    commands.add_parser("macmini-link-start")
    commands.add_parser("macmini-link-stop")
    commands.add_parser("sol-backend-start")
    commands.add_parser("server-stop")
    restore = commands.add_parser("restore-active-repair")
    restore.add_argument("job_id")
    restore.add_argument("--evidence", required=True)
    adopt = commands.add_parser("chrome-adopt")
    adopt.add_argument("--pid", type=int, required=True)
    adopt.add_argument("--profile", type=Path, required=True)
    load = commands.add_parser("import-plan")
    load.add_argument("file", type=Path)
    load.add_argument("--budget-cents", type=int, default=0)
    load.add_argument("--successor-review", help="Independent OPS paused/drained read-only plan handoff evidence")
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    recovery = commands.add_parser("reconcile")
    recovery.add_argument("job_id")
    recovery.add_argument("--raw", type=Path, required=True)
    retry = commands.add_parser("retry-blocked")
    retry.add_argument("task_id")
    retry.add_argument("--reason", required=True)
    preparation_repair = commands.add_parser("queue-preparation-repair")
    preparation_repair.add_argument("task_id")
    preparation_repair.add_argument("--evidence", required=True, help="JSON path relative to state/inputs")
    preparation_repair.add_argument("--reason", required=True)
    preserved_repair = commands.add_parser("queue-preserved-repair")
    preserved_repair.add_argument("task_id")
    preserved_repair.add_argument("--evidence", required=True, help="OPS product gap JSON relative to state/inputs")
    preserved_repair.add_argument("--reason", required=True)
    native_inspect = commands.add_parser("inspect-native-continuation")
    existing_inspect = commands.add_parser("inspect-existing-query")
    existing_inspect.add_argument("task_id")
    existing_inspect.add_argument("--handoff", required=True)
    existing_inspect.add_argument("--output", required=True)
    commands.add_parser("queue-existing-query").add_argument("--evidence", required=True)
    capture = commands.add_parser("inspect-existing-query-capture")
    capture.add_argument("task_id")
    capture.add_argument("--diagnosis", required=True)
    capture.add_argument("--output", required=True)
    commands.add_parser("queue-existing-query-capture").add_argument("--evidence", required=True)
    native_inspect.add_argument("job_id")
    native_inspect.add_argument("--repair", required=True, help="Completed repair JSON relative to inputs")
    native_inspect.add_argument("--output", type=Path, required=True)
    native_queue = commands.add_parser("queue-native-continuation")
    native_queue.add_argument("job_id")
    native_queue.add_argument("--evidence", required=True, help="Independent OPS review JSON relative to inputs")
    managed_inspect = commands.add_parser("inspect-managed-continuation")
    managed_inspect.add_argument("job_id")
    managed_inspect.add_argument("--output", required=True)
    managed_inspect.add_argument("--renew-identity", action="store_true")
    managed_inspect.add_argument("--capture-only", action="store_true")
    managed_queue = commands.add_parser("queue-managed-continuation")
    managed_queue.add_argument("job_id")
    managed_queue.add_argument("--evidence", required=True)
    retained_inspect = commands.add_parser("inspect-retained-capture")
    retained_inspect.add_argument("job_id")
    retained_inspect.add_argument("--output", required=True)
    retained_queue = commands.add_parser("queue-retained-capture")
    retained_queue.add_argument("job_id")
    retained_queue.add_argument("--evidence", required=True)
    native_retry_inspect = commands.add_parser("inspect-native-retry")
    native_retry_inspect.add_argument("job_id")
    native_retry_inspect.add_argument("--repair", required=True)
    native_retry_inspect.add_argument("--output", required=True)
    native_retry_queue = commands.add_parser("queue-native-retry")
    native_retry_queue.add_argument("job_id")
    native_retry_queue.add_argument("--evidence", required=True)
    native_cleanup_inspect = commands.add_parser("inspect-native-cleanup")
    native_cleanup_inspect.add_argument("job_id")
    native_cleanup_inspect.add_argument("--screenshot-review", required=True)
    native_cleanup_inspect.add_argument("--output", required=True)
    native_cleanup_queue = commands.add_parser("queue-native-cleanup")
    native_cleanup_queue.add_argument("job_id")
    native_cleanup_queue.add_argument("--evidence", required=True)
    commands.add_parser("inspect-native-identity-recovery").add_argument(
        "--output", required=True, help="New OPS proposal path relative to inputs",
    )
    commands.add_parser("execute-native-identity-recovery").add_argument(
        "--evidence", required=True, help="Approved OPS review path relative to inputs",
    )
    input_repair = commands.add_parser("queue-input-repair")
    input_repair.add_argument("task_id")
    input_repair.add_argument("--evidence", required=True, help="Diagnostic-only JSON relative to state/inputs")
    input_repair.add_argument("--reason", required=True)
    rereview = commands.add_parser("reopen-review")
    rereview.add_argument("task_id")
    rereview.add_argument("--reason", required=True)
    rereview.add_argument("--proof", type=Path, help="Optional original cleanup proof for already-deleted owners only")
    pending_delete = commands.add_parser("accept-pending-delete")
    pending_delete.add_argument("task_id")
    pending_delete.add_argument("--evidence", required=True, help="Explicit human disposition JSON relative to inputs")
    recovery_task = commands.add_parser("recover-blocked")
    recovery_task.add_argument("task_id")
    recovery_task.add_argument("--proof", type=Path, required=True)
    recovery_task.add_argument("--reason", required=True)
    rejected = commands.add_parser("reconcile-rejected-creation")
    rejected.add_argument("task_id")
    rejected.add_argument("--evidence", required=True, help="Independent OPS JSON path relative to state/inputs")
    for name in ("web-start", "web-stop", "web-attest", "web-restart", "bootstrap"):
        op = commands.add_parser(name)
        op.add_argument("slot", type=int)
    plan = commands.add_parser("plan")
    plan.add_argument("--catalog", type=Path, default=Path("scripts/web/e2e/resource_link_workflow/catalog.json"))
    plan.add_argument("--id", required=True)
    plan.add_argument("--resources", nargs="+", required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--initial-wave", type=int, default=1)
    plan.add_argument("--retest-scenes", nargs="+",
                      help="Exact failed QUERY/ROS navigation scenes on existing resources")
    reviewed = commands.add_parser("plan-from-review")
    reviewed.add_argument("--catalog", type=Path, default=Path("scripts/web/e2e/resource_link_workflow/catalog.json"))
    reviewed.add_argument("--review", type=Path, required=True)
    reviewed.add_argument("--id", required=True)
    reviewed.add_argument("--output", type=Path, required=True)
    addition = commands.add_parser("catalog-add")
    addition.add_argument("--catalog", type=Path, default=Path("scripts/web/e2e/resource_link_workflow/catalog.json"))
    addition.add_argument("--spec", type=Path, required=True)
    commands.add_parser("matrix")
    report = commands.add_parser("report")
    report.add_argument(
        "--output", type=Path, default=Path("scripts/web/e2e/resource_link_workflow/resource-link-results.html")
    )
    return result


def main() -> None:
    args = parser().parse_args()
    root = args.state.resolve()
    if args.command == "init":
        Store.initialize(root, slots=args.slots, base_port=args.base_port)
        result = {"initialized": str(root), "paused": True}
    elif args.command == "serve":
        serve(root, args.port)
        return
    elif args.command == "plan":
        from .reports import make_plan

        make_plan(
            args.catalog,
            args.id,
            args.resources,
            args.output,
            Store(root) if (root / "state.sqlite3").exists() else None,
            initial_wave=args.initial_wave,
            retest_scenes=args.retest_scenes,
        )
        result = {"plan": str(args.output)}
    elif args.command == "plan-from-review":
        from .reports import make_review_plan

        make_review_plan(args.catalog, args.review, args.id, args.output, Store(root))
        result = {"plan": str(args.output)}
    elif args.command == "catalog-add":
        from .reports import add_resource

        result = {"resource_id": add_resource(args.catalog, read_json(args.spec))}
    elif args.command == "matrix":
        from .reports import render_matrix

        render_matrix(
            Path("scripts/web/e2e/resource_link_workflow/catalog.json"),
            Path("scripts/web/e2e/resource_link_workflow/resource-link-test-matrix.html"),
        )
        result = {"matrix": "scripts/web/e2e/resource_link_workflow/resource-link-test-matrix.html"}
    elif args.command == "report":
        from .reports import render_results

        render_results(Store(root), args.output)
        result = {"report": str(args.output)}
    elif args.command == "configure":
        config = read_json(args.file)
        worker_hosts.host(config)
        if worker_hosts.host(config) == "macmini":
            worker_hosts.mini(config)
        if not Path(config["repo"]).is_dir():
            raise WorkflowError("repo path does not exist")
        old_file = root / "environment.json"
        if old_file.exists():
            old = read_json(old_file)
            if worker_hosts.host(config) != worker_hosts.host(old):
                raise WorkflowError("use worker-host-select to preserve each host's exact browser targets")
            protected = ("macmini", "bailian_endpoint", "sol_endpoint", "cdp_endpoint", "repo")
            if any(config.get(key) != old.get(key) for key in protected):
                runtime = Runtime(Store(root))
                worker_hosts.require_idle(runtime)
                with runtime.store.transaction() as connection:
                    bound = connection.execute("SELECT 1 FROM jobs WHERE thread_id IS NOT NULL").fetchone()
                if bound and any(config.get(key) != old.get(key) for key in ("bailian_endpoint", "sol_endpoint")):
                    raise WorkflowError("preserve original model endpoints for bound thread history")
        json_write(root / "environment.json", config, private=True)
        result = {"configured": True, "restart_server_to_load": True}
    elif args.command == "claim":
        runtime = Runtime(Store(root))
        repo = Path(runtime.config["repo"]).resolve()
        from scripts.web.e2e.qwen_batch_guard import _active_leases

        from .runtime import exclusive

        with exclusive(repo / "evidence" / "resource-link-workflow-owner.lock"):
            # Historical queue documents are not a runtime dependency. Preserve
            # the actual old/new dispatch interlock by checking durable leases.
            if _active_leases(repo / "evidence/formal/supervision/qwen-batch-leases"):
                raise WorkflowError("legacy guard has active leases; no handoff")
            marker = repo / "evidence" / "resource-link-workflow-owner.json"
            if marker.exists() and read_json(marker)["state"] != str(root):
                raise WorkflowError("another resource workflow owns this repository")
            json_write(marker, {"schema_version": 1, "state": str(root)}, private=True)
        result = {"dispatch_owner": "resource-workflow", "legacy_dispatch_disabled": True}
    elif args.command == "provision":
        result = Runtime(Store(root)).provision(args.from_config)
    elif args.command == "cloud-source-adopt":
        from .credentials import adopt_oauth

        result = adopt_oauth(Store(root), args.cli_config, args.account, args.region)
    elif args.command == "authorize-creation":
        result = Runtime(Store(root)).authorize_creation(args.proposal, args.evidence, args.max_budget_cents)
    elif args.command == "amend-budget":
        result = Store(root).amend_budget(args.plan_id, args.budget_cents, args.evidence)
    elif args.command == "register-hourly-fee":
        result = Store(root).register_hourly_fee(args.plan_id, args.evidence)
    elif args.command == "authorize-temporary-samples":
        result = Store(root).authorize_temporary_samples(args.task_id, args.evidence)
    elif args.command == "chrome-start":
        result = Runtime(Store(root)).start_chrome()
    elif args.command == "worker-host-select":
        from .runtime import exclusive

        with exclusive(root / "worker-host-ops.lock"):
            result = worker_hosts.select_host(Runtime(Store(root)), args.host)
    elif args.command in {"macmini-link-start", "macmini-link-stop"}:
        from .runtime import exclusive

        with exclusive(root / "worker-host-ops.lock"):
            runtime = Runtime(Store(root))
            result = (worker_hosts.start_link(runtime) if args.command == "macmini-link-start" else
                      worker_hosts.stop_link(runtime))
    elif args.command == "sol-backend-start":
        result = Runtime(Store(root)).start_sol_backend()
    elif args.command == "chrome-adopt":
        result = Runtime(Store(root)).adopt_chrome(args.pid, args.profile)
    elif args.command == "restore-active-repair":
        from .active_restore import restore

        result = restore(Runtime(Store(root)), args.job_id, args.evidence)
    elif args.command == "server-stop":
        import signal

        descriptor = read_json(root / "server.json")
        Runtime(Store(root)).assert_process(descriptor["pid"], descriptor["identity"])
        os.kill(descriptor["pid"], signal.SIGTERM)
        result = {"stopped_server": descriptor["pid"], "durable_state_preserved": True}
    elif args.command == "check":
        Store(root).check()
        result = {"integrity": "ok"}
    elif args.command == "queue-cleanup":
        result = Runtime(Store(root)).queue_cleanup(
            args.job_id, args.dimension, args.evidence, args.retry_job, read_only=args.read_only,
        )
    elif args.command == "inspect-native-identity-recovery":
        result = Runtime(Store(root)).inspect_native_identity_recovery(args.output)
    elif args.command == "execute-native-identity-recovery":
        result = Runtime(Store(root)).execute_native_identity_recovery(args.evidence)
    elif args.command == "inspect-cleanup":
        result = Runtime(Store(root)).inspect_cleanup(args.job_id, args.dimension)
    elif args.command == "queue-navigation":
        result = Runtime(Store(root)).queue_navigation(args.job_id, args.evidence)
    elif args.command == "authorize-cleanup-identity":
        result = Runtime(Store(root)).authorize_cleanup_identity(args.job_id, args.dimension, args.evidence)
    elif args.command == "backup":
        Store(root).backup(args.destination)
        result = {"backup": str(args.destination)}
    elif args.command == "reconcile":
        result = reconcile(Store(root), args.job_id, read_json(args.raw))
    elif args.command == "reopen-review":
        Store(root).reopen_review(args.task_id, args.reason, read_json(args.proof) if args.proof else None)
        result = {"review_reopened": args.task_id}
    elif args.command == "accept-pending-delete":
        result = Store(root).accept_pending_delete(args.task_id, args.evidence)
    elif args.command == "retry-blocked":
        Store(root).retry_blocked(args.task_id, args.reason)
        result = {"retry_queued": args.task_id}
    elif args.command == "queue-preparation-repair":
        result = Runtime(Store(root)).queue_preparation_repair(args.task_id, args.evidence, args.reason)
    elif args.command == "queue-preserved-repair":
        result = Runtime(Store(root)).queue_preserved_repair(args.task_id, args.evidence, args.reason)
    elif args.command == "inspect-existing-query":
        from . import existing_query

        output = contained(root / "inputs", args.output)
        if output.exists():
            raise WorkflowError("existing query proposal is immutable")
        proposal = existing_query.propose(Runtime(Store(root)), args.task_id, args.handoff)
        json_write(output, proposal, private=True)
        result = {"proposal": output.relative_to(root).as_posix(), "sha256": digest(output), "cloud_calls": 0}
    elif args.command == "queue-existing-query":
        from . import existing_query

        result = existing_query.queue(Runtime(Store(root)), args.evidence)
    elif args.command == "inspect-existing-query-capture":
        from . import existing_query

        output = contained(root / "inputs", args.output)
        if output.exists():
            raise WorkflowError("capture proposal is immutable")
        proposal = existing_query.propose_capture(Runtime(Store(root)), args.task_id, args.diagnosis)
        json_write(output, proposal, private=True)
        result = {"proposal": output.relative_to(root).as_posix(), "sha256": digest(output), "cloud_calls": 0}
    elif args.command == "queue-existing-query-capture":
        from . import existing_query

        result = existing_query.queue_capture(Runtime(Store(root)), args.evidence)
    elif args.command == "inspect-native-continuation":
        from . import native_continuation

        output = contained(root, str(args.output))
        if output.exists():
            raise WorkflowError("native continuation proposal is immutable")
        proposal = native_continuation.inspect(Runtime(Store(root)), args.job_id, args.repair)
        json_write(output, proposal, private=True)
        result = {"proposal": output.relative_to(root).as_posix(), "approved": False, "cloud_calls": 0}
    elif args.command == "queue-native-continuation":
        from . import native_continuation

        result = native_continuation.queue(Runtime(Store(root)), args.job_id, args.evidence)
    elif args.command == "inspect-managed-continuation":
        from . import native_continuation

        output = contained(root / "inputs", args.output)
        if output.exists():
            raise WorkflowError("managed continuation proposal is immutable")
        proposal = native_continuation.inspect_managed(Runtime(Store(root)), args.job_id,
                                                       renew_identity=args.renew_identity,
                                                       capture_only=args.capture_only)
        json_write(output, proposal, private=True)
        result = {"proposal": output.relative_to(root).as_posix(), "approved": False, "cloud_calls": 0}
    elif args.command == "queue-managed-continuation":
        from . import native_continuation

        result = native_continuation.queue_managed(Runtime(Store(root)), args.job_id, args.evidence)
    elif args.command == "inspect-retained-capture":
        from . import retained_capture

        output = contained(root / "inputs", args.output)
        if output.exists():
            raise WorkflowError("retained capture proposal is immutable")
        json_write(output, retained_capture.inspect(Runtime(Store(root)), args.job_id), private=True)
        result = {"proposal": output.relative_to(root).as_posix(), "cloud_calls": 0, "approved": False}
    elif args.command == "queue-retained-capture":
        from . import retained_capture

        result = retained_capture.queue(Runtime(Store(root)), args.job_id, args.evidence)
    elif args.command == "inspect-native-retry":
        from . import native_retry

        output = contained(root / "inputs", args.output)
        if output.exists():
            raise WorkflowError("native retry proposal is immutable")
        proposal = native_retry.inspect(Runtime(Store(root)), args.job_id, args.repair)
        json_write(output, proposal, private=True)
        result = {"proposal": output.relative_to(root).as_posix(), "approved": False, "cloud_calls": 0}
    elif args.command == "queue-native-retry":
        from . import native_retry

        result = native_retry.queue(Runtime(Store(root)), args.job_id, args.evidence)
    elif args.command == "inspect-native-cleanup":
        from . import native_retry

        output = contained(root / "inputs", args.output)
        if output.exists():
            raise WorkflowError("native cleanup proposal is immutable")
        proposal = native_retry.inspect_cleanup(Runtime(Store(root)), args.job_id, args.screenshot_review)
        json_write(output, proposal, private=True)
        result = {"proposal": output.relative_to(root).as_posix(), "sha256": digest(output), "cloud_calls": 0}
    elif args.command == "queue-native-cleanup":
        from . import native_retry

        result = native_retry.queue_cleanup(Runtime(Store(root)), args.job_id, args.evidence)
    elif args.command == "queue-input-repair":
        Store(root).queue_input_repair(args.task_id, args.evidence, args.reason)
        result = {"input_repair_queued": args.task_id, "no_glm_dispatched": True, "pass_count_unchanged": True}
    elif args.command == "recover-blocked":
        Store(root).recover_blocked(args.task_id, read_json(args.proof), args.reason)
        result = {"ops_recovered": args.task_id, "pass_count_unchanged": True}
    elif args.command == "reconcile-rejected-creation":
        result = Store(root).reconcile_rejected_creation(args.task_id, args.evidence)
    elif args.command in {"web-start", "web-stop", "web-attest", "web-restart", "bootstrap"}:
        runtime = Runtime(Store(root))
        result = (
            getattr(runtime, args.command.replace("-", "_"))(args.slot)
            if args.command == "bootstrap"
            else (runtime.restart_web(args.slot) if args.command == "web-restart" else
                  runtime.attest_web(args.slot) if args.command == "web-attest" else
                  runtime.start_web(args.slot) if args.command == "web-start" else runtime.stop_web(args.slot))
        )
    else:
        ensure(root, 16099)
        command = args.command
        arguments = {}
        if command == "import-plan":
            command, arguments = "import", {"plan": read_json(args.file), "budget_cents": args.budget_cents,
                                           "successor_review": args.successor_review}
        elif command in {"pause", "resume"}:
            command, arguments = "pause", {"paused": command == "pause"}
        elif command == "review-only":
            arguments = {"reason": args.reason}
        elif command == "execute":
            arguments = {"action_id": args.action_id}
        elif command == "launch":
            arguments = {"job_id": args.job_id}
        elif command == "slot-recover":
            arguments = {"slot": args.slot}
        result = call(root, command, arguments)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError, KeyError) as error:
        if isinstance(error, urllib.error.HTTPError):
            message = "Server rejected operation; inspect status and OPS guide"
        else:
            message = str(error)
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
