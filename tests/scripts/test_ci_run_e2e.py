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
    assert len(full) == 41
    assert len(live) == 69
    assert len(run_e2e.CASES) == 110
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
    monkeypatch.setenv("IAC_CODE_E2E_PROVIDER_CAPTURE", "inherited-fixture")
    env = run_e2e._case_env(tmp_path)
    assert "ALIYUN_ACCESS_KEY_ID" not in env
    assert "OPENAI_API_KEY" not in env
    assert "IAC_CODE_API_KEY" not in env
    assert "IAC_CODE_E2E_PROVIDER_CAPTURE" not in env
    assert env["IAC_CODE_CONFIG_DIR"] == str(tmp_path / "config")


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
    assert "runs/failed/stdout.log" in page
    assert "step one" in page
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
        "import argparse, json\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--run-dir', type=Path, required=True)\n"
        "p.add_argument('--source-config-dir', type=Path, required=True)\n"
        "args, _ = p.parse_known_args()\n"
        "if args.run_dir.name.startswith('scenario-'):\n"
        "    args.run_dir.mkdir(parents=True, exist_ok=False)\n"
        "assert (args.source_config_dir / '.credentials.yml').read_text(encoding='utf-8') == 'fixture-secret'\n"
        "(args.run_dir / 'summary.json').write_text("
        "json.dumps({'passed': True, 'checks': {'ok': True}}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (source / name).write_text("fixture-secret", encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("selector-smoke" if runner == "selector" else "repl-smoke", "fake_live.py", (),
                        5, "live", live_runner=runner)

    result = run_e2e.run_case(case, tmp_path / "report", source)

    assert result["status"] == "passed"
    assert result["command"] == [case.name]
    assert "fixture-secret" not in json.dumps(result)
    assert (tmp_path / "report" / "runs" / case.name / "config" / ".credentials.yml").is_file()
