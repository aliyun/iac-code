#!/usr/bin/env python3
"""Run the 13 ECS retention groups through real LLM + HTTP/SSE A2A only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.ci.model_pool import TEXT_MODELS  # noqa: E402
from scripts.ci.run_e2e import RETENTION_CASES  # noqa: E402

ROUTES = tuple(case.name for case in RETENTION_CASES if "-route-" in case.name)
GROUPS = {
    "A01": ROUTES + ("ecs-retention-purchase-routing", "ecs-retention-noncloud-purchase"),
    "A02": ROUTES,
    "A03": ("ecs-retention-selection-recovery",),
    "A04": ("wiki-auto-p19-a2a", "ecs-retention-sizing"),
    "A05": ("promotional-offer-followup-a2a",),
    "A06": ("ecs-retention-n29-swas",),
    "A07": ("ecs-retention-n56-price-chain", "plan-cost-refresh-a2a"),
    "A08": ("ecs-retention-n59-price-chain",),
    "A09": ("coupon-budget-a2a",),
    "A10": ("ecs-retention-pricing-normal", "ecs-retention-api-aliases"),
    "A11": ("ssf-step1-followup-a2a",),
    "A12": ("ssf-step2-followup-a2a",),
    "A13": ("ecs-retention-context-boundaries", "wiki-auto-handoff-a2a"),
}
CASES = tuple(dict.fromkeys(name for names in GROUPS.values() for name in names))
STABILITY_CASES = frozenset({
    "wiki-auto-p19-a2a", "ssf-step1-followup-a2a", "ssf-step2-followup-a2a",
    "ecs-retention-context-boundaries", "wiki-auto-handoff-a2a",
})


def business_hashes():
    return {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (REPO / "src").rglob("*") if path.is_file() and "__pycache__" not in path.parts
            and path.suffix in {".py", ".md", ".yaml", ".yml", ".json"}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-source-dir", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--case", choices=CASES, action="append", help="Limit a documented runner-fix retest")
    parser.add_argument("--model", choices=TEXT_MODELS, action="append")
    parser.add_argument("--stability-rounds", type=int, choices=(1, 3), default=3)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    models, cases = args.model or TEXT_MODELS, args.case or CASES
    rounds = [cases] + [tuple(case for case in cases if case in STABILITY_CASES)] * (args.stability_rounds - 1)
    if args.list:
        print(json.dumps({"groups": GROUPS, "cases": len(cases), "models": list(models),
                          "model_case_runs": sum(map(len, rounds)) * len(models)}, ensure_ascii=False, indent=2))
        return 0
    if not args.run_dir or not args.credential_source_dir:
        parser.error("--run-dir and --credential-source-dir are required")
    args.run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    source = args.credential_source_dir.expanduser().resolve()
    durable = os.environ.get("IAC_CODE_E2E_CLOUD_SOURCE")
    if not durable:
        parser.error("IAC_CODE_E2E_CLOUD_SOURCE must name the durable test OAuth source")
    common = args.run_dir / "cloud-source/.cloud-credentials.yml"
    snapshot = business_hashes()
    (args.run_dir / "manifest.json").write_text(json.dumps({
        "groups": GROUPS, "cases_by_round": rounds, "models": list(models), "effort": "low",
        "fallback": False, "scenario_retries": 0, "business_sha256": snapshot,
        "test_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (REPO / "scripts/ci").glob("*.py")},
    }, indent=2), encoding="utf-8")
    stop = threading.Event()

    def refresh():
        with (args.run_dir / "credential-helper.log").open("a", encoding="utf-8") as log:
            return subprocess.call([sys.executable, "scripts/ci/cloud_test_credentials.py", "cloud", "--output",
                                    str(common)], cwd=REPO, env={**os.environ, "IAC_CODE_E2E_CLOUD_SOURCE": durable},
                                   stdout=log, stderr=subprocess.STDOUT)

    if refresh():
        parser.error("Cloud credential preflight failed; see private credential-helper.log")

    def refresh_loop():
        while not stop.wait(600):
            refresh()

    refresher = threading.Thread(target=refresh_loop, daemon=True)
    refresher.start()

    def execute(model: str) -> dict:
        directory = args.run_dir / model
        directory.mkdir(mode=0o700, exist_ok=True)
        completed = []
        for number, names in enumerate(rounds, 1):
            if not names:
                continue
            run_dir = directory / f"round-{number}"
            run_dir.mkdir(mode=0o700, exist_ok=True)
            command = [sys.executable, str(REPO / "scripts/ci/run_e2e.py"), "--suite", "live",
                       "--run-dir", str(run_dir), "--credential-source-dir", str(source),
                       "--cloud-credential-helper", str(REPO / "scripts/ci/cloud_test_credentials.py"),
                       "--cloud-credential-python", sys.executable, "--text-model", model,
                       "--jobs", "2", "--text-model-jobs", "2", "--retries", "0"]
            for name in names:
                command.extend(("--case", name))
            env = {**os.environ, "IAC_CODE_E2E_CLOUD_SOURCE": str(common.parent),
                   "IAC_CODE_ROS_AGENT_AWARENESS": "true",
                   "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_ONLY"}
            with (run_dir / "driver.log").open("w", encoding="utf-8") as log:
                code = subprocess.call(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
            completed.append({"round": number, "returncode": code, "summary": str(run_dir / "summary.json")})
            (directory / "progress.json").write_text(json.dumps(completed, indent=2), encoding="utf-8")
        result = {"model": model, "returncode": int(any(row["returncode"] for row in completed)), "rounds": completed}
        print(json.dumps(result), flush=True)
        return result

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            completed = list(pool.map(execute, models))
    finally:
        stop.set()
        refresher.join()
        common.unlink(missing_ok=True)
        (args.run_dir / "business-verification.json").write_text(
            json.dumps({"business_code_unchanged": snapshot == business_hashes()}), encoding="utf-8",
        )
    (args.run_dir / "matrix.json").write_text(json.dumps(completed, indent=2), encoding="utf-8")
    return int(any(row["returncode"] != 0 for row in completed))


if __name__ == "__main__":
    raise SystemExit(main())
