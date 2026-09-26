#!/usr/bin/env python3
"""Run selected process E2E cases locally or in CI with bounded parallelism."""

from __future__ import annotations

import argparse
import html
import json
import os
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

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.a2a.e2e.execution_control.run_execution_control_scenarios import SCENARIO_MODES  # noqa: E402
from scripts.a2a.e2e.resource_selector.run_live_resource_selector import SCENARIOS as SELECTOR_SCENARIOS  # noqa: E402
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


FAST_CASES = (
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


def _selling_group(spec: Any) -> str:
    for group in ("core", "recovery", "multimodal", "legacy", "safety"):
        if group in spec.suites:
            return group
    raise ValueError("unclassified selling E2E scenario: " + spec.name)


LIVE_CASES = tuple(
    Case(
        "ssf-" + spec.name, LIVE_SCRIPT, ("--scenario", spec.name), 2700, "live",
        cloud_write=spec.cloud_write, cleanup_grace=900,
        resource_lock=spec.resource_lock, group=_selling_group(spec),
    )
    for spec in SELLING_SCENARIOS
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
) + (
    Case(
        "repl-aliyun-readonly-canary", "scripts/repl/e2e/run_real_aliyun_contract_canary.py",
        (), 900, "live", cleanup_grace=60, live_runner="canary", group="readonly",
    ),
)
CASES += LIVE_CASES
EXCLUDED = (
    (
        "A2A e3a-recovery deterministic contract",
        "isolated local runs consistently lack the expected persisted aliyun_api ToolResult; repair fixture before CI",
    ),
    (
        "selling_solution_first Web and Desktop cases (W01, W02, D01)",
        "require provisioned Chrome or a native Desktop package and display host",
    ),
    (
        "A2A legacy cloud-write recovery (except two read-only step4 cases)",
        "old runner lacks ownership teardown; rollback cleanup cases deliberately retain a second ROS Stack",
    ),
    ("StartChat permission and Qoder reconnect", "real cloud/LLM, Qoder installation and mutable local state"),
    ("Web browser contract", "requires provisioned Chrome and playwright-core; API coverage is listed separately"),
    ("ACP/headless/VPC smoke scripts", "lack run-dir summaries and bounded cleanup for unattended CI"),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=(
            "fast", "full", "live", "live-core", "live-recovery", "live-multimodal",
            "live-readonly", "live-legacy", "live-safety", "live-repl", "all",
        ),
        default="fast",
    )
    parser.add_argument("--case", action="append", choices=sorted(case.name for case in CASES))
    parser.add_argument("--jobs", type=int, default=3, help="Maximum simultaneously running cases")
    parser.add_argument("--run-dir", type=Path, default=REPO_ROOT / "ci-e2e-report")
    parser.add_argument("--credential-source-dir", type=Path)
    parser.add_argument("--allow-cloud-write", action="store_true")
    parser.add_argument("--list", action="store_true", help="Show the allowlist and exclusions without running")
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.jobs > 8:
        parser.error("--jobs must be between 1 and 8")
    selected = select_cases(args)
    if not args.list and any(case.suite == "live" for case in selected):
        if args.credential_source_dir is None:
            parser.error("live cases require --credential-source-dir")
        missing = [
            name
            for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml")
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


def _case_env(case_dir: Path) -> dict[str, str]:
    blocked = ("ALIBABA_CLOUD_", "ALIYUN_", "DASHSCOPE_", "OPENAI_", "IAC_CODE_", "ANTHROPIC_")
    env = {key: value for key, value in os.environ.items() if not key.startswith(blocked)}
    env["IAC_CODE_CONFIG_DIR"] = str(case_dir / "config")
    env["PYTHONUNBUFFERED"] = "1"
    return env


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
    if case.live_runner in {"selector", "canary", "legacy_a2a_readonly"}:
        return "not-needed"
    if summary is None:
        return "unverified"
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
    return {
        "case_id": summary.get("case_id"),
        "scenario": summary.get("scenario"),
        "status": summary.get("status") or ("passed" if summary.get("passed") is True else "failed"),
        "cleanup_status": cleanup_status or summary.get("cleanup_status"),
        "checks": {str(key): value for key, value in checks.items() if isinstance(value, bool)}
        if isinstance(checks, dict)
        else {},
    }


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


def run_case(case: Case, run_dir: Path, credential_source_dir: Path | None = None) -> dict[str, Any]:
    case_dir = run_dir / "runs" / case.name
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "summary.json").unlink(missing_ok=True)
    # Permission scripts create their run directory with exist_ok=False. Keep their
    # workspace below the case directory so the parent can hold process logs.
    needs_fresh_dir = case.name.startswith("permission-") or case.live_runner == "selector"
    script_dir = case_dir / ("scenario-" + uuid.uuid4().hex) if needs_fresh_dir else case_dir
    command = [sys.executable, str(REPO_ROOT / case.script), "--run-dir", str(script_dir), *case.args]
    if case.suite == "live":
        if credential_source_dir is None:
            raise ValueError("live case requires credential source directory")
        config_dir = case_dir / "config"
        config_dir.mkdir(mode=0o700, exist_ok=True)
        for filename in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
            destination = config_dir / filename
            shutil.copyfile(credential_source_dir / filename, destination)
            destination.chmod(0o600)
        command.append("--allow-real-cloud")
        if case.live_runner == "selling":
            command.extend(
                ("--concurrency", "1", "--inherit-settings", "--credential-source-dir", str(credential_source_dir))
            )
            if case.cloud_write:
                command.append("--allow-cloud-write")
        elif case.live_runner == "selector":
            command.extend(("--source-config-dir", str(config_dir)))
        elif case.live_runner == "canary":
            command.extend(("--source-config-dir", str(credential_source_dir)))
        elif case.live_runner == "repl":
            command.extend(("--source-config-dir", str(config_dir)))
    if case in FAST_CASES or case.suite == "live":
        command.extend(("--python", sys.executable))
    started = time.monotonic()
    timed_out = False
    error = ""
    return_code: int | None = None
    try:
        with (case_dir / "stdout.log").open("wb") as stdout, (case_dir / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=_case_env(case_dir),
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            try:
                return_code = process.wait(timeout=case.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                print("TIMEOUT {}: allowing {}s for cleanup".format(case.name, case.cleanup_grace), flush=True)
                _stop_tree(process, case.cleanup_grace)
                return_code = process.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        error = "{}: {}".format(type(exc).__name__, exc)
    source = "stdout.log" if case.result_source == "stdout" else case.result_source
    if script_dir != case_dir and source != "stdout.log":
        source = str(script_dir.relative_to(case_dir) / source)
    summary = _read_summary(case_dir, source)
    summary_passed = summary is not None and (summary.get("passed") is True or summary.get("status") == "passed")
    passed = return_code == 0 and summary_passed and not timed_out and not error
    cleanup_status = _live_cleanup_status(case, summary) if case.suite == "live" else None
    if case.suite == "live":
        summary = _public_live_summary(summary, cleanup_status)
        error = "" if not error else "runner failed to start; inspect CI job log"
    failed_checks, notes = _failure_details(summary)
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
    elif result["failedChecks"]:
        reason = "检查失败：" + ", ".join(result["failedChecks"])
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
                result["name"], result["artifacts"], result["status"], result["durationSeconds"],
                result.get("cleanupStatus") or "—", reason,
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
                html.escape(result["name"]), result["status"], result["durationSeconds"], detail,
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
            return run_case(case, args.run_dir, args.credential_source_dir)
        with lock:
            return run_case(case, args.run_dir, args.credential_source_dir)

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
                error = "runner exception; inspect CI job log" if case.suite == "live" else "{}: {}".format(
                    type(exc).__name__, exc
                )
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
