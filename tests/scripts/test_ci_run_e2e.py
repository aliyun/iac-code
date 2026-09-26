"""Offline tests for the bounded CI E2E runner."""

from __future__ import annotations

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


def test_summary_failure_keeps_failed_checks_and_log_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fail.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "d = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "(d / 'summary.json').write_text(json.dumps({'passed': False, 'checks': {'step one': False}}))\n"
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
