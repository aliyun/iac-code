#!/usr/bin/env python3
"""
Windows Compatibility Verification Script — Headless Mode
==========================================================

Runs ``iac-code -p "..." --output-format json`` in non-interactive mode,
verifying that headless mode can complete a conversation round and produce output.

Usage:
    python scripts/headless/smoke/test_headless_vpc.py

Prerequisites:
    - iac-code installed (pip install -e . or pip install iac-code)
    - LLM credentials configured (ran iac-code and executed /auth, or set env vars)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROMPT = "帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12，只输出JSON模板内容，不要解释"

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"
HEADLESS_WORKSPACE = "."
TEXT_DIAGNOSTICS: dict[str, int | bool] = {}
OUTPUT_DIAGNOSTICS: dict[str, dict] = {}


def _record_output_diagnostics(output_format: str, result: subprocess.CompletedProcess) -> None:
    """Record output shape and error categories without exporting model output."""
    stdout = result.stdout.strip()
    detail = {
        "exit_code": result.returncode,
        "stdout_length": len(stdout),
        "stderr_length": len(result.stderr),
    }
    if output_format != "text":
        detail.update(tool_error_count=0, permission_denial_count=0)
    error_text = result.stderr

    def record_tool_error(value):
        if isinstance(value, dict) and value.get("is_error") is True:
            detail["tool_error_count"] += 1
            text = json.dumps(value.get("result", ""), ensure_ascii=False).lower()
            if "permission" in text or "denied" in text or "拒绝" in text:
                detail["permission_denial_count"] += 1

    if output_format == "json":
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            detail.update(json_valid=False, parse_error_line=exc.lineno, parse_error_column=exc.colno)
        else:
            detail.update(json_valid=True, json_object=isinstance(data, dict))
            if isinstance(data, dict):
                usage = data.get("usage")
                text = data.get("text")
                detail.update(
                    text_field_present="text" in data,
                    text_length=len(text) if isinstance(text, str) else 0,
                    usage_field_present="usage" in data,
                    usage_object=isinstance(usage, dict),
                    input_tokens_integer=isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int),
                    error_present=bool(data.get("error")),
                )
                error_text += "\n" + str(data.get("error", ""))
                tools = data.get("tool_uses")
                if isinstance(tools, list):
                    for tool in tools:
                        record_tool_error(tool)
    elif output_format == "stream-json":
        events: dict[str, int] = {}
        stops: dict[str, int] = {}
        parse_errors = 0
        lines = stdout.splitlines()
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            kind = event.get("type") if isinstance(event, dict) else None
            if kind not in {"text_delta", "thinking_delta", "message_end", "error", "tool_use_start",
                            "tool_use_end", "tool_result", "tool_input_delta", "permission_request"}:
                kind = "other"
            events[kind] = events.get(kind, 0) + 1
            if kind == "message_end":
                stop = event.get("stop_reason")
                if stop not in {"end_turn", "tool_use", "max_turns", "stop_sequence", "max_tokens"}:
                    stop = "other"
                stops[stop] = stops.get(stop, 0) + 1
            elif kind == "error":
                error_text += "\n" + str(event.get("error", ""))
            elif kind == "tool_result":
                record_tool_error(event)
        detail.update(line_count=len([line for line in lines if line.strip()]), parse_error_count=parse_errors,
                      event_counts=events, stop_reason_counts=stops)
    error_lower = error_text.lower()
    category = "none" if not error_lower.strip() else "other"
    for label, markers in (
        ("rate_limit", ("ratelimit", "rate limit", "rate_limit", "throttl", "too many requests")),
        ("authentication", ("invalid_api_key", "invalid api key", "unauthorized", "authenticationerror")),
        ("timeout", ("timeout", "timed out")),
        ("connection", ("connectionerror", "connection error", "connection reset", "connecterror")),
        ("max_turns", ("max_turns", "max turns")),
    ):
        if any(marker in error_lower for marker in markers):
            category = label
            break
    detail["error_category"] = category
    OUTPUT_DIAGNOSTICS[output_format] = detail


def run_headless(output_format: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "-p", PROMPT,
        "--output-format", output_format,
        "--max-turns", "20",
        # Generation skills save and inspect local templates. Keep the normal
        # headless approval flow, including its cloud-write rejection guard.
        "--permission-mode", "accept_edits",
    ]
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    print(f"{INFO} Running command: {' '.join(cmd[:6])}... --output-format {output_format}")
    start = time.time()
    result = subprocess.run(
        cmd,
        cwd=HEADLESS_WORKSPACE,
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    elapsed = time.time() - start
    print(f"{INFO} Elapsed: {elapsed:.1f}s, exit code: {result.returncode}")
    return result


def test_text_output():
    print("\n=== Test 1: headless text output ===")
    result = run_headless("text")
    _record_output_diagnostics("text", result)
    TEXT_DIAGNOSTICS.update(
        text_exit_code=result.returncode,
        text_output_length=len(result.stdout.strip()),
        text_has_vpc_marker=any(
            keyword in result.stdout.upper() for keyword in ("VPC", "VPCNAME", "CIDRBLOCK", "ROSTEMPLATE")
        ),
    )

    if result.returncode != 0:
        print(f"{FAIL} Process exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stdout is empty")
        return False

    print(f"{INFO} Output length: {len(stdout)} chars")
    print(f"{INFO} First 200 chars: {stdout[:200]}")

    checks = {
        "has output content": len(stdout) > 10,
        "contains VPC-related content": any(
            kw in stdout.upper() for kw in ["VPC", "VPCNAME", "CIDRBLOCK", "ROSTEMPLATE"]
        ),
    }

    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print(f"{PASS if all_pass else FAIL} text output test {'passed' if all_pass else 'failed'}")
    return all_pass


def test_json_output():
    print("\n=== Test 2: headless JSON output ===")
    result = run_headless("json")
    _record_output_diagnostics("json", result)

    if result.returncode != 0:
        print(f"{FAIL} Process exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stdout is empty")
        return False

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        print(f"{FAIL} JSON parse failed: {e}")
        print(f"{INFO} Raw output first 300 chars: {stdout[:300]}")
        return False

    if not isinstance(data, dict):
        print(f"{FAIL} JSON output is not an object")
        return False
    usage = data.get("usage")
    checks = {
        "JSON parsed successfully": True,
        "contains text field": "text" in data,
        "text is non-empty": bool(data.get("text")),
        "contains usage field": "usage" in data,
        "usage contains input_tokens": isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int),
    }
    OUTPUT_DIAGNOSTICS["json"]["checks"] = checks

    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    if data.get("text"):
        print(f"{INFO} text first 200 chars: {data['text'][:200]}")
    if data.get("usage"):
        print(f"{INFO} usage: {data['usage']}")

    print(f"{PASS if all_pass else FAIL} JSON output test {'passed' if all_pass else 'failed'}")
    return all_pass


def test_stream_json_output():
    print("\n=== Test 3: headless stream-json output ===")
    result = run_headless("stream-json")
    _record_output_diagnostics("stream-json", result)

    if result.returncode != 0:
        print(f"{FAIL} Process exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stdout is empty")
        return False

    lines = [line for line in stdout.split("\n") if line.strip()]
    print(f"{INFO} Total {len(lines)} NDJSON lines")

    parsed_count = 0
    type_counts: dict[str, int] = {}
    parse_errors = 0
    for line in lines:
        try:
            obj = json.loads(line)
            parsed_count += 1
            event_type = obj.get("type", "unknown")
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
        except json.JSONDecodeError:
            parse_errors += 1

    checks = {
        "at least 3 event lines": len(lines) >= 3,
        "all lines are valid JSON": parse_errors == 0,
        "contains text_delta events": type_counts.get("text_delta", 0) > 0,
        "contains message_end event": type_counts.get("message_end", 0) > 0,
    }
    OUTPUT_DIAGNOSTICS["stream-json"]["checks"] = checks

    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print(f"{INFO} Event type distribution: {type_counts}")
    print(f"{PASS if all_pass else FAIL} stream-json output test {'passed' if all_pass else 'failed'}")
    return all_pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    if args.run_dir is not None:
        args.run_dir.mkdir(parents=True, exist_ok=True)
        global HEADLESS_WORKSPACE
        HEADLESS_WORKSPACE = str(args.run_dir / "workspace")
        Path(HEADLESS_WORKSPACE).mkdir(exist_ok=True)
    print("=" * 60)
    print("  iac-code Headless Mode Windows Compatibility Test")
    print("=" * 60)

    results = {
        "text output": test_text_output(),
        "JSON output": test_json_output(),
        "stream-json output": test_stream_json_output(),
    }

    print("\n" + "=" * 60)
    print("  Test Results Summary")
    print("=" * 60)
    all_pass = True
    for name, passed in results.items():
        status = PASS if passed else FAIL
        print(f"  {status} {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print(f"{PASS} All headless tests passed!")
    else:
        print(f"{FAIL} Some tests failed, check output above")

    if args.run_dir is not None:
        (args.run_dir / "summary.json").write_text(
            json.dumps(
                {"passed": all_pass, "checks": results,
                 "diagnostics": {**TEXT_DIAGNOSTICS, "headless_output": OUTPUT_DIAGNOSTICS}},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
