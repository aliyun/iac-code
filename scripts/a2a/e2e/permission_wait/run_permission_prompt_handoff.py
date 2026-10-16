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
    "mixed-confirmation-followup": "test_permission_confirmation_continues_batch_and_accepts_next_prompt",
}
EXTRA_NODES = {
    **{
        "protocol-" + protocol: "tests/a2a/test_permission_protocol_e2e.py::"
        "test_real_provider_sends_canceled_results_before_new_prompt[{}]".format(protocol)
        for protocol in ("chat", "responses", "anthropic")
    },
    **{
        "handoff-" + fault: "tests/a2a/test_permission_handoff_process_e2e.py::"
        "test_incomplete_handoff_rejects_cold_worker_and_local_reply_can_retry[{}]".format(fault)
        for fault in ("runtime-close", "backup-failure")
    },
    "handoff-crash-before-commit": "tests/a2a/test_permission_handoff_process_e2e.py::"
    "test_crash_after_shared_backup_before_handoff_commit_cannot_authorize_takeover",
    "handoff-concurrent-workers": "tests/a2a/test_permission_handoff_process_e2e.py::"
    "test_two_http_workers_claim_once_and_old_owner_cannot_overwrite_replacement",
    **{
        "real-pipeline-" + decision: "tests/a2a/test_permission_pipeline_engine_e2e.py::"
        "test_real_pipeline_permission_recovery_completes_step_and_accepts_next_prompt[{}]".format(decision)
        for decision in ("allow_once", "deny")
    },
    "artifact-edit-restart": "tests/a2a/test_artifact_edit_e2e.py::"
    "test_edited_template_artifact_content_and_metadata_survive_restart",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS.keys() | EXTRA_NODES.keys()), required=True)
    parser.add_argument("--mode", choices=("same-process", "after-restart"), default="same-process")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    repo_root = Path(__file__).resolve().parents[4]
    os.chdir(repo_root)
    junit_path = run_dir / "pytest.xml"
    if args.scenario in EXTRA_NODES:
        node_id = EXTRA_NODES[args.scenario]
    else:
        mode = "mixed-deny-" + args.mode if args.scenario == "mixed-confirmation-followup" else args.mode
        node_id = "tests/a2a/test_permission_prompt_e2e.py::{}[{}]".format(SCENARIOS[args.scenario], mode)
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
