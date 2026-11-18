#!/usr/bin/env python3
"""Expose Wiki process contracts through the existing CI dispatcher."""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCENARIOS = {
    "off": ("test_off",),
    "normal-order": ("test_normal_order",),
    "empty": ("test_empty", "test_pipeline_order[empty]"),
    "pipeline-order": ("test_pipeline_order[hits]", "test_pipeline_clarification_wiki_input"),
    "late-isolation": ("test_late_isolation",),
    "recovery": ("test_recovery", "test_interrupted_recovery", "test_live_sse_reconnect"),
    "cancel-error": ("test_cancel_error",),
    "request": ("test_request",),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(Path(__file__).resolve().parents[2])
    junit = run_dir / "pytest.xml"
    code = pytest.main(
        [
            "-q",
            "--tb=short",
            "-o",
            "addopts=",
            *("tests/a2a/test_wiki_auto_search_e2e.py::" + test for test in SCENARIOS[args.scenario]),
            "--basetemp=" + str(run_dir / "pytest"),
            "--junitxml=" + str(junit),
        ]
    )
    try:
        tests = ET.parse(junit).findall(".//testcase")
        complete = bool(tests) and not any(
            test.find(tag) is not None for test in tests for tag in ("skipped", "failure", "error")
        )
    except (OSError, ET.ParseError):
        tests, complete = [], False
    passed = code == 0 and complete
    report = {
        "passed": passed,
        "scenario": args.scenario,
        "test_count": len(tests),
        "samples": [
            {
                "name": test.get("name"),
                "passed": not any(test.find(tag) is not None for tag in ("skipped", "failure", "error")),
            }
            for test in tests
        ],
        "checks": {"production A2A Wiki contract passes without skipping": passed},
    }
    (run_dir / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
