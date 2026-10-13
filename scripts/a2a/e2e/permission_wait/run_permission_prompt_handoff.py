#!/usr/bin/env python3
"""Expose the HTTP permission-batch E2Es through the shared CI runner."""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCENARIOS = {
    "prompt-supersede": "test_new_prompt_cancels_unexecuted_permission_batch",
    "confirmation-followup": "test_permission_confirmation_continues_batch_and_accepts_next_prompt",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--mode", choices=("same-process", "after-restart"), required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    repo_root = Path(__file__).resolve().parents[4]
    os.chdir(repo_root)
    junit_path = run_dir / "pytest.xml"
    node_id = "tests/a2a/test_permission_prompt_e2e.py::{}[{}]".format(SCENARIOS[args.scenario], args.mode)
    exit_code = pytest.main(
        [
            "-q",
            "--tb=short",
            "-o",
            "addopts=",
            node_id,
            "--basetemp=" + str(run_dir / "pytest"),
            "--junitxml=" + str(junit_path),
        ]
    )
    try:
        cases = ET.parse(junit_path).findall(".//testcase")
        completed = len(cases) == 1 and not any(
            case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")
        )
    except (OSError, ET.ParseError):
        completed = False
    passed = exit_code == 0 and completed
    summary = {
        "passed": passed,
        "scenario": args.scenario,
        "mode": args.mode,
        "checks": {"HTTP permission scenario passes without skipping": passed},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
