#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.aliyun.e2e_contract_audit import (  # noqa: E402
    audit_aliyun_result_contract,
    find_latest_aliyun_tool_result,
)
from scripts.observability.local_observe.e2e_audit import (  # noqa: E402
    ObserveCapture,
    audit_provider_attempts,
)
from scripts.repl.e2e.deterministic_openai_server import (  # noqa: E402
    FIXTURE_MODEL,
    PIPELINE_PROMPT_MARKER,
    DeterministicOpenAIServer,
)
from scripts.repl.e2e.run_contract_scenarios import (  # noqa: E402
    _has_request_metrics,
    _terminal_count,
    _write_pty_artifacts,
    _write_settings,
)
from scripts.repl.e2e.run_pipeline_scenarios import (  # noqa: E402
    CANDIDATE_SELECTION_PATTERNS,
    CANDIDATE_SELECTION_READY_PATTERNS,
    REPL_INPUT_READY_PATTERNS,
    ReplPty,
)

SCENARIO = "e2-selling-contract-resume"
HEADER_SENTINEL = "e2e-internal-header-value"
TELEMETRY_MODEL = "other"
EXPECTED_ATTEMPTS = 8
EXPECTED_BODY = {"RequestId": "request-e2e-validatetemplate", "Success": True}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic selling-pipeline contract E2E scenario.")
    parser.add_argument("--run-dir", default="")
    parser.add_argument(
        "--run-root",
        default=str(Path(tempfile.gettempdir()) / "iac-code-repl-e2e-runs" / "contract"),
    )
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--stream-timeout", type=float, default=180.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = _run_dir(args)
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    config_dir = run_dir / "config"
    config_dir.mkdir(exist_ok=True)
    provider_capture_path = run_dir / "provider-requests.jsonl"
    aliyun_capture_path = run_dir / "aliyun-transport.jsonl"

    checks: dict[str, bool] = {}
    notes: list[str] = []
    manifest: dict[str, Any] = {
        "scenario": SCENARIO,
        "entry": "repl",
        "mode": "pipeline",
        "pipeline": "selling",
        "provider": "openai_compatible",
        "model": FIXTURE_MODEL,
        "expected_provider_attempts": EXPECTED_ATTEMPTS,
    }

    observe = ObserveCapture(run_dir / "telemetry").start()
    provider = DeterministicOpenAIServer(provider_capture_path).start()
    first: ReplPty | None = None
    second: ReplPty | None = None
    try:
        _write_settings(config_dir, provider.base_url)
        env = _child_env(config_dir=config_dir, observe=observe, aliyun_capture_path=aliyun_capture_path)
        pty_args = SimpleNamespace(
            python=args.python,
            timeout=args.timeout,
            stream_timeout=args.stream_timeout,
            candidate_selection_ready_timeout=args.timeout,
            terminal_height=40,
            terminal_width=140,
            permission_prompt_response="enter",
        )

        first = ReplPty(args=pty_args, run_dir=run_dir, cwd=workspace, env=env)
        first.spawn()
        _expect_input_ready(first, "initial pipeline input", args.timeout)
        first.sendline("在阿里云杭州创建一个 VPC。" + PIPELINE_PROMPT_MARKER)
        first.expect_any(
            ("E2E_PIPELINE_REFUSAL",),
            description="deterministic refusal before complete_step nudge",
            timeout=args.stream_timeout,
        )
        _expect_candidate_selection(first, args, "candidate selection before resume")
        checks["selling pipeline reached candidate selection"] = True
        checks["refusal branch recovered through complete_step nudge"] = "E2E_PIPELINE_REFUSAL" in first.transcript

        records = observe.wait_for(
            lambda value: (
                len(provider.requests()) >= EXPECTED_ATTEMPTS
                and _terminal_count(value) >= EXPECTED_ATTEMPTS
                and _has_request_metrics(value)
            ),
            timeout=args.stream_timeout,
        )
        sidecar_path, sidecar = _latest_pipeline_sidecar(config_dir)
        session_path, tool_result = find_latest_aliyun_tool_result(config_dir)
        session_id = sidecar_path.parent.parent.name
        manifest.update(
            {
                "session_id": session_id,
                "session_path": str(session_path),
                "pipeline_sidecar": str(sidecar_path),
            }
        )
        checks["pipeline sidecar is waiting for input"] = sidecar.get("status") == "waiting_input"
        checks["pipeline identity persisted"] = sidecar.get("pipeline_name") == "selling"
        checks["delegated ROS result rendered"] = _has_delegated_rendering(tool_result)

        first.terminate(force=True)
        checks["first process killed at persisted waiting point"] = True
        _write_pty_artifacts(first, run_dir, "epoch-1")

        second = ReplPty(args=pty_args, run_dir=run_dir, cwd=workspace, env=env)
        second.spawn(extra_args=["--continue"])
        _expect_candidate_selection(second, args, "candidate selection after resume")
        checks["candidate selection replayed after resume"] = True
        checks["resume did not call provider again"] = len(provider.requests()) == EXPECTED_ATTEMPTS
        resumed_sidecar_path, resumed_sidecar = _latest_pipeline_sidecar(config_dir)
        resumed_session_path, resumed_tool_result = find_latest_aliyun_tool_result(config_dir)
        checks["resume selected same session"] = (
            resumed_sidecar_path == sidecar_path and resumed_session_path == session_path
        )
        checks["resumed pipeline remains waiting for selection"] = resumed_sidecar.get("status") == "waiting_input"
        second.terminate()
        _write_pty_artifacts(second, run_dir, "epoch-2")

        provider_requests = provider.requests()
        checks["provider request count"] = len(provider_requests) == EXPECTED_ATTEMPTS
        checks["transport called delegated ValidateTemplate once"] = _transport_actions(aliyun_capture_path) == [
            "ValidateTemplate"
        ]
        contract = audit_aliyun_result_contract(
            expected_body=EXPECTED_BODY,
            tool_result_content=tool_result["content"],
            tool_result_metadata=tool_result.get("metadata") or {},
            resumed_content=resumed_tool_result["content"],
            resumed_metadata=resumed_tool_result.get("metadata") or {},
            public_payloads=[provider_requests, first.transcript, second.transcript],
            forbidden_values=[HEADER_SENTINEL],
            output_path=run_dir / "aliyun-contract-audit.json",
        )
        checks["delegated Aliyun result contract"] = contract["passed"]

        telemetry = audit_provider_attempts(
            records,
            expected_attempts=EXPECTED_ATTEMPTS,
            expected_provider="openai",
            expected_model=TELEMETRY_MODEL,
            expected_span_attributes={"iac_code.mode": "pipeline", "pipeline_name": "selling"},
        )
        attribution = _audit_pipeline_attribution(records)
        (run_dir / "pipeline-attribution-audit.json").write_text(
            json.dumps(attribution, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        checks["provider attempt telemetry"] = telemetry["passed"]
        checks["pipeline step and candidate attribution"] = attribution["passed"]
    except Exception as exc:
        notes.append(f"{type(exc).__name__}: {exc}")
    finally:
        if first is not None and first.transcript:
            _write_pty_artifacts(first, run_dir, "epoch-1")
        if second is not None and second.transcript:
            _write_pty_artifacts(second, run_dir, "epoch-2")
        for pty in (second, first):
            if pty is not None:
                pty.terminate()
        provider.stop()
        records = observe.stop()
        manifest["telemetry_record_count"] = len(records)
        (run_dir / "scenario-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    passed = bool(checks) and all(checks.values()) and not notes
    summary = {"scenario": SCENARIO, "passed": passed, "checks": checks, "notes": notes, "run_dir": str(run_dir)}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


def _run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser().resolve()
    return (Path(args.run_root).expanduser().resolve() / f"{int(time.time())}-{os.getpid()}").resolve()


def _child_env(*, config_dir: Path, observe: ObserveCapture, aliyun_capture_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    bootstrap = Path(__file__).resolve().parents[2] / "aliyun" / "e2e_fixture_bootstrap"
    env.update(observe.env)
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(item for item in [str(bootstrap), env.get("PYTHONPATH", "")] if item),
            "IAC_CODE_CONFIG_DIR": str(config_dir),
            "IAC_CODE_MODE": "pipeline",
            "IAC_CODE_PIPELINE_NAME": "selling",
            "IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING": "0",
            "IAC_CODE_PROVIDER": "openai_compatible",
            "IAC_CODE_MODEL": FIXTURE_MODEL,
            "IAC_CODE_API_KEY": "e2e-provider-key",
            "IAC_CODE_E2E_ALIYUN_TRANSPORT_FIXTURE": "1",
            "IAC_CODE_E2E_ALIYUN_CAPTURE": str(aliyun_capture_path),
            "IAC_CODE_E2E_ALIYUN_HEADER_SENTINEL": HEADER_SENTINEL,
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "e2e-access-key-id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "e2e-access-key-secret",
            "ALIBABA_CLOUD_REGION_ID": "cn-hangzhou",
        }
    )
    return env


def _expect_input_ready(pty: ReplPty, description: str, timeout: float) -> None:
    pty.expect_any((r"❯",), description=f"{description} prompt", timeout=timeout)
    pty.expect_any(REPL_INPUT_READY_PATTERNS, description=f"{description} ready", timeout=timeout)


def _expect_candidate_selection(pty: ReplPty, args: argparse.Namespace, description: str) -> None:
    pty.expect_any(CANDIDATE_SELECTION_PATTERNS, description=description, timeout=args.stream_timeout)
    pty.expect_optional(
        CANDIDATE_SELECTION_READY_PATTERNS,
        description=f"{description} controls",
        timeout=min(args.timeout, 3.0),
    )
    pty.expect_any(REPL_INPUT_READY_PATTERNS, description=f"{description} input", timeout=args.timeout)


def _latest_pipeline_sidecar(config_dir: Path) -> tuple[Path, dict[str, Any]]:
    paths = list((config_dir / "projects").rglob("pipeline/meta.yaml"))
    if not paths:
        raise AssertionError("no pipeline sidecar was persisted")
    path = max(paths, key=lambda item: item.stat().st_mtime_ns)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("pipeline sidecar is not a mapping")
    return path, payload


def _transport_actions(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["action"] for line in path.read_text(encoding="utf-8").splitlines() if line]


def _has_delegated_rendering(tool_result: dict[str, Any]) -> bool:
    rendering = (tool_result.get("metadata") or {}).get("_iac_code_tool_render") or {}
    return bool(rendering.get("result_compact")) and rendering.get("render_verbose_result_in_transcript") is True


def _audit_pipeline_attribution(records: list[dict[str, Any]]) -> dict[str, Any]:
    spans = [
        record
        for record in records
        if record.get("kind") == "span" and str(record.get("name") or "").startswith("chat ")
    ]
    scopes = [record.get("attributes") or {} for record in spans]
    required = [
        {"step_id": "intent_parsing"},
        {"step_id": "architecture_planning"},
        {
            "parent_step_id": "evaluate_candidates",
            "sub_pipeline_name": "evaluate_candidate",
            "sub_step_id": "template_generating",
            "candidate_index": 0,
        },
        {
            "parent_step_id": "evaluate_candidates",
            "sub_pipeline_name": "evaluate_candidate",
            "sub_step_id": "cost_estimating",
            "candidate_index": 0,
        },
        {"step_id": "confirm_and_select"},
    ]
    missing = [expected for expected in required if not any(_contains(scope, expected) for scope in scopes)]
    nudges = [
        record
        for record in records
        if record.get("kind") == "log"
        and record.get("name") == "iac.pipeline.step.nudged"
        and (record.get("attributes") or {}).get("step_id") == "intent_parsing"
    ]
    return {"passed": not missing and bool(nudges), "missing_scopes": missing, "nudge_count": len(nudges)}


def _contains(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


if __name__ == "__main__":
    raise SystemExit(main())
