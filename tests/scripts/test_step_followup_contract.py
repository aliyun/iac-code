"""Contract runner counts remain strict after adding parametrized recovery cases."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.ci import run_step_followup_contract as runner


@pytest.mark.parametrize("scenario,count,result_tag,exit_code,passed", [
    ("step1", 9, None, 0, True),
    ("step1", 8, None, 0, False),
    ("step1", 10, None, 0, False),
    ("step1", 0, None, 0, False),
    ("step1", 9, "skipped", 0, False),
    ("step1", 9, "failure", 0, False),
    ("step1", 9, "error", 0, False),
    ("step1", 9, None, 1, False),
    ("step2", 4, None, 0, True),
    ("handoff-reply", 3, None, 0, True),
])
def test_contract_requires_all_expected_tests_to_pass(
    tmp_path, monkeypatch, scenario, count, result_tag, exit_code, passed,
):
    run_dir = tmp_path / "contract"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["runner", "--scenario", scenario, "--run-dir", str(run_dir)])

    def fake_pytest(args):
        assert args[args.index("-k") + 1] == ("handoff_reply" if scenario == "handoff-reply" else scenario)
        path = Path(next(value.removeprefix("--junitxml=") for value in args if value.startswith("--junitxml=")))
        suite = ET.Element("testsuite")
        for index in range(count):
            case = ET.SubElement(suite, "testcase", name=f"test_{index}")
            if result_tag and index == 0:
                ET.SubElement(case, result_tag)
        ET.ElementTree(suite).write(path, encoding="utf-8")
        return exit_code

    monkeypatch.setattr(runner.pytest, "main", fake_pytest)
    assert runner.main() == (0 if passed else 1)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["test_count"] == count
    assert summary["passed"] is passed
