"""Failed-case reruns preserve acceptance, isolation, cleanup and first-run evidence."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET

import pytest

from scripts.ci import run_e2e
from scripts.ci.model_pool import ModelAssignment


@pytest.mark.parametrize("mode,retries,count,passed", [
    ("retry-pass", 1, 2, True), ("fail", 1, 2, False),
    ("pass", 1, 1, True), ("retry-pass", 0, 1, False),
    ("false-check", 1, 2, False),
])
def test_real_subprocess_reruns_once_and_keeps_both_results(tmp_path, monkeypatch, mode, retries, count, passed):
    script = tmp_path / "scenario.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "d = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        f"passed = {mode!r} == 'pass' or ({mode!r} == 'retry-pass' and d.name == 'retry-1')\n"
        "assert 'e2e' in os.environ['IAC_CODE_TELEMETRY_E2E_USER_ID']\n"
        f"(d / 'summary.json').write_text(json.dumps({{'passed': passed or {mode!r} == 'false-check', "
        "'checks': {'original acceptance': passed}}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    case = run_e2e.Case("fixture", script.name, (), 5, "full")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_e2e, "select_cases", lambda _: [case])
    report = tmp_path / "report"
    assert run_e2e.main(["--run-dir", str(report), "--retries", str(retries)]) == int(not passed)
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    result, = summary["cases"]
    assert summary["total"] == 1 and summary["passedCount"] == int(passed)
    assert summary["retriedCount"] == int(count == 2)
    assert result["attemptCount"] == count
    assert result["passedAfterRetry"] == (passed and count == 2)
    assert result["durationSeconds"] == round(sum(a["durationSeconds"] for a in result["attempts"]), 2)
    assert len(list(report.glob("runs/**/ci-result.json"))) == 1
    assert len(list(report.glob("runs/**/attempt-result.json"))) == count
    for attempt in result["attempts"]:
        assert json.loads((report / attempt["resultFile"]).read_text(encoding="utf-8")) == attempt
        raw = json.loads((report / attempt["artifacts"] / "summary.json").read_text(encoding="utf-8"))
        assert raw["checks"]["original acceptance"] == (attempt["status"] == "passed")
    junit = ET.parse(report / "junit.xml")
    assert len(junit.findall(".//testcase")) == 1
    assert (junit.find(".//failure") is None) == passed
    if mode != "pass":
        assert result["attempts"][0]["failedChecks"] == ["original acceptance"]
        for filename in ("report.md", "report.html", "junit.xml"):
            text = (report / filename).read_text(encoding="utf-8")
            assert "original acceptance" in text and "定向重跑" in text
            assert "--retries 0" in text
        assert result["rerun"]["parameters"] == {"suite": "full", "case": "fixture", "jobs": 1, "retries": 0}


def test_timed_out_process_is_stopped_before_isolated_retry(tmp_path, monkeypatch):
    script = tmp_path / "timeout.py"
    script.write_text(
        "import json, pathlib, sys, time\n"
        "d = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "if d.name != 'retry-1': time.sleep(60)\n"
        "(d / 'summary.json').write_text(json.dumps({'passed': True, "
        "'checks': {'original acceptance': True}}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    case = run_e2e.Case("fixture", script.name, (), 1, "full", cleanup_grace=0)
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_e2e, "select_cases", lambda _: [case])
    # This fixture has no descendants. Signal its PID directly because some
    # local execution hosts prohibit killpg even for the test's own session.
    stopped = []

    def stop(process, grace):
        process.kill()
        stopped.append(process.wait(timeout=5))

    monkeypatch.setattr(run_e2e, "_stop_tree", stop)
    report = tmp_path / "report"
    assert run_e2e.main(["--run-dir", str(report), "--retries", "1"]) == 0
    result, = json.loads((report / "summary.json").read_text(encoding="utf-8"))["cases"]
    first, second = result["attempts"]
    assert first["status"] == "timeout" and first["returnCode"] is not None
    assert stopped == [first["returnCode"]]
    assert first["durationSeconds"] < 8
    assert second["status"] == "passed" and result["passedAfterRetry"]


def test_unconfirmed_timeout_does_not_launch_overlapping_retry(tmp_path, monkeypatch):
    case = run_e2e.Case("fixture", "unused", (), 1, "full")
    args = argparse.Namespace(run_dir=tmp_path, retries=1, credential_source_dir=None,
                              cloud_credential_helper=None, cloud_credential_python=None)
    calls = []

    def execute(*positional, **kwargs):
        calls.append(kwargs)
        result = run_e2e._runner_exception_result(case, None, RuntimeError("termination failed"))
        result.update(status="timeout", returnCode=None)
        return result

    monkeypatch.setattr(run_e2e, "run_case", execute)
    result = run_e2e._run_case_attempts(case, None, args, tmp_path / "registry", 123.0)
    assert len(calls) == 1
    assert result["status"] == "timeout" and result["attemptCount"] == 1


@pytest.mark.parametrize("first_cleanup,second_cleanup,passed", [
    ("completed", "completed", True), ("completed", "not-needed", True),
    ("failed", "completed", False), ("unverified", "completed", False),
])
@pytest.mark.parametrize("model,multimodal", [("glm-5.3-prime", False), ("qwen3.8-flash", True)])
def test_live_rerun_retains_model_and_cannot_hide_incomplete_cleanup(
    tmp_path, monkeypatch, first_cleanup, second_cleanup, passed, model, multimodal,
):
    case = run_e2e.Case("fixture", "unused", (), 5, "live", multimodal=multimodal)
    assignment = ModelAssignment(model, multimodal)
    args = argparse.Namespace(run_dir=tmp_path, retries=1, credential_source_dir=None,
                              cloud_credential_helper=None, cloud_credential_python=None)
    calls = []

    def execute(*positional, **kwargs):
        calls.append((positional, kwargs))
        assert positional[5] is assignment
        result = run_e2e._runner_exception_result(case, assignment, RuntimeError("private-secret"))
        result.update(error="", status="passed" if len(calls) == 2 else "failed", durationSeconds=2,
                      cleanupStatus=second_cleanup if len(calls) == 2 else first_cleanup,
                      failedChecks=[] if len(calls) == 2 else ["original acceptance"])
        return result

    monkeypatch.setattr(run_e2e, "run_case", execute)
    result = run_e2e._run_case_attempts(case, assignment, args, tmp_path / "registry", 123.0)
    assert len(calls) == 2 and calls[1][1]["retry"] is True
    assert calls[0][0][-1] == calls[1][0][-1]
    assert calls[0][1]["network_fixture_before"] == calls[1][1]["network_fixture_before"] == 123.0
    assert (result["status"] == "passed") == passed
    assert result["passedAfterRetry"] == passed
    assert result["cleanupStatus"] == first_cleanup
    assert result["rerun"]["parameters"]["case_models"] == "fixture=" + model
    assert "--case-model fixture=" + model in result["rerun"]["localCommand"]
    assert all(a["model"] == model for a in result["attempts"])
    assert all(a["thinkingBudget"] == assignment.thinking_budget for a in result["attempts"])
    assert "private-secret" not in json.dumps(result)
    if not passed:
        assert "所有执行尝试的资源清理均已完成" in result["failedChecks"]


def test_exception_is_retried_but_live_payload_is_not_published(tmp_path, monkeypatch):
    case = run_e2e.Case("fixture", "unused", (), 5, "live")
    args = argparse.Namespace(run_dir=tmp_path, retries=1, credential_source_dir=None,
                              cloud_credential_helper=None, cloud_credential_python=None)
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("private-credential-payload")

    monkeypatch.setattr(run_e2e, "run_case", fail)
    result = run_e2e._run_case_attempts(case, None, args, tmp_path / "registry", 123.0)
    assert len(calls) == 2 and result["status"] == "failed"
    assert all(a["error"] == "runner exception; inspect CI job log" for a in result["attempts"])
    for filename in tmp_path.glob("runs/**/*.json"):
        assert "private-credential-payload" not in filename.read_text(encoding="utf-8")


def test_more_than_one_retry_is_rejected():
    with pytest.raises(SystemExit):
        run_e2e.parse_args(["--retries", "2"])
