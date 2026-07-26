#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.aliyun.e2e_contract_audit import (  # noqa: E402
    audit_aliyun_result_contract,
    audit_public_payloads,
    find_latest_aliyun_tool_result,
)
from scripts.observability.local_observe.e2e_audit import (  # noqa: E402
    ObserveCapture,
    audit_provider_attempts,
)
from scripts.repl.e2e.deterministic_openai_server import (  # noqa: E402
    FIXTURE_MODEL,
    DeterministicOpenAIServer,
)

HEADER_SENTINEL = "e2e-internal-header-value"
TELEMETRY_MODEL = "other"
SCENARIOS = {
    "e3a-recovery": "fault-after-snapshot",
    "e3b-success": "contract-graceful-success",
    "e3b-cancel": "contract-graceful-cancel",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic A2A contract and telemetry E2E scenarios.")
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--run-dir", default="")
    parser.add_argument(
        "--run-root",
        default=str(Path(tempfile.gettempdir()) / "iac-code-a2a-e2e-runs" / "contract"),
    )
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--server-timeout", type=float, default=45.0)
    parser.add_argument("--stream-timeout", type=float, default=300.0)
    parser.add_argument("--event-timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = _run_dir(args)
    run_dir.mkdir(parents=True, exist_ok=True)
    selected = args.scenario or list(SCENARIOS)
    results = [_run_scenario(args, name, run_dir / name) for name in selected]
    passed = all(result["passed"] for result in results)
    summary = {"passed": passed, "run_dir": str(run_dir), "scenarios": results}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


def _run_scenario(args: argparse.Namespace, scenario: str, run_dir: Path) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    config_dir = run_dir / "config"
    config_dir.mkdir(exist_ok=True)
    provider_capture = run_dir / "provider-requests.jsonl"
    aliyun_capture = run_dir / "aliyun-transport.jsonl"
    checks: dict[str, bool] = {}
    notes: list[str] = []

    observe = ObserveCapture(run_dir / "telemetry").start()
    provider = DeterministicOpenAIServer(provider_capture, response_delay=0.25).start()
    records: list[dict[str, Any]] = []
    try:
        env = _child_env(
            config_dir=config_dir,
            observe=observe,
            provider_capture=provider_capture,
            aliyun_capture=aliyun_capture,
        )
        command = [
            *shlex.split(args.python),
            str(Path(__file__).with_name("run_recovery_scenarios.py")),
            "--scenario",
            SCENARIOS[scenario],
            "--deterministic",
            "--allow-real-cloud",
            "--skip-preflight",
            "--run-dir",
            str(run_dir),
            "--server-cwd",
            str(REPO_ROOT),
            "--provider",
            "openai_compatible",
            "--model",
            FIXTURE_MODEL,
            "--api-base",
            provider.base_url,
            "--initial-prompt",
            "选择一个已有 VPC，创建一个 VSwitch。",
            "--selection-prompt",
            json.dumps(
                {"selected_candidate_name": "Contract VSwitch", "selected_candidate_index": 0},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "--server-timeout",
            str(args.server_timeout),
            "--stream-timeout",
            str(args.stream_timeout),
            "--event-timeout",
            str(args.event_timeout),
        ]
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=args.stream_timeout + 120,
            check=False,
        )
        (run_dir / "contract-runner.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (run_dir / "contract-runner.stderr.log").write_text(completed.stderr, encoding="utf-8")
        base_summary = _load_json(run_dir / "summary.json")
        checks["base A2A scenario passed"] = completed.returncode == 0 and base_summary.get("passed") is True
    except Exception as exc:
        notes.append(f"{type(exc).__name__}: {exc}")
        base_summary = {}
    finally:
        provider.stop()
        records = observe.stop()

    public_payloads = _load_public_payloads(run_dir)
    public_audit = audit_public_payloads(
        public_payloads,
        forbidden_values=[HEADER_SENTINEL],
        output_path=run_dir / "public-payload-audit.json",
    )
    checks["A2A public payload has no internal metadata"] = public_audit["passed"]

    if scenario != "e3b-cancel":
        try:
            _, tool_result = find_latest_aliyun_tool_result(config_dir)
            captured_bodies = _captured_bodies(aliyun_capture)
            actual_body = json.loads(tool_result["content"])
            expected_body = next(body for body in captured_bodies if body == actual_body)
            contract = audit_aliyun_result_contract(
                expected_body=expected_body,
                tool_result_content=tool_result["content"],
                tool_result_metadata=tool_result.get("metadata") or {},
                public_payloads=[public_payloads, provider.requests()],
                forbidden_values=[HEADER_SENTINEL],
                output_path=run_dir / "aliyun-contract-audit.json",
            )
            checks["Aliyun result contract"] = contract["passed"]
        except Exception as exc:
            checks["Aliyun result contract"] = False
            notes.append(f"Aliyun contract: {type(exc).__name__}: {exc}")

    task_id = str(base_summary.get("pipeline_task_id") or "")
    context_id = str(base_summary.get("context_id") or "")
    telemetry = audit_provider_attempts(
        records,
        expected_attempts=len(provider.requests()),
        expected_provider="openai",
        expected_model=TELEMETRY_MODEL,
        output_path=run_dir / "telemetry-audit.json",
    )
    checks["provider attempt telemetry"] = telemetry["passed"]
    checks["provider request observed"] = len(provider.requests()) > 0
    provider_attribution = _audit_pipeline_provider_attribution(records, context_id=context_id)
    (run_dir / "provider-attribution-audit.json").write_text(
        json.dumps(provider_attribution, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    checks["pipeline provider telemetry attribution"] = provider_attribution["passed"]
    attribution = _audit_a2a_attribution(records, task_id=task_id, context_id=context_id)
    (run_dir / "a2a-attribution-audit.json").write_text(
        json.dumps(attribution, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    checks["A2A task and context telemetry attribution"] = attribution["passed"]
    if scenario == "e3a-recovery":
        checks["recovery reused persisted task"] = bool(task_id and context_id)
    if scenario == "e3b-cancel":
        checks["cancel terminal observed"] = any(
            item.get("terminal") == "iac.api.request.failed" for item in telemetry["attempts"]
        )

    manifest = {
        "scenario": scenario,
        "base_scenario": SCENARIOS[scenario],
        "entry": "a2a",
        "mode": "pipeline",
        "pipeline": "selling",
        "task_id": task_id,
        "context_id": context_id,
        "provider": "openai_compatible",
        "model": FIXTURE_MODEL,
        "expected_provider_attempts": len(provider.requests()),
        "telemetry_record_count": len(records),
    }
    (run_dir / "scenario-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = bool(checks) and all(checks.values()) and not notes
    result = {"scenario": scenario, "passed": passed, "checks": checks, "notes": notes, "run_dir": str(run_dir)}
    (run_dir / "contract-summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser().resolve()
    return (Path(args.run_root).expanduser().resolve() / f"{int(time.time())}-{os.getpid()}").resolve()


def _child_env(
    *,
    config_dir: Path,
    observe: ObserveCapture,
    provider_capture: Path,
    aliyun_capture: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    bootstrap = REPO_ROOT / "scripts" / "aliyun" / "e2e_fixture_bootstrap"
    env.update(observe.env)
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(item for item in [str(bootstrap), env.get("PYTHONPATH", "")] if item),
            "IAC_CODE_CONFIG_DIR": str(config_dir),
            "IAC_CODE_MODE": "pipeline",
            "IAC_CODE_PIPELINE_NAME": "selling",
            "IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING": "0",
            "IAC_CODE_API_KEY": "e2e-provider-key",
            "IAC_CODE_E2E_PROVIDER_CAPTURE": str(provider_capture),
            "IAC_CODE_E2E_ALIYUN_TRANSPORT_FIXTURE": "1",
            "IAC_CODE_E2E_ALIYUN_CAPTURE": str(aliyun_capture),
            "IAC_CODE_E2E_ALIYUN_HEADER_SENTINEL": HEADER_SENTINEL,
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "e2e-access-key-id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "e2e-access-key-secret",
            "ALIBABA_CLOUD_REGION_ID": "cn-hangzhou",
        }
    )
    return env


def _load_public_payloads(run_dir: Path) -> list[Any]:
    payloads: list[Any] = []
    paths = [*run_dir.glob("*.events.jsonl"), *run_dir.glob("*.task-*.json"), *run_dir.glob("*.cancel-response.json")]
    for path in sorted(set(paths)):
        if path.suffix == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                if line:
                    payloads.append(json.loads(line))
        else:
            payloads.append(_load_json(path))
    return payloads


def _captured_bodies(path: Path) -> list[Any]:
    if not path.exists():
        return []
    return [json.loads(line).get("response_body") for line in path.read_text(encoding="utf-8").splitlines() if line]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _audit_a2a_attribution(
    records: list[dict[str, Any]],
    *,
    task_id: str,
    context_id: str,
) -> dict[str, Any]:
    matched_logs: list[str] = []
    for record in records:
        if record.get("kind") != "log" or not str(record.get("name") or "").startswith("iac.pipeline."):
            continue
        attributes = record.get("attributes") or {}
        if (
            attributes.get("task_id") == task_id
            and attributes.get("context_id") == context_id
            and attributes.get("pipeline_run_id") == context_id
        ):
            matched_logs.append(str(record.get("name") or ""))
    failures: list[dict[str, Any]] = []
    if not task_id or not context_id:
        failures.append({"check": "task_and_context_identity_present", "task_id": task_id, "context_id": context_id})
    if not matched_logs:
        failures.append({"check": "pipeline_log_correlation", "task_id": task_id, "context_id": context_id})
    return {"passed": not failures, "matched_logs": matched_logs, "failures": failures}


def _audit_pipeline_provider_attribution(
    records: list[dict[str, Any]],
    *,
    context_id: str,
) -> dict[str, Any]:
    pipeline_spans: list[str] = []
    failures: list[dict[str, Any]] = []
    expected = {
        "iac_code.mode": "pipeline",
        "pipeline_name": "selling",
        "pipeline_run_id": context_id,
    }
    for record in records:
        if record.get("kind") != "span" or not str(record.get("name") or "").startswith("chat "):
            continue
        attributes = record.get("attributes") or {}
        if attributes.get("iac_code.mode") != "pipeline":
            continue
        span_id = str(record.get("span_id") or "")
        pipeline_spans.append(span_id)
        for key, value in expected.items():
            if attributes.get(key) != value:
                failures.append(
                    {
                        "span_id": span_id,
                        "check": f"span_attribute:{key}",
                        "expected": value,
                        "actual": attributes.get(key),
                    }
                )
    if not context_id:
        failures.append({"check": "context_id_present", "actual": context_id})
    if not pipeline_spans:
        failures.append({"check": "at_least_one_pipeline_provider_span"})
    return {"passed": not failures, "pipeline_span_ids": pipeline_spans, "failures": failures}


if __name__ == "__main__":
    raise SystemExit(main())
