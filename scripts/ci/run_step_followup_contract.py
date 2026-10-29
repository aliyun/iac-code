#!/usr/bin/env python3
"""Run the offline multi-turn Step1/Step2 contracts via the shared CI dispatcher."""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=("step1", "step2"), required=True)
    args = parser.parse_args()
    args.run_dir = args.run_dir.resolve()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(Path(__file__).resolve().parents[2])
    junit = args.run_dir / "pytest.xml"
    code = pytest.main(
        [
            "-q",
            "--tb=short",
            "-o",
            "addopts=",
            "-k",
            args.scenario,
            "tests/pipeline/selling_solution_first/test_step_followup_e2e.py",
            "--basetemp=" + str(args.run_dir / "pytest"),
            "--junitxml=" + str(junit),
        ]
    )
    try:
        cases = ET.parse(junit).findall(".//testcase")
        expected = 3 if args.scenario == "step1" else 4
        completed = len(cases) == expected and not any(
            case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")
        )
    except (OSError, ET.ParseError):
        cases, completed = [], False
    passed = code == 0 and completed
    summary = {
        "passed": passed,
        "scenario": args.scenario,
        "test_count": len(cases),
        "checks": {"production pipeline multi-turn contract": passed},
    }
    (args.run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
