#!/usr/bin/env python3
"""Run the deterministic P18 selection regression through the shared CI entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("same-process", "cold-recovery"), required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(Path(__file__).resolve().parents[3])
    junit_path = run_dir / "pytest.xml"
    node = "tests/pipeline/selling_solution_first/test_candidate_batch_e2e.py::"
    node += f"test_idempotent_outline_survives_selection_resume[{args.mode}]"
    exit_code = pytest.main(
        [
            "-q",
            "--tb=short",
            "-o",
            "addopts=",
            node,
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
        "mode": args.mode,
        "checks": {"unchanged candidate batch survives real pipeline persistence and selection": passed},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
