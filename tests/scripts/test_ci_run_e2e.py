"""Offline tests for the bounded CI E2E runner."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.ci import run_e2e


def test_default_selection_is_allowlisted_and_credential_free() -> None:
    selected = run_e2e.select_cases(run_e2e.parse_args([]))
    assert selected == list(run_e2e.FAST_CASES)
    assert len({case.name for case in run_e2e.CASES}) == len(run_e2e.CASES)
    assert all("--allow-real-cloud" not in case.args for case in run_e2e.CASES)
    assert len(run_e2e.select_cases(run_e2e.parse_args(["--suite", "full"]))) > len(selected)


def test_catalog_includes_headless_surfaces_and_excludes_browser_desktop() -> None:
    full = run_e2e.select_cases(run_e2e.parse_args(["--suite", "full", "--list"]))
    live = run_e2e.select_cases(run_e2e.parse_args(["--suite", "live", "--list"]))
    assert len(full) == 42
    assert len(live) == 103
    assert len(run_e2e.CASES) == 145
    assert {case.name for case in live if case.live_runner.startswith("legacy_a2a")} == {
        "a2a-recovery-" + name for name in run_e2e.A2A_RECOVERY_SCENARIOS
    }
    assert {case.name for case in live if case.live_runner == "smoke"} == {
        "smoke-a2a-vpc", "smoke-acp-vpc", "smoke-headless-vpc"
    }
    fault = next(case for case in live if case.name == "a2a-recovery-fault-after-snapshot")
    assert "--deterministic" in fault.args
    assert all("--ci-teardown" in case.args for case in live if case.live_runner == "legacy_a2a")
    unsupported = {
        "ssf-" + spec.name for spec in run_e2e.SELLING_SCENARIOS
        if spec.surface.value in {"web", "desktop"}
    }
    assert len(unsupported) == 3
    assert not unsupported.intersection(case.name for case in live)
    assert all(case.script != "scripts/a2a/e2e/reconnect/run_qoder_mcp_reconnect.py" for case in live)
    selling = [case for case in live if case.name.startswith("ssf-")]
    assert len(selling) == 42
    pools = [ipaddress.IPv4Network(case.args[case.args.index("--cidr-pool") + 1]) for case in selling]
    assert len(set(pools)) == len(selling)
    assert all(pool.subnet_of(ipaddress.IPv4Network("10.250.0.0/16")) for pool in pools)


def test_missing_or_empty_stdout_summary_is_failure_data(tmp_path: Path) -> None:
    assert run_e2e._read_summary(tmp_path, "stdout.log") is None
    (tmp_path / "stdout.log").write_text("", encoding="utf-8")
    assert run_e2e._read_summary(tmp_path, "stdout.log") is None
    (tmp_path / "stdout.log").write_text('{"passed": true}\n', encoding="utf-8")
    assert run_e2e._read_summary(tmp_path, "stdout.log") == {"passed": True}


def test_live_cleanup_status_is_reported_from_teardown_checks() -> None:
    case = next(case for case in run_e2e.LIVE_CASES if case.live_runner == "repl")
    assert run_e2e._live_cleanup_status(case, {"checks": {"teardown: stacks deleted": True}}) == "completed"
    assert run_e2e._live_cleanup_status(case, {"checks": {"teardown: stacks deleted": False}}) == "failed"
    assert run_e2e._live_cleanup_status(case, None) == "unverified"
    legacy = next(case for case in run_e2e.LIVE_CASES if case.live_runner == "legacy_a2a")
    assert run_e2e._live_cleanup_status(legacy, {"cleanup_status": "completed"}) == "completed"
    smoke = next(case for case in run_e2e.LIVE_CASES if case.live_runner == "smoke")
    assert run_e2e._live_cleanup_status(smoke, None) == "not-needed"


def test_live_requires_complete_credential_source_and_write_opt_in(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run_e2e.parse_args(["--suite", "live", "--credential-source-dir", str(tmp_path)])
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    with pytest.raises(SystemExit):
        run_e2e.parse_args(["--suite", "live", "--credential-source-dir", str(tmp_path)])
    args = run_e2e.parse_args(
        ["--suite", "live", "--credential-source-dir", str(tmp_path), "--allow-cloud-write"]
    )
    assert len(run_e2e.select_cases(args)) == len(run_e2e.LIVE_CASES)


def test_live_accepts_llm_only_source_with_cloud_helper(tmp_path: Path) -> None:
    for name in (".credentials.yml", "settings.yml"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    helper = tmp_path / "helper.py"
    helper.write_text("", encoding="utf-8")
    args = run_e2e.parse_args([
        "--suite", "live", "--credential-source-dir", str(tmp_path),
        "--cloud-credential-helper", str(helper), "--cloud-credential-python", sys.executable,
        "--allow-cloud-write",
    ])
    assert len(run_e2e.select_cases(args)) == len(run_e2e.LIVE_CASES)


def test_cloud_helper_failure_is_classified_without_exposing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("import sys\nprint('secret-fixture', file=sys.stderr)\nsys.exit(1)\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", "settings.yml"):
        (source / name).write_text("fixture", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    report = tmp_path / "report"

    code = run_e2e.main([
        "--suite", "live", "--case", "a2a-recovery-scenario1", "--jobs", "1",
        "--run-dir", str(report), "--credential-source-dir", str(source),
        "--cloud-credential-helper", str(helper), "--allow-cloud-write",
    ])

    assert code == 1
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    assert summary["cases"][0]["error"].startswith("cloud credential setup failed")
    assert "secret-fixture" not in (report / "report.md").read_text(encoding="utf-8")


def test_live_public_summary_drops_notes_error_and_paths() -> None:
    summary = {
        "case_id": "A01", "scenario": "example", "status": "failed", "cleanup_status": "completed",
        "checks": {"safe check": False, "unsafe key": "secret"},
        "notes": ["sensitive token"], "error": "sensitive token", "run_dir": "/private/path",
    }
    public = run_e2e._public_live_summary(summary)
    assert public == {
        "case_id": "A01", "scenario": "example", "status": "failed", "cleanup_status": "completed",
        "checks": {"safe check": False},
    }


def test_live_public_summary_keeps_safe_failure_location_only() -> None:
    public = run_e2e._public_live_summary({
        "status": "failed",
        "error": "RuntimeError: private provider response sk-fixture",
        "error_type": "RuntimeError",
        "error_site": "scripts/pipeline/e2e/selling_solution_first/run_scenarios.py:1752",
    })
    assert public is not None
    assert public["error_type"] == "RuntimeError"
    assert public["error_site"] == "scripts/pipeline/e2e/selling_solution_first/run_scenarios.py:1752"
    assert "sk-fixture" not in json.dumps(public)
    unsafe = run_e2e._public_live_summary({
        "error_type": "RuntimeError: sk-fixture",
        "error_site": "scripts/../../secrets.py:1",
    })
    assert unsafe is not None
    assert "error_type" not in unsafe
    assert "error_site" not in unsafe


def test_live_public_summary_keeps_only_safe_cleanup_diagnostic() -> None:
    public = run_e2e._public_live_summary({
        "cleanup_diagnostic": {
            "error_type": "UnretryableError",
            "stage": "list_stacks",
            "sdk_code": "Throttling",
            "failure_count": 1,
            "remaining_count": 0,
            "stack_id": "sensitive-stack-id",
        },
    })
    assert public is not None
    assert public["cleanup_diagnostic"] == {
        "error_type": "UnretryableError",
        "stage": "list_stacks",
        "sdk_code": "Throttling",
        "failure_count": 1,
        "remaining_count": 0,
    }


def test_live_public_summary_keeps_only_known_a2a_states() -> None:
    public = run_e2e._public_live_summary({
        "a2a_states": ["TASK_STATE_WORKING", "TASK_STATE_FAILED", "TASK_STATE_PRIVATE_sk-fixture"],
        "a2a_phase": "next-turn",
        "a2a_event_count": 1,
        "a2a_text_present": True,
        "a2a_raw_line_count": 2,
        "a2a_response_content_type": "text/event-stream",
        "jsonrpc_error_code": -32602,
        "terminal_markers": ["resource_selection_resume_invalid", "sk-fixture"],
        "terminal_message_present": True,
        "control_state": {
            "present": True, "task_matches": True, "phase": "running",
            "release_ready": False, "input_handoff_ready": False,
            "execution_status": "working", "stream_available": True, "blocker_count": 2,
            "subprocess_tracking": True, "active_subprocess_tools": 0,
            "external_operation_count": 1, "revision_settled": True,
            "backup_status": "not_requested",
            "unsafe": "sk-fixture",
        },
    })
    assert public is not None
    assert public["a2a_states"] == ["TASK_STATE_WORKING", "TASK_STATE_FAILED"]
    assert public["a2a_phase"] == "next-turn"
    assert public["a2a_event_count"] == 1
    assert public["a2a_text_present"] is True
    assert public["a2a_raw_line_count"] == 2
    assert public["a2a_response_content_type"] == "text/event-stream"
    assert public["jsonrpc_error_code"] == -32602
    assert public["terminal_markers"] == ["resource_selection_resume_invalid"]
    assert public["terminal_message_present"] is True
    assert public["control_state"] == {
        "present": True, "task_matches": True, "phase": "running",
        "release_ready": False, "input_handoff_ready": False,
        "execution_status": "working", "stream_available": True, "blocker_count": 2,
        "subprocess_tracking": True, "active_subprocess_tools": 0,
        "external_operation_count": 1, "revision_settled": True,
        "backup_status": "not_requested",
    }
    assert "sk-fixture" not in json.dumps(public)


def test_live_public_summary_classifies_a2a_terminal_without_text() -> None:
    public = run_e2e._public_live_summary({
        "error": "RuntimeError: A2A task entered unexpected terminal state TASK_STATE_FAILED: "
        "ValueError: Rate limit exceeded for secret sk-fixture",
    })
    assert public is not None
    assert public["terminal_category"] == "rate_limit"
    assert public["terminal_exception"] == "ValueError"
    assert public["terminal_message_present"] is True
    assert "sk-fixture" not in json.dumps(public)


def test_live_public_summary_keeps_fixed_recovery_failure_stage_only() -> None:
    public = run_e2e._public_live_summary({
        "error_type": "TimeoutError",
        "error_site": "scripts/a2a/e2e/run_recovery_scenarios.py:1820",
        "failure_stage": "post_rollback_confirmation",
        "abort_reason": "private token sk-fixture",
    })
    assert public is not None
    assert public["error_type"] == "TimeoutError"
    assert public["failure_stage"] == "post_rollback_confirmation"
    assert "sk-fixture" not in json.dumps(public)
    assert "failure_stage" not in run_e2e._public_live_summary({"failure_stage": "sk-fixture"})


def test_live_public_summary_extracts_only_safe_chinese_terminal_terms() -> None:
    public = run_e2e._public_live_summary({
        "error": "RuntimeError: A2A task entered unexpected terminal state TASK_STATE_FAILED: "
        "恢复会话失败，凭证 sk-fixture 不可用",
    })
    assert public is not None
    assert set(public["terminal_terms"]) == {"恢复", "会话", "失败", "凭证", "不可用"}
    assert "sk-fixture" not in json.dumps(public)


def test_live_public_summary_recognizes_fixed_snake_case_code() -> None:
    public = run_e2e._public_live_summary({
        "error": "RuntimeError: A2A task entered unexpected terminal state TASK_STATE_FAILED: "
        "pipeline_identity_mismatch; private value sk-fixture",
    })
    assert public is not None
    assert public["terminal_code"] == "pipeline_identity_mismatch"
    assert {"pipeline", "identity", "mismatch"} <= set(public["terminal_terms"])
    assert "sk-fixture" not in json.dumps(public)


def test_live_a2a_terminal_evidence_keeps_only_fixed_fields(tmp_path: Path) -> None:
    event = {
        "metadata": {"iac_code": {"pipeline": {
            "eventType": "pipeline_failed",
            "data": {
                "errorSummary": "ValueError: Rate limit exceeded; token=sk-fixture",
                "errorDetails": {"type": "ValueError", "traceback": "secret fixture"},
            },
        }}},
    }
    (tmp_path / "failed.events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    evidence = run_e2e._live_a2a_terminal_evidence(tmp_path)

    assert evidence == {
        "pipeline_failed_event": "observed",
        "terminal_inner_type": "ValueError",
        "terminal_category": "rate_limit",
        "terminal_terms": ["error"],
        "pipeline_events": ["pipeline_failed"],
    }
    assert "sk-fixture" not in json.dumps(evidence)


def test_live_a2a_terminal_evidence_reads_persistent_journal(tmp_path: Path) -> None:
    journal = tmp_path / "config" / "projects" / "session" / "pipeline" / "a2a-events.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({
        "events": [
            {"eventType": "step_started"},
            {"eventType": "pipeline_failed", "data": {
                "errorSummary": "TimeoutError: private provider payload sk-fixture",
                "errorDetails": {"type": "TimeoutError"},
            }},
        ],
    }) + "\n", encoding="utf-8")

    evidence = run_e2e._live_a2a_terminal_evidence(tmp_path)

    assert evidence == {
        "pipeline_failed_event": "observed",
        "terminal_inner_type": "TimeoutError",
        "terminal_category": "timeout",
        "terminal_terms": ["provider", "error"],
        "pipeline_events": ["step_started", "pipeline_failed"],
    }
    assert "sk-fixture" not in json.dumps(evidence)


def test_live_a2a_terminal_evidence_reports_step_failure_without_raw_error(tmp_path: Path) -> None:
    event = {
        "metadata": {"iac_code": {"pipeline": {
            "eventType": "step_failed",
            "data": {
                "errorSummary": "TimeoutError: provider timed out; token=sk-fixture",
                "errorDetails": {"type": "TimeoutError", "traceback": "private fixture"},
            },
        }}},
    }
    (tmp_path / "failed.events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    evidence = run_e2e._live_a2a_terminal_evidence(tmp_path)

    assert evidence == {
        "step_failed_event": "observed",
        "step_failure_inner_type": "TimeoutError",
        "step_failure_category": "timeout",
        "step_failure_terms": ["provider", "error"],
        "pipeline_events": ["step_failed"],
    }
    assert "sk-fixture" not in json.dumps(evidence)


def test_live_a2a_progress_evidence_ignores_untrusted_event_names(tmp_path: Path) -> None:
    events = [
        {"metadata": {"iac_code": {"pipeline": {"eventType": "input_required"}}}},
        {"metadata": {"iac_code": {"pipeline": {"eventType": "private-token-sk-fixture"}}}},
    ]
    (tmp_path / "turn.events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    assert run_e2e._live_a2a_terminal_evidence(tmp_path) == {"pipeline_events": ["input_required"]}


def test_live_public_summary_keeps_only_safe_watchdog_fields() -> None:
    summary = {
        "passed": False,
        "watchdog": {
            "state": "waiting_for_input", "confidence": 0.93,
            "waitingFor": "pipeline completed", "elapsedSeconds": 125.33,
            "action": "early_abort", "cue": "ask_question", "raw": "secret-fixture-value",
        },
        "progress": {
            "candidate_selection_ready": 2, "user_input_received": 1,
            "ros_deploy_used": 1, "pipeline_completed_early_exit": 1,
            "stack_progress_create_complete": 1, "cleanup_ledger_found": 0,
            "cleanup_failure_route_conflict": 1,
            "cleanup_failure_route_conflict_after_rollback": 0,
            "private-token-sk-fixture": 9, "step_started": True,
        },
    }

    public = run_e2e._public_live_summary(summary)

    assert public is not None
    assert public["watchdog"] == {
        "state": "waiting_for_input", "waitingFor": "pipeline completed",
        "elapsedSeconds": 125.3, "action": "early_abort", "cue": "ask_question",
    }
    assert public["progress"] == {
        "candidate_selection_ready": 2, "user_input_received": 1,
        "ros_deploy_used": 1, "pipeline_completed_early_exit": 1,
        "stack_progress_create_complete": 1, "cleanup_ledger_found": 0,
        "cleanup_failure_route_conflict": 1,
        "cleanup_failure_route_conflict_after_rollback": 0,
    }
    assert "secret-fixture-value" not in json.dumps(public)
    assert run_e2e._public_live_summary({
        "watchdog": {**summary["watchdog"], "waitingFor": "secret: sk-fixture"}
    }) == {
        "case_id": None, "scenario": None, "status": "failed", "cleanup_status": None, "checks": {},
    }


def test_live_audit_note_allowlist_excludes_provider_data() -> None:
    assert run_e2e.SAFE_LIVE_AUDIT_NOTE.fullmatch(
        "credential audit: source=cloud; location=logs; suffix=log"
    )
    assert not run_e2e.SAFE_LIVE_AUDIT_NOTE.fullmatch(
        "credential audit: source=cloud; location=logs; suffix=log; secret=unit-secret-value"
    )


def test_nested_contract_failure_details_are_reported() -> None:
    checks, notes = run_e2e._failure_details(
        {"passed": False, "scenarios": [{"checks": {"provider request observed": False}, "notes": ["missing request"]}]}
    )
    assert checks == ["provider request observed"]
    assert notes == ["missing request"]


def test_child_environment_removes_cloud_and_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALIYUN_ACCESS_KEY_ID", "fake-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-secret")
    monkeypatch.setenv("IAC_CODE_API_KEY", "fake-secret")
    monkeypatch.setenv("AKLESS_BOOTSTRAP_TOKEN", "fake-secret")
    monkeypatch.setenv("IAC_CODE_E2E_PROVIDER_CAPTURE", "inherited-fixture")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://telemetry.example.com")
    user_id = "iac_user_e2e_" + "a" * 32
    env = run_e2e._case_env(tmp_path, run_e2e.EXECUTION_CASES[0], user_id)
    assert "ALIYUN_ACCESS_KEY_ID" not in env
    assert "OPENAI_API_KEY" not in env
    assert "IAC_CODE_API_KEY" not in env
    assert "AKLESS_BOOTSTRAP_TOKEN" not in env
    assert "IAC_CODE_E2E_PROVIDER_CAPTURE" not in env
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert env["IAC_CODE_CONFIG_DIR"] == str(tmp_path / "config")
    assert env["IAC_CODE_TELEMETRY_E2E_USER_ID"] == user_id
    assert "IAC_CODE_TELEMETRY_LOCAL_ONLY" not in env
    local_env = run_e2e._case_env(tmp_path, run_e2e.FAST_CASES[0], user_id)
    assert local_env["IAC_CODE_TELEMETRY_LOCAL_ONLY"] == "1"
    assert sum(case.script in run_e2e.LOCAL_TELEMETRY_SCRIPTS for case in run_e2e.CASES) == 49


def test_e2e_settings_id_is_preserved_or_generated(tmp_path: Path) -> None:
    settings = tmp_path / "settings.yml"
    settings.write_text('{"userID": "iac_user_regular", "activeProvider": "dashscope"}', encoding="utf-8")
    generated = run_e2e._prepare_e2e_user_id(settings)
    assert generated.startswith("iac_user_e2e_")
    assert generated in settings.read_text(encoding="utf-8")
    assert run_e2e._prepare_e2e_user_id(settings) == generated


def test_timeout_writes_failure_report_without_hanging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("hung", "hang.py", (), 1, "full")
    result = run_e2e.run_case(case, tmp_path / "report")
    assert result["status"] == "timeout"
    assert result["durationSeconds"] < 8
    run_e2e._write_reports(tmp_path / "report", [result], result["durationSeconds"])
    summary = json.loads((tmp_path / "report" / "summary.json").read_text(encoding="utf-8"))
    assert summary["failedCount"] == 1
    assert "硬超时" in (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    assert ET.parse(tmp_path / "report" / "junit.xml").find(".//failure") is not None
    result["live"] = True
    result["cleanupStatus"] = "unverified"
    assert "清理结果未验证" in run_e2e._reason(result)


def test_summary_failure_keeps_failed_checks_and_log_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fail.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "d = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "summary = {'passed': False, 'checks': {'step one': False}}\n"
        "(d / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')\n"
        "print('fixture failure')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("failed", "fail.py", (), 5, "full")
    result = run_e2e.run_case(case, tmp_path / "report")
    assert result["status"] == "failed"
    assert result["failedChecks"] == ["step one"]
    run_e2e._write_reports(tmp_path / "report", [result], result["durationSeconds"])
    page = (tmp_path / "report" / "report.html").read_text(encoding="utf-8")
    markdown = (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    machine_summary = json.loads((tmp_path / "report" / "summary.json").read_text(encoding="utf-8"))
    assert "runs/failed/stdout.log" in page
    assert "step one" in page
    assert "· 失败 ·" in page
    assert "| 失败 |" in markdown
    assert machine_summary["cases"][0]["status"] == "failed"
    assert sys.executable in result["command"]
    assert os.path.isfile(tmp_path / "report" / "runs" / "failed" / "ci-result.json")


def test_unexpected_case_exception_still_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> dict[str, object]:
        raise ValueError("broken fixture")

    monkeypatch.setattr(run_e2e, "run_case", fail)
    assert run_e2e.main(["--suite", "fast", "--run-dir", str(tmp_path)]) == 1
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["failedCount"] == len(run_e2e.FAST_CASES)
    assert (tmp_path / "report.md").is_file()


@pytest.mark.parametrize("runner", ["selector", "repl"])
def test_live_adapter_uses_isolated_credentials_and_sanitized_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str
) -> None:
    script = tmp_path / "fake_live.py"
    script.write_text(
        "import argparse, json, os, yaml\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--run-dir', type=Path, required=True)\n"
        "p.add_argument('--source-config-dir', type=Path, required=True)\n"
        "args, _ = p.parse_known_args()\n"
        "if args.run_dir.name.startswith('scenario-'):\n"
        "    args.run_dir.mkdir(parents=True, exist_ok=False)\n"
        "assert (args.source_config_dir / '.credentials.yml').read_text(encoding='utf-8') == 'fixture-secret'\n"
        "assert yaml.safe_load((args.source_config_dir / 'settings.yml').read_text("
        "encoding='utf-8'))['userID'] == os.environ['IAC_CODE_TELEMETRY_E2E_USER_ID']\n"
        "(args.run_dir / 'summary.json').write_text("
        "json.dumps({'passed': True, 'checks': {'ok': True}}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (source / name).write_text("fixture-secret", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("selector-smoke" if runner == "selector" else "repl-smoke", "fake_live.py", (),
                        5, "live", live_runner=runner)

    result = run_e2e.run_case(case, tmp_path / "report", source)

    assert result["status"] == "passed"
    assert result["command"] == [case.name]
    assert "fixture-secret" not in json.dumps(result)
    assert (tmp_path / "report" / "runs" / case.name / "config" / ".credentials.yml").is_file()
    if runner == "repl":
        scenarios = list((tmp_path / "report" / "runs" / case.name).glob("scenario-*"))
        assert len(scenarios) == 1
        assert len(scenarios[0].name.removeprefix("scenario-")) == 32


def test_smoke_adapter_has_model_config_but_no_cloud_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "smoke.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "run_dir = Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "config = Path(os.environ['IAC_CODE_CONFIG_DIR'])\n"
        "assert (config / '.credentials.yml').read_text(encoding='utf-8') == 'model-fixture'\n"
        "assert (config / 'settings.yml').is_file()\n"
        "assert not (config / '.cloud-credentials.yml').exists()\n"
        "assert Path(os.environ['HOME']).is_relative_to(run_dir)\n"
        "assert Path(os.environ['USERPROFILE']).is_relative_to(run_dir)\n"
        "assert '--allow-real-cloud' not in sys.argv\n"
        "(run_dir / 'summary.json').write_text("
        "json.dumps({'passed': True, 'checks': {'ok': True}}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / ".credentials.yml").write_text("model-fixture", encoding="utf-8")
    (source / ".cloud-credentials.yml").write_text("cloud-fixture", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("smoke-vpc-fixture", "smoke.py", (), 5, "live", live_runner="smoke")

    result = run_e2e.run_case(case, tmp_path / "report", source)

    assert result["status"] == "passed"
    assert result["cleanupStatus"] == "not-needed"
    assert "cloud-fixture" not in json.dumps(result)


@pytest.mark.parametrize("runner", ["repl", "selling", "canary"])
def test_cloud_helper_generates_per_case_sts_without_leaking_bootstrap_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import os, pathlib, sys\n"
        "assert sys.argv[1] == 'cloud'\n"
        "assert os.environ['AKLESS_BOOTSTRAP_TOKEN'] == 'bootstrap-fixture'\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "path.write_text('temporary-sts', encoding='utf-8')\n",
        encoding="utf-8",
    )
    script = tmp_path / "live.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "source = pathlib.Path(sys.argv[sys.argv.index('--source-config-dir') + 1])\n"
        if runner in {"repl", "canary"} else
        "import json, os, pathlib, sys\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "source = pathlib.Path(sys.argv[sys.argv.index('--credential-source-dir') + 1])\n",
        encoding="utf-8",
    )
    with script.open("a", encoding="utf-8") as stream:
        stream.write(
            "assert 'AKLESS_BOOTSTRAP_TOKEN' not in os.environ\n"
            "assert 'DASHSCOPE_API_KEY' not in os.environ\n"
            "assert (source / '.cloud-credentials.yml').read_text(encoding='utf-8') == 'temporary-sts'\n"
            "run_dir.mkdir(parents=True, exist_ok=True)\n"
            "(run_dir / 'summary.json').write_text(json.dumps({'passed': True}), encoding='utf-8')\n"
        )
    source = tmp_path / "source"
    source.mkdir()
    (source / ".credentials.yml").write_text("model-key", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setenv("AKLESS_BOOTSTRAP_TOKEN", "bootstrap-fixture")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "model-key")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("akless-" + runner, "live.py", (), 5, "live", live_runner=runner)

    result = run_e2e.run_case(case, tmp_path / "report", source, helper, Path(sys.executable))

    assert result["status"] == "passed"
    assert "bootstrap-fixture" not in json.dumps(result)
    if runner in {"selling", "canary"}:
        assert (tmp_path / "report" / "runs" / case.name / "credential-source" / ".cloud-credentials.yml").is_file()


def test_long_live_case_refreshes_cloud_file_while_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "count = path.parent / 'refresh-count'\n"
        "value = int(count.read_text(encoding='utf-8')) + 1 if count.exists() else 1\n"
        "count.write_text(str(value), encoding='utf-8')\n"
        "path.write_text('sts-' + str(value), encoding='utf-8')\n",
        encoding="utf-8",
    )
    script = tmp_path / "long.py"
    script.write_text(
        "import json, pathlib, sys, time\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "time.sleep(0.4)\n"
        "(run_dir / 'summary.json').write_text(json.dumps({'passed': True}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", "settings.yml"):
        (source / name).write_text("fixture", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_e2e, "CLOUD_REFRESH_SECONDS", 0.1)
    case = run_e2e.Case("akless-refresh", "long.py", (), 5, "live", live_runner="legacy_a2a")

    result = run_e2e.run_case(case, tmp_path / "report", source, helper)

    assert result["status"] == "passed"
    count = tmp_path / "report" / "runs" / case.name / "config" / "refresh-count"
    assert int(count.read_text(encoding="utf-8")) >= 2


def test_legacy_a2a_hard_timeout_starts_independent_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "hang_with_stack.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "(run_dir / 'owned-stacks.json').write_text('fixture', encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    cleanup = tmp_path / "scripts" / "a2a" / "e2e" / "cleanup_owned_stacks.py"
    cleanup.parent.mkdir(parents=True)
    cleanup.write_text(
        "import pathlib, sys\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "(run_dir / 'cleanup-called').write_text('yes', encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (source / name).write_text("fixture", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case(
        "legacy-timeout", "hang_with_stack.py", (), 1, "live", cloud_write=True,
        cleanup_grace=0, live_runner="legacy_a2a",
    )

    result = run_e2e.run_case(case, tmp_path / "report", source)

    assert result["status"] == "timeout"
    assert result["cleanupStatus"] == "completed"
    assert (tmp_path / "report" / "runs" / case.name / "cleanup-called").read_text(encoding="utf-8") == "yes"
