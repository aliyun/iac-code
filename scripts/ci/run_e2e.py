#!/usr/bin/env python3
"""Run selected process E2E cases locally or in CI with bounded parallelism."""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_REFRESH_SECONDS = 600
SAFE_LIVE_AUDIT_NOTE = re.compile(
    r"credential audit: source=(?:llm|cloud); "
    r"location=(?:logs|artifacts|workspace|templates|other); "
    r"suffix=(?:json|jsonl|log|txt|yaml|yml|md|other)\Z"
)
TERMINAL_CATEGORIES = (
    ("task_busy", r"already working|already running|task is busy|任务.{0,8}(?:运行|处理中)"),
    ("rate_limit", r"rate.?limit|throttl|\b429\b|quota|限流|配额"),
    ("timeout", r"timed? out|timeout|deadline|超时"),
    ("authentication", r"unauthorized|invalid.{0,20}api.?key|\b401\b|认证失败|鉴权失败"),
    ("permission", r"forbidden|permission denied|\b403\b|权限不足"),
    ("model_unavailable", r"model.{0,30}not found|\b404\b|模型.{0,8}不存在"),
    ("network", r"connection|network|\b50[234]\b|网络错误|连接失败"),
    ("model_context", r"context length|max(?:imum)? tokens?|上下文长度"),
)
SAFE_TERMINAL_TERMS = (
    "task", "pipeline", "recovery", "restore", "backup", "session", "context", "identity",
    "credential", "provider", "model", "permission", "input", "state", "failed", "error",
    "unavailable", "missing", "invalid", "retry", "cancelled", "canceled", "concurrent",
    "mismatch", "resume", "step", "active", "checkpoint", "journal", "lock", "conflict",
    "runtime", "execution", "message", "transport", "stream", "closed", "delivery",
    "任务", "流水线", "恢复", "备份", "会话", "上下文", "身份", "凭证", "模型", "权限", "输入",
    "状态", "失败", "错误", "不可用", "不存在", "超时", "重试", "取消", "并发",
)
TERMINAL_FIXED_CODES = (
    "pipeline_identity_mismatch", "input_response_mismatch", "permission_resume_invalid",
    "resource_selection_resume_invalid", "cloud_execution_identity_changed", "state_commit_failed",
    "external_operation_commit_failed", "pipeline_transport_delivery_required",
)
RESULT_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "timeout": "超时",
    "canceled": "已取消",
    "not-started": "未开始",
}
CLEANUP_LABELS = {
    "completed": "已清理",
    "failed": "清理失败",
    "skipped": "已跳过",
    "not-needed": "无需清理",
    "unverified": "未验证",
}
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from iac_code.services.telemetry.identity import E2E_USER_ID_ENV, is_e2e_user_id  # noqa: E402
from scripts.a2a.e2e.execution_control.run_execution_control_scenarios import SCENARIO_MODES  # noqa: E402
from scripts.a2a.e2e.resource_selector.run_live_resource_selector import SCENARIOS as SELECTOR_SCENARIOS  # noqa: E402
from scripts.a2a.e2e.run_recovery_scenarios import _SCENARIOS as A2A_RECOVERY_SCENARIOS  # noqa: E402
from scripts.pipeline.e2e.selling_solution_first.run_scenarios import SCENARIOS as SELLING_SCENARIOS  # noqa: E402
from scripts.repl.e2e.run_pipeline_scenarios import _SCENARIOS as REPL_PIPELINE_SCENARIOS  # noqa: E402


@dataclass(frozen=True)
class Case:
    name: str
    script: str
    args: tuple[str, ...]
    timeout: int
    suite: str = "fast"
    cloud_write: bool = False
    cleanup_grace: int = 5
    result_source: str = "summary.json"
    live_runner: str = "selling"
    resource_lock: str = ""
    group: str = ""


class CloudCredentialSetupError(RuntimeError):
    """A credential helper failed without exposing its output in CI artifacts."""


FAST_CASES = (
    Case("a2a-recovery-contract", "scripts/a2a/e2e/run_contract_scenarios.py", ("--scenario", "e3a-recovery"), 480),
    Case("a2a-success-contract", "scripts/a2a/e2e/run_contract_scenarios.py", ("--scenario", "e3b-success"), 480),
    Case("a2a-cancel-contract", "scripts/a2a/e2e/run_contract_scenarios.py", ("--scenario", "e3b-cancel"), 480),
    Case("repl-normal-contract", "scripts/repl/e2e/run_contract_scenarios.py", (), 360),
    Case("repl-pipeline-contract", "scripts/repl/e2e/run_pipeline_contract_scenario.py", (), 360),
    # This checks the real Web HTTP/session path. Browser DOM acceptance needs Chrome and is kept manual.
    Case("web-api-contract", "scripts/web/e2e/run_contract_scenario.py", ("--skip-browser",), 360),
)
EXECUTION_CASES = tuple(
    Case(
        "execution-{}-{}".format(scenario, mode),
        "scripts/a2a/e2e/execution_control/run_execution_control_scenarios.py",
        ("--scenario", scenario, "--mode", mode, "--timeout", "20", "--overall-timeout", "60"),
        90,
        "full",
    )
    for scenario, modes in SCENARIO_MODES.items()
    for mode in modes
)
PERMISSION_SCRIPT = "scripts/a2a/e2e/permission_wait/run_permission_wait_restart.py"
PERMISSION_CASES = (
    Case(
        "permission-agui-generation-fence",
        "scripts/a2a/e2e/permission_wait/run_agui_generation_fence.py",
        ("--timeout", "25"), 120, "full", result_source="stdout",
    ),
    Case(
        "permission-staged-generation-fence", PERMISSION_SCRIPT,
        ("--decision", "allow_once", "--staged-backup-generation-fence", "--timeout", "20"),
        90, "full", result_source="stdout",
    ),
    Case(
        "permission-sub-pipeline-timeout",
        "scripts/a2a/e2e/permission_wait/run_sub_pipeline_permission_timeout.py",
        ("--timeout-seconds", "1"), 90, "full", result_source="result.json",
    ),
) + tuple(
    Case(
        "permission-{}-{}".format(mode, decision), PERMISSION_SCRIPT,
        ("--decision", decision, "--mode", mode, "--timeout", "20"),
        90, "full", result_source="stdout",
    )
    for mode in ("normal", "pipeline")
    for decision in ("allow_once", "deny")
) + (
    Case(
        "permission-pipeline-candidate-first", PERMISSION_SCRIPT,
        ("--decision", "allow_once", "--mode", "pipeline", "--candidate-first", "--timeout", "20"),
        90, "full", result_source="stdout",
    ),
) + tuple(
    Case(
        "permission-{}-{}".format(step, decision), PERMISSION_SCRIPT,
        ("--decision", decision, "--mode", "pipeline", "--pipeline-step-id", step, "--timeout", "20"),
        90, "full", result_source="stdout",
    )
    for step in ("solution_planning_and_selection", "materialize_selected_candidate", "deploying")
    for decision in ("allow_once", "deny")
) + tuple(
    Case(
        "permission-handoff-{}".format(decision), PERMISSION_SCRIPT,
        ("--decision", decision, "--mode", "pipeline", "--handoff-first", "--timeout", "20"),
        90, "full", result_source="stdout",
    )
    for decision in ("allow_once", "deny")
)
CASES = FAST_CASES + EXECUTION_CASES + PERMISSION_CASES
LIVE_SCRIPT = "scripts/pipeline/e2e/selling_solution_first/run_scenarios.py"
SELLING_CIDR_POOLS = tuple(str(pool) for pool in ipaddress.IPv4Network("10.250.0.0/16").subnets(new_prefix=22))
if len(SELLING_SCENARIOS) > len(SELLING_CIDR_POOLS):
    raise ValueError("selling E2E scenario count exceeds isolated CIDR pool count")


def _selling_group(spec: Any) -> str:
    for group in ("core", "recovery", "multimodal", "legacy", "safety"):
        if group in spec.suites:
            return group
    raise ValueError("unclassified selling E2E scenario: " + spec.name)


LIVE_CASES = tuple(
    Case(
        "ssf-" + spec.name, LIVE_SCRIPT,
        ("--scenario", spec.name, "--cidr-pool", SELLING_CIDR_POOLS[index]), 2700, "live",
        cloud_write=spec.cloud_write, cleanup_grace=900,
        resource_lock=spec.resource_lock, group=_selling_group(spec),
    )
    for index, spec in enumerate(SELLING_SCENARIOS)
    if spec.surface.value not in {"web", "desktop"}
) + tuple(
    Case(
        "selector-" + scenario, "scripts/a2a/e2e/resource_selector/run_live_resource_selector.py",
        ("--scenario", scenario), 1800, "live", cleanup_grace=60,
        live_runner="selector", group="readonly",
    )
    for scenario in SELECTOR_SCENARIOS
) + tuple(
    Case(
        "repl-pipeline-" + scenario, "scripts/repl/e2e/run_pipeline_scenarios.py",
        ("--scenario", scenario), 2700, "live", cloud_write=True,
        cleanup_grace=900, live_runner="repl", group="repl",
        resource_lock="rollback-stack-cleanup" if "cleanup" in scenario else "",
    )
    for scenario in REPL_PIPELINE_SCENARIOS
) + tuple(
    Case(
        "a2a-recovery-" + scenario, "scripts/a2a/e2e/run_recovery_scenarios.py",
        ("--scenario", scenario), 2700, "live", cleanup_grace=60,
        live_runner="legacy_a2a_readonly", group="readonly",
    )
    for scenario in ("redaction-step4", "iac-code-web-2c4g-step4")
) + tuple(
    Case(
        "a2a-recovery-" + scenario, "scripts/a2a/e2e/run_recovery_scenarios.py",
        ("--scenario", scenario, "--ci-teardown") +
        (("--deterministic",) if scenario == "fault-after-snapshot" else ()),
        2700, "live", cloud_write=True,
        cleanup_grace=900, live_runner="legacy_a2a", group="legacy",
    )
    for scenario in A2A_RECOVERY_SCENARIOS
    if scenario not in {"redaction-step4", "iac-code-web-2c4g-step4"}
) + (
    Case(
        "repl-aliyun-readonly-canary", "scripts/repl/e2e/run_real_aliyun_contract_canary.py",
        (), 900, "live", cleanup_grace=60, live_runner="canary", group="readonly",
    ),
) + (
    Case("smoke-a2a-vpc", "scripts/a2a/smoke/test_a2a_vpc.py", (), 800, "live", live_runner="smoke", group="smoke"),
    Case("smoke-acp-vpc", "scripts/acp/smoke/test_acp_vpc.py", (), 450, "live", live_runner="smoke", group="smoke"),
    Case(
        "smoke-headless-vpc", "scripts/headless/smoke/test_headless_vpc.py",
        (), 1050, "live", live_runner="smoke", group="smoke",
    ),
)
CASES += LIVE_CASES
EXCLUDED = (
    (
        "selling_solution_first Web and Desktop cases (W01, W02, D01)",
        "require provisioned Chrome or a native Desktop package and display host",
    ),
    ("StartChat permission wait", "depends on an external StartChat endpoint and mutable permission state"),
    ("Qoder MCP reconnect", "requires a Qoder installation and its local MCP state"),
    ("Web browser contract", "requires provisioned Chrome and playwright-core; API coverage is listed separately"),
)
LOCAL_TELEMETRY_SCRIPTS = frozenset({
    "scripts/a2a/e2e/run_contract_scenarios.py",
    "scripts/repl/e2e/run_contract_scenarios.py",
    "scripts/repl/e2e/run_pipeline_contract_scenario.py",
    "scripts/web/e2e/run_contract_scenario.py",
    "scripts/pipeline/e2e/selling_solution_first/run_scenarios.py",
    "scripts/repl/e2e/run_real_aliyun_contract_canary.py",
})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=(
            "fast", "full", "live", "live-core", "live-recovery", "live-multimodal",
            "live-readonly", "live-legacy", "live-safety", "live-repl", "live-smoke", "all",
        ),
        default="fast",
    )
    parser.add_argument("--case", action="append", choices=sorted(case.name for case in CASES))
    parser.add_argument("--jobs", type=int, default=3, help="Maximum simultaneously running cases")
    parser.add_argument("--run-dir", type=Path, default=REPO_ROOT / "ci-e2e-report")
    parser.add_argument("--credential-source-dir", type=Path)
    parser.add_argument("--cloud-credential-helper", type=Path)
    parser.add_argument("--cloud-credential-python", type=Path)
    parser.add_argument("--allow-cloud-write", action="store_true")
    parser.add_argument("--list", action="store_true", help="Show the allowlist and exclusions without running")
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.jobs > 8:
        parser.error("--jobs must be between 1 and 8")
    selected = select_cases(args)
    if not args.list and any(case.suite == "live" for case in selected):
        if args.credential_source_dir is None:
            parser.error("live cases require --credential-source-dir")
        if args.cloud_credential_helper is not None and not args.cloud_credential_helper.is_file():
            parser.error("--cloud-credential-helper must name an existing file")
        if args.cloud_credential_python is not None and not args.cloud_credential_python.is_file():
            parser.error("--cloud-credential-python must name an existing Python interpreter")
        if args.cloud_credential_python is not None and args.cloud_credential_helper is None:
            parser.error("--cloud-credential-python requires --cloud-credential-helper")
        needs_cloud = any(case.live_runner != "smoke" for case in selected)
        required_files = [".credentials.yml", "settings.yml"]
        if needs_cloud and args.cloud_credential_helper is None:
            required_files.append(".cloud-credentials.yml")
        missing = [
            name
            for name in required_files
            if not (args.credential_source_dir / name).is_file()
        ]
        if missing:
            parser.error("credential source directory lacks required files: " + ", ".join(missing))
        if any(case.cloud_write for case in selected) and not args.allow_cloud_write:
            parser.error("selected live cases create ROS resources; pass --allow-cloud-write")
    return args


def select_cases(args: argparse.Namespace) -> list[Case]:
    if args.case:
        chosen = set(args.case)
        return [case for case in CASES if case.name in chosen]
    if args.suite == "fast":
        return list(FAST_CASES)
    if args.suite == "full":
        return list(FAST_CASES + EXECUTION_CASES + PERMISSION_CASES)
    if args.suite == "live":
        return list(LIVE_CASES)
    if args.suite.startswith("live-"):
        return [case for case in LIVE_CASES if case.group == args.suite.removeprefix("live-")]
    return list(CASES)


def _case_env(case_dir: Path, case: Case, user_id: str) -> dict[str, str]:
    blocked = (
        "ALIBABA_CLOUD_", "ALIYUN_", "AKLESS_", "DASHSCOPE_", "OPENAI_", "IAC_CODE_", "ANTHROPIC_",
        "OTEL_EXPORTER_",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith(blocked)}
    env["IAC_CODE_CONFIG_DIR"] = str(case_dir / "config")
    env[E2E_USER_ID_ENV] = user_id
    if case.script in LOCAL_TELEMETRY_SCRIPTS:
        env["IAC_CODE_TELEMETRY_LOCAL_ONLY"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _prepare_e2e_user_id(settings_path: Path) -> str:
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("settings.yml must contain a mapping")
    user_id = settings.get("userID")
    if not is_e2e_user_id(user_id):
        user_id = "iac_user_e2e_" + uuid.uuid4().hex
        settings["userID"] = user_id
        settings_path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    return user_id


def _prepare_cloud_credentials(helper: Path, config_dir: Path, helper_python: Path | None = None) -> None:
    command = [
        str(helper_python or sys.executable), str(helper), "cloud", "--output",
        str(config_dir / ".cloud-credentials.yml"),
    ]
    try:
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloudCredentialSetupError(type(exc).__name__) from exc
    if result.returncode != 0 or not (config_dir / ".cloud-credentials.yml").is_file():
        raise CloudCredentialSetupError("helper returned no usable cloud credential")


def _stop_tree(process: subprocess.Popen[bytes], grace: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # The parent may exit while a child is still in cleanup; close the whole group.
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # Still report the timeout if the OS has not reaped an unkillable process.
        pass


def _read_summary(case_dir: Path, source: str) -> dict[str, Any] | None:
    path = case_dir / source
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        if source == "stdout.log":
            lines = content.splitlines()
            if not lines:
                return None
            content = lines[-1]
        value = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _live_cleanup_status(case: Case, summary: dict[str, Any] | None) -> str:
    if case.live_runner in {"selector", "canary", "legacy_a2a_readonly", "smoke"}:
        return "not-needed"
    if summary is None:
        return "unverified"
    if case.live_runner == "legacy_a2a":
        return str(summary.get("cleanup_status") or "unverified")
    if case.live_runner == "repl":
        checks = summary.get("checks")
        if isinstance(checks, dict):
            teardown = [value for key, value in checks.items() if str(key).startswith("teardown:")]
            if any(value is False for value in teardown):
                return "failed"
            if teardown and all(value is True for value in teardown):
                return "completed"
        return "unverified"
    return str(summary.get("cleanup_status") or "unverified")


def _public_live_summary(summary: dict[str, Any] | None, cleanup_status: str | None = None) -> dict[str, Any] | None:
    if summary is None:
        return None
    # Live runner notes, errors, and filesystem paths can contain provider data.
    # Keep only fixed-schema status fields in CI artifacts and rendered reports.
    checks = summary.get("checks")
    raw_watchdog = summary.get("watchdog")
    watchdog: dict[str, Any] | None = None
    if isinstance(raw_watchdog, dict):
        state = raw_watchdog.get("state")
        action = raw_watchdog.get("action")
        waiting_for = raw_watchdog.get("waitingFor")
        elapsed = raw_watchdog.get("elapsedSeconds")
        cue = raw_watchdog.get("cue")
        if (
            isinstance(state, str)
            and state in {
                "waiting_for_input", "terminal_error", "normal_operation", "unknown", "unavailable", "no_output",
            }
            and isinstance(action, str)
            and action in {"early_abort", "observe"}
            and isinstance(waiting_for, str)
            and re.fullmatch(r"[A-Za-z0-9 _()-]{1,100}", waiting_for)
            and isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
        ):
            watchdog = {
                "state": state, "action": action, "waitingFor": waiting_for,
                "elapsedSeconds": round(max(0.0, min(float(elapsed), 2700.0)), 1),
            }
            if isinstance(cue, str) and cue in {"ask_question", "candidate_controls", "repl_prompt", "none"}:
                watchdog["cue"] = cue
    raw_progress = summary.get("progress")
    allowed_progress = {
        "candidate_selection_ready", "candidate_selection_submitted", "user_input_required", "user_input_received",
        "step_started", "step_completed", "pipeline_completed", "pipeline_failed",
        "step_started_deploying", "step_completed_deploying", "ros_deploy_used",
        "aliyun_api_used", "ros_stack_used", "bash_used",
        "ros_deploy_result", "ros_deploy_result_error",
        "pipeline_completed_early_exit", "stack_progress", "stack_progress_create_complete",
        "cleanup_ledger_files", "cleanup_ledger_found", "observed_stack_count",
        "cloud_stack_without_ledger", "cloud_stack_not_created", "cloud_probe_failures",
        "cleanup_failure_create_failed", "cleanup_failure_create_failed_after_rollback",
        "cleanup_failure_route_conflict", "cleanup_failure_route_conflict_after_rollback",
        "cleanup_failure_stack_exists", "cleanup_failure_stack_exists_after_rollback",
        "cleanup_failure_invalid_cidr_block", "cleanup_failure_invalid_cidr_block_after_rollback",
    }
    public = {
        "case_id": summary.get("case_id"),
        "scenario": summary.get("scenario"),
        "status": summary.get("status") or ("passed" if summary.get("passed") is True else "failed"),
        "cleanup_status": cleanup_status or summary.get("cleanup_status"),
        "checks": {str(key): value for key, value in checks.items() if isinstance(value, bool)}
        if isinstance(checks, dict)
        else {},
    }
    raw_diagnostics = summary.get("diagnostics")
    if isinstance(raw_diagnostics, dict):
        diagnostics: dict[str, Any] = {}
        for key in (
            "confirmation_event_count", "unstructured_confirmation_count", "image_confirmation_count",
            "ros_deploy_event_count", "public_tool_event_count",
            "public_journal_aliyun_count", "persisted_aliyun_public_tool_event_count",
            "repl_confirmation_count", "candidate_option_count",
            "repl_selection_ready_count", "repl_selection_submitted_count", "repl_step_started_count",
            "repl_step1_stall_restarts", "repl_step2_stall_restarts", "repl_selection_image_retries",
            "repl_normal_resume_reselections",
            "repl_step2_attempt_count", "repl_step2_tool_use_count",
            "text_exit_code", "text_output_length",
            "cleanup_turn_event_count", "cleanup_turn_cleanup_event_count", "cleanup_target_count",
            "cleanup_ledger_pending_count", "cleanup_delete_tool_use_count", "cleanup_get_tool_use_count",
            "cleanup_failure_event_count", "cleanup_delete_http_status", "cleanup_get_http_status",
        ):
            count = raw_diagnostics.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 10000:
                diagnostics[key] = count
        for key in (
            "repl_solution_summary_changed", "repl_effective_parameters_changed",
            "repl_first_rollback_input_intact",
            "persisted_aliyun_tool_publicly_seen", "persisted_aliyun_publicly_attributed",
            "text_has_vpc_marker",
            "cleanup_prompt_active", "cleanup_first_ros_not_found",
            "cleanup_delete_target_matches", "cleanup_get_target_matches",
        ):
            if isinstance(raw_diagnostics.get(key), bool):
                diagnostics[key] = raw_diagnostics[key]
        pending_kinds = raw_diagnostics.get("a2a_pending_kinds")
        allowed_pending = {
            "none", "ask_user_question", "candidate_select", "candidate_selection", "deployment_confirmation",
        }
        if isinstance(pending_kinds, list):
            diagnostics["a2a_pending_kinds"] = [
                kind for kind in pending_kinds if isinstance(kind, str) and kind in allowed_pending
            ][:24]
        public_tool_names = raw_diagnostics.get("public_tool_names")
        allowed_tool_names = {
            "aliyun_api", "ros_deploy", "ros_stack", "write", "write_file", "edit", "edit_file", "bash",
        }
        if isinstance(public_tool_names, list):
            diagnostics["public_tool_names"] = [
                name for name in public_tool_names if isinstance(name, str) and name in allowed_tool_names
            ][:16]
        persisted_tool_names = raw_diagnostics.get("persisted_aliyun_public_tool_names")
        allowed_attributed_names = allowed_tool_names | {
            "ros_preview_template", "ros_estimate_template_cost", "ros_get_template_parameter_constraints",
            "ros_validate_template", "complete_step", "ask_user_question", "read_file", "show_candidate_detail",
            "select_cloud_resource", "resolve_cloud_resource_selector", "none",
        }
        if isinstance(persisted_tool_names, list):
            diagnostics["persisted_aliyun_public_tool_names"] = [
                name for name in persisted_tool_names
                if isinstance(name, str) and name in allowed_attributed_names
            ][:16]
        tool_name_categories = raw_diagnostics.get("persisted_aliyun_public_tool_name_categories")
        allowed_name_categories = {
            "missing", "non_string", "aliyun_api_alias", "other_tool_name", "other_string",
        }
        if isinstance(tool_name_categories, list):
            diagnostics["persisted_aliyun_public_tool_name_categories"] = [
                category for category in tool_name_categories
                if isinstance(category, str) and category in allowed_name_categories
            ][:8]
        step2_tool_names = raw_diagnostics.get("repl_step2_tool_use_names")
        allowed_step2_tools = allowed_tool_names | {
            "ros_preview_template", "ros_estimate_template_cost", "ros_get_template_parameter_constraints",
            "ros_validate_template", "complete_step", "ask_user_question", "read_file",
        }
        if isinstance(step2_tool_names, list):
            diagnostics["repl_step2_tool_use_names"] = [
                name for name in step2_tool_names if isinstance(name, str) and name in allowed_step2_tools
            ][:16]
        repl_step_ids = raw_diagnostics.get("repl_step_started_ids")
        allowed_repl_steps = {
            "solution_planning_and_selection", "materialize_selected_candidate", "deploying",
        }
        if isinstance(repl_step_ids, list):
            diagnostics["repl_step_started_ids"] = [
                step for step in repl_step_ids if isinstance(step, str) and step in allowed_repl_steps
            ][:16]
        image_keys = raw_diagnostics.get("repl_image_keys")
        allowed_images = {
            "initial", "selection", "ask-first-answer", "ask-second-answer", "confirmation-adjust",
            "rollback-interrupt", "rollback-ask-answer", "normal-followup",
            *(f"{phase}-parameter-{index}" for phase in ("initial", "adjustment", "rollback") for index in (2, 3, 4)),
        }
        if isinstance(image_keys, list):
            diagnostics["repl_image_keys"] = [
                key for key in image_keys if isinstance(key, str) and key in allowed_images
            ][:16]
        failed_wait_phase = raw_diagnostics.get("repl_failed_wait_phase")
        if failed_wait_phase in {
            "initial_image_input", "adjustment_image_input", "rollback_image_input",
            "pipeline_handoff", "normal_followup", "other",
        }:
            diagnostics["repl_failed_wait_phase"] = failed_wait_phase
        allowed_cleanup_states = {"pending", "started", "in_progress", "completed", "failed", "unknown"}
        allowed_ros_states = {
            "CREATE_COMPLETE", "DELETE_STARTED", "DELETE_IN_PROGRESS", "DELETE_COMPLETE", "DELETE_FAILED", "unknown",
        }
        for key in ("cleanup_first_ledger_status", "cleanup_first_snapshot_status"):
            value = raw_diagnostics.get(key)
            if isinstance(value, str) and value in allowed_cleanup_states:
                diagnostics[key] = value
        ros_status = raw_diagnostics.get("cleanup_first_ros_status")
        if isinstance(ros_status, str) and ros_status in allowed_ros_states:
            diagnostics["cleanup_first_ros_status"] = ros_status
        allowed_task_states = {
            "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "unknown",
        }
        terminal_state = raw_diagnostics.get("cleanup_turn_terminal_state")
        if isinstance(terminal_state, str) and terminal_state in allowed_task_states:
            diagnostics["cleanup_turn_terminal_state"] = terminal_state
        for key in ("cleanup_delete_error_code", "cleanup_get_error_code"):
            value = raw_diagnostics.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value):
                diagnostics[key] = value
        for key in ("cleanup_delete_tool_kind", "cleanup_get_tool_kind"):
            value = raw_diagnostics.get(key)
            if isinstance(value, str) and value in {"aliyun_api", "ros_stack", "unknown"}:
                diagnostics[key] = value
        allowed_error_kinds = {
            "permission", "credential", "not_found", "resource_busy", "rate_limited",
            "invalid_input", "timeout", "network", "unknown",
        }
        for key in ("cleanup_delete_error_kind", "cleanup_get_error_kind"):
            value = raw_diagnostics.get(key)
            if isinstance(value, str) and value in allowed_error_kinds:
                diagnostics[key] = value
        if diagnostics:
            public["diagnostics"] = diagnostics
    if isinstance(raw_progress, dict):
        public["progress"] = {
            key: value for key, value in raw_progress.items()
            if key in allowed_progress
            and isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 10000
        }
    cleanup_diagnostic = summary.get("cleanup_diagnostic")
    if isinstance(cleanup_diagnostic, dict):
        safe_cleanup_diagnostic: dict[str, Any] = {}
        error_type = cleanup_diagnostic.get("error_type")
        if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,59}", error_type):
            safe_cleanup_diagnostic["error_type"] = error_type
        stage = cleanup_diagnostic.get("stage")
        if stage in {"credential_lookup", "client_create", "list_stacks", "other"}:
            safe_cleanup_diagnostic["stage"] = stage
        sdk_code = cleanup_diagnostic.get("sdk_code")
        if isinstance(sdk_code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", sdk_code):
            safe_cleanup_diagnostic["sdk_code"] = sdk_code
        for count_key in ("failure_count", "remaining_count"):
            count = cleanup_diagnostic.get(count_key)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 100:
                safe_cleanup_diagnostic[count_key] = count
        if safe_cleanup_diagnostic:
            public["cleanup_diagnostic"] = safe_cleanup_diagnostic
    error_type = summary.get("error_type")
    error_site = summary.get("error_site")
    if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,59}", error_type):
        public["error_type"] = error_type
    safe_error_site = r"(?:scripts|src)/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.py:[1-9][0-9]{0,5}"
    if isinstance(error_site, str) and re.fullmatch(safe_error_site, error_site):
        public["error_site"] = error_site
    if summary.get("failure_stage") in {
        "pre_rollback_candidate", "rollback_completion", "post_rollback_confirmation",
        "post_rollback_step", "restart", "resume", "verify",
        "initial_selection", "first_stack_create", "rollback_cleanup", "second_stack_create",
        "cleanup_recovery", "cleanup_normal_turn", "cleanup_verify",
    }:
        public["failure_stage"] = summary["failure_stage"]
    states = summary.get("a2a_states")
    allowed_states = {
        "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED", "TASK_STATE_AUTH_REQUIRED", "TASK_STATE_UNKNOWN",
    }
    if isinstance(states, list):
        public["a2a_states"] = [state for state in states if isinstance(state, str) and state in allowed_states][:12]
    if summary.get("a2a_phase") in {"answer", "next-turn"}:
        public["a2a_phase"] = summary["a2a_phase"]
    event_count = summary.get("a2a_event_count")
    if isinstance(event_count, int) and not isinstance(event_count, bool) and 0 <= event_count <= 100000:
        public["a2a_event_count"] = event_count
    if isinstance(summary.get("a2a_text_present"), bool):
        public["a2a_text_present"] = summary["a2a_text_present"]
    raw_line_count = summary.get("a2a_raw_line_count")
    if isinstance(raw_line_count, int) and not isinstance(raw_line_count, bool) and 0 <= raw_line_count <= 100000:
        public["a2a_raw_line_count"] = raw_line_count
    if summary.get("a2a_response_content_type") in {"text/event-stream", "application/json", "text/plain"}:
        public["a2a_response_content_type"] = summary["a2a_response_content_type"]
    jsonrpc_error_code = summary.get("jsonrpc_error_code")
    if (
        isinstance(jsonrpc_error_code, int)
        and not isinstance(jsonrpc_error_code, bool)
        and -1000000 <= jsonrpc_error_code <= 1000000
    ):
        public["jsonrpc_error_code"] = jsonrpc_error_code
    terminal_markers = summary.get("terminal_markers")
    allowed_terminal_markers = {
        "resource_selection_resume_invalid", "active session", "execution", "permission",
        "credential", "timeout", "model", "context", "task", "selector",
        "task is already working", "not found", "terminal state", "rate limit", "unsupported", "duplicate",
    }
    if isinstance(terminal_markers, list):
        public["terminal_markers"] = [
            marker for marker in terminal_markers
            if isinstance(marker, str) and marker in allowed_terminal_markers
        ][:10]
    if isinstance(summary.get("terminal_message_present"), bool):
        public["terminal_message_present"] = summary["terminal_message_present"]
    control_state = summary.get("control_state")
    if isinstance(control_state, dict):
        safe_control_state = {
            key: value for key, value in control_state.items()
            if key in {"present", "task_matches", "release_ready", "input_handoff_ready", "stream_available"}
            and (isinstance(value, bool) or value is None)
        }
        if control_state.get("phase") in {"running", "paused", "terminating", "terminated"}:
            safe_control_state["phase"] = control_state["phase"]
        if control_state.get("execution_status") in {
            "working", "input-required", "completed", "failed", "canceled",
        }:
            safe_control_state["execution_status"] = control_state["execution_status"]
        blocker_count = control_state.get("blocker_count")
        if isinstance(blocker_count, int) and not isinstance(blocker_count, bool) and 0 <= blocker_count <= 100:
            safe_control_state["blocker_count"] = blocker_count
        for key in ("active_subprocess_tools", "external_operation_count"):
            count = control_state.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 100:
                safe_control_state[key] = count
        for key in ("subprocess_tracking", "revision_settled"):
            if isinstance(control_state.get(key), bool):
                safe_control_state[key] = control_state[key]
        if control_state.get("backup_status") in {
            "not_requested", "disabled", "shared_committed", "staged_committed", "failed"
        }:
            safe_control_state["backup_status"] = control_state["backup_status"]
        public["control_state"] = safe_control_state
    raw_error = summary.get("error")
    if isinstance(raw_error, str) and "A2A task entered unexpected terminal state TASK_STATE_FAILED" in raw_error:
        terminal_message = raw_error.rsplit("TASK_STATE_FAILED", 1)[-1]
        terminal_text = terminal_message.lower()
        normalized_latin = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", terminal_message).replace("_", " ").lower()
        safe_terms = [
            term for term in SAFE_TERMINAL_TERMS
            if term.isascii() and re.search(r"\b{}\b".format(term), normalized_latin)
        ]
        safe_terms.extend(term for term in SAFE_TERMINAL_TERMS if not term.isascii() and term in terminal_text)
        if safe_terms:
            public["terminal_terms"] = safe_terms[:12]
        for code in TERMINAL_FIXED_CODES:
            if code in terminal_text:
                public["terminal_code"] = code
                break
        public["terminal_message_present"] = bool(terminal_text.strip(" :"))
        public["terminal_category"] = next(
            (category for category, pattern in TERMINAL_CATEGORIES if re.search(pattern, terminal_text)), "other"
        )
        known_exceptions = (
            "AssertionError", "AttributeError", "ConnectionError", "FileNotFoundError", "KeyError",
            "PermissionError", "RuntimeError", "TimeoutError", "TypeError", "ValueError",
        )
        for exception in known_exceptions:
            if re.search(r"\b{}:".format(exception), terminal_text, re.IGNORECASE):
                public["terminal_exception"] = exception
                break
    if watchdog is not None:
        public["watchdog"] = watchdog
    return public


def _live_a2a_terminal_evidence(script_dir: Path) -> dict[str, Any]:
    """Read local A2A events and return fixed-schema failure clues, never event text."""
    from scripts.a2a.debugger import _extract_pipeline_envelopes

    evidence: dict[str, Any] = {}
    safe_event_types = {
        "step_started", "step_completed", "step_failed", "input_required", "input_received",
        "rollback_started", "rollback_completed", "cleanup_started", "cleanup_completed",
        "pipeline_completed", "pipeline_failed", "pipeline_user_aborted",
    }
    recent_events: list[str] = []

    def record_failure(envelope: dict[str, Any]) -> None:
        event_type = envelope.get("eventType")
        if isinstance(event_type, str) and event_type in safe_event_types:
            recent_events.append(event_type)
            del recent_events[:-12]
        if event_type not in {"pipeline_failed", "step_failed"}:
            return
        prefix = "terminal" if event_type == "pipeline_failed" else "step_failure"
        evidence[event_type + "_event"] = "observed"
        if event_type == "step_failed":
            step = envelope.get("step")
            step_id = step.get("id") if isinstance(step, dict) else envelope.get("step_id")
            if step_id in {
                "solution_planning_and_selection", "materialize_selected_candidate", "deploying",
                "intent_parsing", "architecture_generation", "confirm_and_select", "deployment_preparation",
            }:
                evidence["step_failure_step"] = step_id
        data = envelope.get("data")
        if not isinstance(data, dict):
            return
        details = data.get("errorDetails")
        inner_type = details.get("type") if isinstance(details, dict) else None
        if isinstance(inner_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,59}", inner_type):
            evidence[prefix + "_inner_type"] = inner_type
        error_summary = data.get("errorSummary")
        if isinstance(error_summary, str):
            lower_summary = error_summary.lower()
            evidence[prefix + "_category"] = next(
                (category for category, pattern in TERMINAL_CATEGORIES if re.search(pattern, lower_summary)),
                "other",
            )
            normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", error_summary).replace("_", " ").lower()
            terms = [
                term for term in SAFE_TERMINAL_TERMS
                if (re.search(r"\b{}\b".format(term), normalized) if term.isascii() else term in normalized)
            ]
            if terms:
                evidence[prefix + "_terms"] = terms[:12]
            code = next((code for code in TERMINAL_FIXED_CODES if code in lower_summary), None)
            if code is not None:
                evidence[prefix + "_code"] = code

    for event_path in (*script_dir.glob("*.events.jsonl"), *script_dir.rglob("a2a-events.jsonl")):
        with event_path.open(encoding="utf-8", errors="replace") as events:
            for line in events:
                if not any(event_type in line for event_type in safe_event_types):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_path.name == "a2a-events.jsonl":
                    records = payload.get("events") if isinstance(payload, dict) else None
                    for envelope in records if isinstance(records, list) else [payload]:
                        if isinstance(envelope, dict):
                            record_failure(envelope)
                else:
                    for envelope in _extract_pipeline_envelopes(payload):
                        record_failure(envelope)
    if recent_events:
        evidence["pipeline_events"] = recent_events
    return evidence


def _tail(path: Path, limit: int = 4000) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


def _failure_details(summary: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    if summary is None:
        return [], []
    records = [summary]
    scenarios = summary.get("scenarios")
    if isinstance(scenarios, list):
        records.extend(item for item in scenarios if isinstance(item, dict))
    failed_checks: list[str] = []
    notes: list[str] = []
    for record in records:
        checks = record.get("checks")
        if isinstance(checks, dict):
            failed_checks.extend(str(key) for key, value in checks.items() if value is False)
        record_notes = record.get("notes")
        if isinstance(record_notes, list):
            notes.extend(str(note) for note in record_notes)
    return failed_checks, notes


def run_case(
    case: Case, run_dir: Path, credential_source_dir: Path | None = None,
    cloud_credential_helper: Path | None = None,
    cloud_credential_python: Path | None = None,
) -> dict[str, Any]:
    case_dir = run_dir / "runs" / case.name
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "summary.json").unlink(missing_ok=True)
    # Permission scripts create their run directory with exist_ok=False. Keep their
    # workspace below the case directory so the parent can hold process logs.
    needs_fresh_dir = case.name.startswith("permission-") or case.live_runner in {"selector", "repl"}
    script_dir = case_dir / ("scenario-" + uuid.uuid4().hex) if needs_fresh_dir else case_dir
    command = [sys.executable, str(REPO_ROOT / case.script), "--run-dir", str(script_dir), *case.args]
    case_user_id = "iac_user_e2e_" + uuid.uuid4().hex
    if case.suite == "live":
        if credential_source_dir is None:
            raise ValueError("live case requires credential source directory")
        config_dir = case_dir / "config"
        config_dir.mkdir(mode=0o700, exist_ok=True)
        source_config_dir = (
            case_dir / "credential-source"
            if case.live_runner in {"selling", "canary"} and cloud_credential_helper is not None else config_dir
        )
        source_config_dir.mkdir(mode=0o700, exist_ok=True)
        filenames = (
            (".credentials.yml", "settings.yml") if case.live_runner == "smoke" or cloud_credential_helper is not None
            else (".credentials.yml", ".cloud-credentials.yml", "settings.yml")
        )
        for filename in filenames:
            destination = source_config_dir / filename
            shutil.copyfile(credential_source_dir / filename, destination)
            destination.chmod(0o600)
        case_user_id = _prepare_e2e_user_id(source_config_dir / "settings.yml")
        if case.live_runner != "smoke" and cloud_credential_helper is not None:
            _prepare_cloud_credentials(cloud_credential_helper, source_config_dir, cloud_credential_python)
        if case.live_runner != "smoke":
            command.append("--allow-real-cloud")
        if case.live_runner == "selling":
            command.extend(
                ("--concurrency", "1", "--inherit-settings", "--credential-source-dir",
                 str(source_config_dir if cloud_credential_helper is not None else credential_source_dir))
            )
            if case.cloud_write:
                command.append("--allow-cloud-write")
        elif case.live_runner == "selector":
            command.extend(("--source-config-dir", str(source_config_dir)))
        elif case.live_runner == "canary":
            command.extend(("--source-config-dir", str(
                source_config_dir if cloud_credential_helper is not None else credential_source_dir
            )))
        elif case.live_runner == "repl":
            command.extend(("--source-config-dir", str(source_config_dir)))
    if case in FAST_CASES or (case.suite == "live" and case.live_runner != "smoke"):
        command.extend(("--python", sys.executable))
    case_env = _case_env(case_dir, case, case_user_id)
    if case.live_runner == "smoke":
        isolated_home = case_dir / "home"
        isolated_home.mkdir(mode=0o700, exist_ok=True)
        case_env["HOME"] = str(isolated_home)
        case_env["USERPROFILE"] = str(isolated_home)
        case_env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
    started = time.monotonic()
    timed_out = False
    error = ""
    return_code: int | None = None
    refresh_stop = threading.Event()
    refresh_failed = threading.Event()
    refresh_thread: threading.Thread | None = None

    def refresh_cloud_credentials() -> None:
        assert cloud_credential_helper is not None
        if case.live_runner == "selector":
            runtime_config_dir = script_dir / ".runtime-config"
        elif case.live_runner == "repl":
            runtime_config_dir = config_dir / ".e2e-runs" / script_dir.name
        else:
            runtime_config_dir = config_dir
        while not refresh_stop.wait(CLOUD_REFRESH_SECONDS):
            try:
                _prepare_cloud_credentials(cloud_credential_helper, runtime_config_dir, cloud_credential_python)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                refresh_failed.set()

    try:
        with (case_dir / "stdout.log").open("wb") as stdout, (case_dir / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=case_env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            if cloud_credential_helper is not None and case.suite == "live" and case.live_runner != "smoke":
                refresh_thread = threading.Thread(target=refresh_cloud_credentials, daemon=True)
                refresh_thread.start()
            try:
                return_code = process.wait(timeout=case.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                print("TIMEOUT {}: allowing {}s for cleanup".format(case.name, case.cleanup_grace), flush=True)
                _stop_tree(process, case.cleanup_grace)
                return_code = process.returncode
            finally:
                refresh_stop.set()
                if refresh_thread is not None:
                    refresh_thread.join(timeout=95)
    except (OSError, subprocess.SubprocessError) as exc:
        error = "{}: {}".format(type(exc).__name__, exc)
    if refresh_failed.is_set():
        error = "cloud credential refresh failed; inspect CI job log"
    fallback_cleanup_status: str | None = None
    if timed_out and case.live_runner == "legacy_a2a" and (script_dir / "owned-stacks.json").is_file():
        if cloud_credential_helper is not None:
            try:
                _prepare_cloud_credentials(cloud_credential_helper, case_dir / "config", cloud_credential_python)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                fallback_cleanup_status = "failed"
        cleanup_command = [
            sys.executable, str(REPO_ROOT / "scripts/a2a/e2e/cleanup_owned_stacks.py"),
            "--run-dir", str(script_dir), "--timeout", "840",
        ]
        try:
            if fallback_cleanup_status == "failed":
                raise RuntimeError("cloud credential refresh failed before fallback cleanup")
            with (case_dir / "cleanup.stdout.log").open("wb") as stdout, (
                case_dir / "cleanup.stderr.log"
            ).open("wb") as stderr:
                cleanup_process = subprocess.run(
                    cleanup_command, cwd=REPO_ROOT, env=_case_env(case_dir, case, case_user_id),
                    stdout=stdout, stderr=stderr, timeout=900, check=False,
                )
            fallback_cleanup_status = "completed" if cleanup_process.returncode == 0 else "failed"
        except (OSError, RuntimeError, subprocess.SubprocessError):
            fallback_cleanup_status = "failed"
    source = "stdout.log" if case.result_source == "stdout" else case.result_source
    if script_dir != case_dir and source != "stdout.log":
        source = str(script_dir.relative_to(case_dir) / source)
    summary = _read_summary(case_dir, source)
    summary_passed = summary is not None and (summary.get("passed") is True or summary.get("status") == "passed")
    passed = return_code == 0 and summary_passed and not timed_out and not error
    cleanup_status = (
        fallback_cleanup_status or _live_cleanup_status(case, summary)
        if case.suite == "live" else None
    )
    safe_audit_notes = (
        [note for note in summary.get("notes", []) if isinstance(note, str) and SAFE_LIVE_AUDIT_NOTE.fullmatch(note)]
        if case.suite == "live" and isinstance(summary, dict) and isinstance(summary.get("notes"), list)
        else []
    )
    if case.suite == "live":
        summary = _public_live_summary(summary, cleanup_status)
        if case.live_runner == "selling" and isinstance(summary, dict):
            summary.update(_live_a2a_terminal_evidence(script_dir))
        error = "" if not error else "runner failed to start; inspect CI job log"
    failed_checks, notes = _failure_details(summary)
    notes.extend(safe_audit_notes)
    result = {
        "name": case.name,
        "status": "passed" if passed else "timeout" if timed_out else "failed",
        "durationSeconds": round(time.monotonic() - started, 2),
        "timeoutSeconds": case.timeout,
        "returnCode": return_code,
        "command": command if case.suite != "live" else [case.name],
        "summary": summary,
        "failedChecks": failed_checks,
        "notes": notes,
        "cleanupStatus": cleanup_status,
        "error": error,
        "stdoutTail": "" if case.suite == "live" else _tail(case_dir / "stdout.log"),
        "stderrTail": "" if case.suite == "live" else _tail(case_dir / "stderr.log"),
        "live": case.suite == "live",
        "artifacts": "runs/{}/".format(case.name),
    }
    (case_dir / "ci-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _reason(result: dict[str, Any]) -> str:
    if result["status"] == "timeout":
        reason = "超过 {} 秒硬超时；进程组已终止".format(result["timeoutSeconds"])
    elif result["error"]:
        reason = result["error"]
    elif result.get("cleanupStatus") == "failed":
        reason = "测试资源清理失败；检查 CI 作业日志和云账号残留资源"
    elif isinstance(result.get("summary"), dict) and isinstance(result["summary"].get("watchdog"), dict) and (
        result["summary"]["watchdog"].get("action") == "early_abort"
    ):
        watchdog = result["summary"]["watchdog"]
        if watchdog["state"] == "no_output":
            reason = "REPL 等待 {} 时终端长期无输出；{} 秒提前终止".format(
                watchdog["waitingFor"], watchdog["elapsedSeconds"]
            )
        else:
            reason = "REPL 交互偏离：等待 {} 时出现额外输入；{} 秒提前终止".format(
                watchdog["waitingFor"], watchdog["elapsedSeconds"]
            )
    elif result["failedChecks"]:
        reason = "检查失败：" + ", ".join(result["failedChecks"])
    elif result["live"] and isinstance(result.get("summary"), dict) and result["summary"].get("error_type"):
        reason = "异常：{}".format(result["summary"]["error_type"])
        if result["summary"].get("error_site"):
            reason += "（{}）".format(result["summary"]["error_site"])
        if result["summary"].get("terminal_category"):
            reason += "；A2A 终态类别：{}".format(result["summary"]["terminal_category"])
        if result["summary"].get("terminal_exception"):
            reason += "；内部异常：{}".format(result["summary"]["terminal_exception"])
        if result["summary"].get("terminal_inner_type"):
            reason += "；流水线异常：{}".format(result["summary"]["terminal_inner_type"])
    elif result["notes"]:
        first_lines = [str(note).splitlines()[0] for note in result["notes"][:3]]
        reason = "；".join(first_lines)[:240]
    elif result["summary"] is None:
        reason = "未生成有效场景摘要；" + ("查看 CI 作业日志" if result["live"] else "查看 stdout/stderr 和服务日志")
    else:
        reason = "退出码 {}；查看详细日志".format(result["returnCode"])
    if result["live"] and result["cleanupStatus"] == "unverified":
        reason += "；清理结果未验证，需检查测试账号残留资源"
    return reason


def _report_label(value: str | None, labels: dict[str, str]) -> str:
    return labels.get(value, "未知") if value else "—"


def _write_junit(run_dir: Path, results: list[dict[str, Any]]) -> None:
    suite = ET.Element(
        "testsuite",
        name="iac-code deterministic E2E",
        tests=str(len(results)),
        failures=str(sum(result["status"] != "passed" for result in results)),
        time=str(round(sum(result["durationSeconds"] for result in results), 2)),
    )
    for result in results:
        case = ET.SubElement(suite, "testcase", name=result["name"], time=str(result["durationSeconds"]))
        if result["status"] != "passed":
            ET.SubElement(case, "failure", message=_reason(result), type=result["status"]).text = (
                result["stderrTail"] or result["stdoutTail"]
            )
        ET.SubElement(case, "system-out").text = result["stdoutTail"]
    ET.indent(suite)
    ET.ElementTree(suite).write(run_dir / "junit.xml", encoding="utf-8", xml_declaration=True)


def _write_reports(run_dir: Path, results: list[dict[str, Any]], elapsed: float) -> None:
    passed = sum(result["status"] == "passed" for result in results)
    failed = len(results) - passed
    live = any(result["live"] for result in results)
    title = "真实云 E2E 报告" if live else "确定性 E2E 报告"
    summary = {
        "passed": failed == 0,
        "total": len(results),
        "passedCount": passed,
        "failedCount": failed,
        "wallSeconds": round(elapsed, 2),
        "cases": results,
        "excluded": [{"scope": scope, "reason": reason} for scope, reason in EXCLUDED],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# " + title,
        "",
        "**{} / {} 通过** · 总耗时 {:.1f} 秒 · 并行执行".format(passed, len(results), elapsed),
        "",
        "| 用例 | 结果 | 耗时 | 清理 | 初步线索 |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for result in results:
        reason = "—" if result["status"] == "passed" else _reason(result).replace("|", "\\|").replace("\n", " ")
        lines.append(
            "| [{}]({}ci-result.json) | {} | {:.1f}s | {} | {} |".format(
                result["name"], result["artifacts"], _report_label(result["status"], RESULT_LABELS),
                result["durationSeconds"], _report_label(result.get("cleanupStatus"), CLEANUP_LABELS), reason,
            )
        )
    lines.extend(["", "## 失败用例复盘入口", ""])
    if failed:
        lines.append(
            "对每个失败用例，Agent 应读取 `ci-result.json`、`summary.json`、日志及相关源码，"
            "复现后区分产品缺陷、用例缺陷、环境故障与超时。不得只根据日志尾部猜测结论。"
        )
        lines.append("")
        for result in results:
            if result["status"] != "passed":
                lines.append("- **{}**：{}；证据目录 `{}`".format(result["name"], _reason(result), result["artifacts"]))
    else:
        lines.append("无失败用例。")
    lines.extend(["", "## 未纳入自动 CI 的范围", ""])
    for scope, reason in EXCLUDED:
        lines.append("- **{}**：{}".format(scope, reason))
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    cards = []
    for result in results:
        detail = html.escape(_reason(result) if result["status"] != "passed" else "通过")
        log = html.escape((result["stderrTail"] or result["stdoutTail"])[-2000:])
        artifact = html.escape(result["artifacts"], quote=True)
        log_links = (
            "真实用例原始日志仅保留在 CI 作业中"
            if result["live"]
            else '<a href="{}stdout.log">stdout</a> · <a href="{}stderr.log">stderr</a>'.format(artifact, artifact)
        )
        cards.append(
            '<details><summary><b>{}</b> · {} · {:.1f}s</summary><p>{}</p>'
            '<p><a href="{}ci-result.json">结构化结果</a> · {}</p><pre>{}</pre></details>'.format(
                html.escape(result["name"]), _report_label(result["status"], RESULT_LABELS),
                result["durationSeconds"], detail,
                artifact, log_links, log,
            )
        )
    page = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>E2E 报告</title>'
        '<style>body{{font:16px system-ui;max-width:1000px;margin:3rem auto;padding:0 1rem}}'
        'details{{border:1px solid #ddd;border-radius:6px;padding:1rem;margin:.7rem 0}}'
        'pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:1rem}}</style>'
        '<h1>{}</h1><p><b>{}/{}</b> 通过 · 总耗时 {:.1f} 秒</p>{}</html>'
    ).format(html.escape(title), passed, len(results), elapsed, "".join(cards))
    (run_dir / "report.html").write_text(page, encoding="utf-8")
    _write_junit(run_dir, results)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = select_cases(args)
    if args.list:
        inventory = {"included": [case.name for case in selected], "excluded": EXCLUDED}
        print(json.dumps(inventory, ensure_ascii=False, indent=2))
        return 0
    args.run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    locks = {case.resource_lock: threading.Lock() for case in selected if case.resource_lock}

    def run_with_lock(case: Case) -> dict[str, Any]:
        lock = locks.get(case.resource_lock)
        if lock is None:
            return run_case(
                case, args.run_dir, args.credential_source_dir,
                args.cloud_credential_helper, args.cloud_credential_python,
            )
        with lock:
            return run_case(
                case, args.run_dir, args.credential_source_dir,
                args.cloud_credential_helper, args.cloud_credential_python,
            )

    with ThreadPoolExecutor(max_workers=min(args.jobs, len(selected))) as pool:
        futures = {
            pool.submit(run_with_lock, case): case for case in selected
        }
        completed = {}
        for future in as_completed(futures):
            case = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                if case.suite != "live":
                    error = "{}: {}".format(type(exc).__name__, exc)
                elif isinstance(exc, CloudCredentialSetupError):
                    error = "cloud credential setup failed; inspect CI job log"
                else:
                    error = "runner exception; inspect CI job log"
                result = {
                    "name": case.name,
                    "status": "failed",
                    "durationSeconds": 0,
                    "timeoutSeconds": case.timeout,
                    "returnCode": None,
                    "command": [case.name],
                    "summary": None,
                    "failedChecks": [],
                    "notes": [],
                    "cleanupStatus": "unverified" if case.suite == "live" else None,
                    "error": error,
                    "stdoutTail": "",
                    "stderrTail": "",
                    "live": case.suite == "live",
                    "artifacts": "runs/{}/".format(case.name),
                }
                case_dir = args.run_dir / result["artifacts"]
                case_dir.mkdir(parents=True, exist_ok=True)
                (case_dir / "ci-result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            completed[result["name"]] = result
            print(
                "{} {} ({:.1f}s)".format(result["status"].upper(), result["name"], result["durationSeconds"]),
                flush=True,
            )
    results = [completed[case.name] for case in selected]
    _write_reports(args.run_dir, results, time.monotonic() - started)
    print("报告：{}".format(args.run_dir / "report.md"), flush=True)
    return 0 if all(result["status"] == "passed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
