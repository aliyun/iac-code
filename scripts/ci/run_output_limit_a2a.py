#!/usr/bin/env python3
"""Expose output-limit A2A process cases through the shared CI runner."""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCENARIOS = {
    **{
        "text-" + protocol: "test_text_continues_after_output_limit[{}]".format(protocol)
        for protocol in ("chat", "responses", "anthropic")
    },
    **{
        "tool-" + protocol: "test_truncated_tool_turn_is_discarded[{}]".format(protocol)
        for protocol in ("chat", "responses", "anthropic")
    },
    "exhausted": "test_repeated_output_limit_fails_with_bounded_requests",
    "empty": "test_output_limit_without_visible_text_fails_immediately",
    "pipeline": "test_pipeline_does_not_complete_after_output_limit",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(Path(__file__).resolve().parents[2])
    junit_path = run_dir / "pytest.xml"
    node_id = "tests/a2a/test_output_limit_e2e.py::" + SCENARIOS[args.scenario]
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
        "checks": {"A2A output-limit scenario passes without skipping": passed},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
