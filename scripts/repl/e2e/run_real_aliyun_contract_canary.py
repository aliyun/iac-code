#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
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
from scripts.repl.e2e.run_pipeline_scenarios import DEFAULT_TEXT_MODEL, ReplPty  # noqa: E402

SCENARIO = "e5-real-aliyun-readonly-canary"
PROMPT_MARKER = "[E5_REAL_ALIYUN_READONLY]"
PROMPT = (
    "这是只读 E2E canary。必须且只能调用一次 aliyun_api：product=vpc，version=2016-04-28，"
    "action=DescribeVpcs，params 仅包含 PageSize=10；禁止调用任何写操作。工具返回后简短回答。"
    + PROMPT_MARKER
)
CONFIG_FILES = (".credentials.yml", ".cloud-credentials.yml", "settings.yml")
ENVELOPE_KEYS = frozenset({"body", "status", "headers", "content_type", "content_encoding", "size"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the real LLM + read-only Aliyun API contract canary.")
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--run-dir", default="")
    parser.add_argument(
        "--run-root",
        default=str(Path(tempfile.gettempdir()) / "iac-code-repl-e2e-runs" / "real-canary"),
    )
    parser.add_argument("--source-config-dir", default="")
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--model", default=DEFAULT_TEXT_MODEL)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--stream-timeout", type=float, default=300.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.allow_real_cloud:
        raise SystemExit("Refusing to call real services without --allow-real-cloud")

    run_dir = _run_dir(args)
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    config_dir = run_dir / "config"
    config_dir.mkdir(exist_ok=True)
    source_config_dir = _source_config_dir(args)

    checks: dict[str, bool] = {}
    notes: list[str] = []
    records: list[dict[str, Any]] = []
    pty: ReplPty | None = None
    observe = ObserveCapture(run_dir / "telemetry").start()
    manifest: dict[str, Any] = {
        "scenario": SCENARIO,
        "entry": "repl",
        "mode": "normal",
        "external_dependencies": ["configured_llm_provider", "aliyun_vpc_DescribeVpcs"],
        "read_only_action": "DescribeVpcs",
    }
    try:
        _copy_runtime_config(source_config_dir, config_dir)
        env = _child_env(config_dir=config_dir, observe=observe, model=args.model)
        pty_args = SimpleNamespace(
            python=args.python,
            timeout=args.timeout,
            terminal_height=40,
            terminal_width=140,
            permission_prompt_response="enter",
        )
        pty = ReplPty(args=pty_args, run_dir=run_dir, cwd=workspace, env=env)
        pty.spawn()
        _expect_repl_input_ready(pty, description="initial REPL input", timeout=args.timeout)
        pty.sendline(PROMPT)
        pty.expect_any((r"\[E5_REAL_ALIYUN_READONLY\]",), description="canary input submitted", timeout=args.timeout)
        _expect_repl_input_ready(pty, description="completed canary response", timeout=args.stream_timeout)
        _exit_repl(pty, timeout=args.timeout)
        _write_pty_artifacts(pty, run_dir)

        records = observe.wait_for(
            lambda current: _terminal_count(current) >= 1 and _has_request_metrics(current),
            timeout=args.timeout,
        )
        session_path, tool_result = find_latest_aliyun_tool_result(config_dir)
        tool_uses = _aliyun_tool_uses(session_path)
        manifest["session_id"] = session_path.parent.name
        manifest["session_path"] = str(session_path)
        manifest["provider_attempt_count"] = _terminal_count(records)

        checks["exactly one aliyun_api call"] = len(tool_uses) == 1
        checks["aliyun_api call is the allowed read-only action"] = bool(tool_uses) and all(
            _is_allowed_describe_vpcs(item) for item in tool_uses
        )
        business_body = json.loads(tool_result["content"])
        checks["Aliyun result is a business object"] = _is_business_vpc_body(business_body)

        contract = audit_aliyun_result_contract(
            expected_body=business_body,
            tool_result_content=tool_result["content"],
            tool_result_metadata=tool_result.get("metadata") or {},
            public_payloads=[pty.transcript],
            output_path=run_dir / "aliyun-contract-audit.json",
        )
        checks["Aliyun result metadata and public boundary"] = contract["passed"]
        telemetry = audit_provider_attempts(
            records,
            expected_attempts=None,
            expected_span_attributes={"iac_code.mode": "normal"},
            output_path=run_dir / "telemetry-audit.json",
        )
        checks["provider attempt telemetry"] = telemetry["passed"]
        providers, models = _provider_identities(records)
        manifest["providers"] = providers
        manifest["models"] = models
    except Exception as exc:
        notes.append(f"{type(exc).__name__}: {exc}")
    finally:
        if pty is not None and pty.transcript:
            _write_pty_artifacts(pty, run_dir)
        if pty is not None:
            pty.terminate()
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


def _source_config_dir(args: argparse.Namespace) -> Path:
    value = args.source_config_dir or os.environ.get("IAC_CODE_CONFIG_DIR") or "~/.iac-code"
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _copy_runtime_config(source: Path, destination: Path) -> None:
    missing = [name for name in CONFIG_FILES if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"missing runtime config file(s) in {source}: {', '.join(missing)}")
    for name in CONFIG_FILES:
        target = destination / name
        shutil.copy2(source / name, target)
        target.chmod(0o600)


def _child_env(*, config_dir: Path, observe: ObserveCapture, model: str = DEFAULT_TEXT_MODEL) -> dict[str, str]:
    env = os.environ.copy()
    env.update(observe.env)
    env.update(
        {
            "PYTHONUTF8": "1",
            "IAC_CODE_CONFIG_DIR": str(config_dir),
            "IAC_CODE_MODE": "normal",
            "IAC_CODE_MODEL": model,
        }
    )
    for key in list(env):
        if key.startswith("IAC_CODE_E2E_"):
            env.pop(key, None)
    return env


def _aliyun_tool_uses(session_path: Path) -> list[dict[str, Any]]:
    uses: list[dict[str, Any]] = []
    for line in session_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        entry = json.loads(line)
        content = entry.get("content") if isinstance(entry, dict) else None
        if not isinstance(content, list):
            continue
        uses.extend(
            block.get("input") or {}
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "aliyun_api"
        )
    return [item for item in uses if isinstance(item, dict)]


def _is_allowed_describe_vpcs(tool_input: dict[str, Any]) -> bool:
    params = tool_input.get("params")
    return (
        str(tool_input.get("product") or "").casefold() == "vpc"
        and tool_input.get("version") == "2016-04-28"
        and tool_input.get("action") == "DescribeVpcs"
        and params == {"PageSize": 10}
    )


def _is_business_vpc_body(value: Any) -> bool:
    if not isinstance(value, dict) or ENVELOPE_KEYS.intersection(value):
        return False
    return isinstance(value.get("RequestId"), str) and isinstance(value.get("Vpcs"), dict)


def _provider_identities(records: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    started = [
        record.get("attributes") or {}
        for record in records
        if record.get("kind") == "log" and record.get("name") == "iac.api.request.started"
    ]
    providers = sorted({str(item.get("provider")) for item in started if item.get("provider")})
    models = sorted({str(item.get("model")) for item in started if item.get("model")})
    return providers, models


def _exit_repl(pty: ReplPty, *, timeout: float) -> None:
    pty.sendline("/exit")
    child = pty.child
    if child is None:
        raise RuntimeError("REPL child is missing")
    child.expect(pexpect.EOF, timeout=timeout)
    pty.terminate()


def _expect_repl_input_ready(pty: ReplPty, *, description: str, timeout: float) -> None:
    pty.expect_any((r"❯",), description=f"{description} prompt", timeout=timeout)
    pty.expect_any((r"\x1b\[>4;2m",), description=f"{description} ready", timeout=timeout)


def _write_pty_artifacts(pty: ReplPty, run_dir: Path) -> None:
    (run_dir / "transcript.log").write_text(pty.transcript, encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in pty.events), encoding="utf-8"
    )


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
