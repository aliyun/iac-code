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

import pexpect

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
    ALIYUN_PROMPT_MARKER,
    FINAL_RESPONSE,
    FIXTURE_MODEL,
    DeterministicOpenAIServer,
)
from scripts.repl.e2e.run_pipeline_scenarios import ReplPty  # noqa: E402

SCENARIO = "e1-normal-contract-resume"
HEADER_SENTINEL = "e2e-internal-header-value"
TELEMETRY_MODEL = "other"
EXPECTED_BODY = {
    "RequestId": "request-e2e-describe-vpcs",
    "PageNumber": 1,
    "PageSize": 10,
    "TotalCount": 1,
    "Vpcs": {
        "Vpc": [
            {
                "VpcId": "vpc-e2e-fixture",
                "VpcName": "contract-e2e",
                "CidrBlock": "172.16.0.0/12",
                "Status": "Available",
            }
        ]
    },
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic REPL contract E2E scenarios.")
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
        "mode": "normal",
        "provider": "openai_compatible",
        "model": FIXTURE_MODEL,
        "expected_provider_attempts": 3,
        "process_epochs": [],
    }

    observe = ObserveCapture(run_dir / "telemetry").start()
    provider = DeterministicOpenAIServer(provider_capture_path).start()
    first: ReplPty | None = None
    second: ReplPty | None = None
    try:
        _write_settings(config_dir, provider.base_url)
        env = _child_env(
            config_dir=config_dir,
            observe=observe,
            aliyun_capture_path=aliyun_capture_path,
        )
        pty_args = SimpleNamespace(
            python=args.python,
            timeout=args.timeout,
            terminal_height=40,
            terminal_width=140,
            permission_prompt_response="enter",
        )

        first = ReplPty(args=pty_args, run_dir=run_dir, cwd=workspace, env=env)
        first.spawn()
        _expect_repl_input_ready(first, description="initial REPL input", timeout=args.timeout)
        first.sendline("请调用 aliyun_api 查询 VPC，只使用工具结果回答。" + ALIYUN_PROMPT_MARKER)
        first.expect_any((r"\[E2E_ALIYUN\]",), description="initial input submitted", timeout=args.timeout)
        first.expect_any((FINAL_RESPONSE,), description="tool round final response", timeout=args.stream_timeout)
        _exit_repl(first, timeout=args.timeout)
        epoch1 = observe.wait_for(
            lambda records: _terminal_count(records) >= 2 and _has_request_metrics(records),
            timeout=args.timeout,
        )
        epoch1_end = len(epoch1)
        _write_pty_artifacts(first, run_dir, "epoch-1")

        session_path, tool_result = find_latest_aliyun_tool_result(config_dir)
        session_id = session_path.parent.name
        manifest["session_id"] = session_id
        manifest["session_path"] = str(session_path)
        manifest["process_epochs"].append({"name": "initial", "record_start": 0, "record_end": epoch1_end})

        second = ReplPty(args=pty_args, run_dir=run_dir, cwd=workspace, env=env)
        second.spawn(extra_args=["--continue"])
        _expect_repl_input_ready(second, description="resumed REPL input", timeout=args.timeout)
        second.sendline("请只回复恢复成功 [E2E_FINAL]")
        second.expect_any((r"\[E2E_FINAL\]",), description="resumed input submitted", timeout=args.timeout)
        second.expect_any((FINAL_RESPONSE,), description="resumed response", timeout=args.stream_timeout)
        if len(provider.requests()) != 3:
            raise AssertionError("resumed response did not produce exactly one new provider request")
        _exit_repl(second, timeout=args.timeout)
        _write_pty_artifacts(second, run_dir, "epoch-2")
        all_records = observe.wait_for(
            lambda records: _terminal_count(records[epoch1_end:]) >= 1 and _has_request_metrics(records[epoch1_end:]),
            timeout=args.timeout,
        )
        epoch2 = all_records[epoch1_end:]
        manifest["process_epochs"].append(
            {"name": "resume", "record_start": epoch1_end, "record_end": len(all_records)}
        )

        resumed_session_path, resumed_tool_result = find_latest_aliyun_tool_result(config_dir)
        checks["resume selected the same session"] = resumed_session_path == session_path
        provider_requests = provider.requests()
        checks["provider observed tool round and resume"] = len(provider_requests) == 3
        checks["transport called DescribeVpcs once"] = _transport_actions(aliyun_capture_path) == ["DescribeVpcs"]
        checks["first transcript rendered final response"] = FINAL_RESPONSE in first.transcript
        checks["resume transcript rendered final response"] = FINAL_RESPONSE in second.transcript

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
        checks["Aliyun result contract"] = contract["passed"]
        telemetry1 = audit_provider_attempts(
            all_records[:epoch1_end],
            expected_attempts=2,
            expected_provider="openai",
            expected_model=TELEMETRY_MODEL,
            expected_span_attributes={"iac_code.mode": "normal"},
        )
        telemetry2 = audit_provider_attempts(
            epoch2,
            expected_attempts=1,
            expected_provider="openai",
            expected_model=TELEMETRY_MODEL,
            expected_span_attributes={"iac_code.mode": "normal"},
        )
        telemetry = {
            "passed": telemetry1["passed"] and telemetry2["passed"],
            "process_epochs": {"initial": telemetry1, "resume": telemetry2},
        }
        (run_dir / "telemetry-audit.json").write_text(
            json.dumps(telemetry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        checks["provider attempt telemetry"] = telemetry["passed"]
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


def _write_settings(config_dir: Path, base_url: str) -> None:
    (config_dir / "settings.yml").write_text(
        "\n".join(
            [
                "activeProvider: openai_compatible",
                "providers:",
                "  openai_compatible:",
                f"    model: {FIXTURE_MODEL}",
                f"    apiBase: {base_url}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _child_env(*, config_dir: Path, observe: ObserveCapture, aliyun_capture_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    bootstrap = Path(__file__).resolve().parents[2] / "aliyun" / "e2e_fixture_bootstrap"
    env.update(observe.env)
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(item for item in [str(bootstrap), env.get("PYTHONPATH", "")] if item),
            "IAC_CODE_CONFIG_DIR": str(config_dir),
            "IAC_CODE_MODE": "normal",
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


def _exit_repl(pty: ReplPty, *, timeout: float) -> None:
    _expect_repl_input_ready(pty, description="REPL exit input", timeout=timeout)
    pty.sendline("/exit")
    child = pty.child
    if child is None:
        raise RuntimeError("REPL child is missing")
    child.expect(pexpect.EOF, timeout=timeout)
    pty.terminate()


def _expect_repl_input_ready(pty: ReplPty, *, description: str, timeout: float) -> None:
    pty.expect_any((r"❯",), description=f"{description} prompt", timeout=timeout)
    pty.expect_any((r"\x1b\[>4;2m",), description=f"{description} ready", timeout=timeout)


def _write_pty_artifacts(pty: ReplPty, run_dir: Path, prefix: str) -> None:
    (run_dir / f"{prefix}.transcript.log").write_text(pty.transcript, encoding="utf-8")
    (run_dir / f"{prefix}.events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in pty.events), encoding="utf-8"
    )


def _transport_actions(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["action"] for line in path.read_text(encoding="utf-8").splitlines() if line]


def _terminal_count(records: list[dict[str, Any]]) -> int:
    return sum(
        record.get("kind") == "log" and record.get("name") in {"iac.api.request.succeeded", "iac.api.request.failed"}
        for record in records
    )


def _has_request_metrics(records: list[dict[str, Any]]) -> bool:
    names = {record.get("name") for record in records if record.get("kind") == "metric"}
    return {"iac.api.request.count", "iac.api.request.duration"}.issubset(names)


if __name__ == "__main__":
    raise SystemExit(main())
