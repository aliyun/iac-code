#!/usr/bin/env python3
"""
Windows Compatibility Verification Script — Headless Mode
==========================================================

Runs ``iac-code -p "..." --output-format json`` in non-interactive mode,
verifying that headless mode can complete a conversation round and produce output.

Usage:
    python scripts/test_headless_vpc.py

Prerequisites:
    - iac-code installed (pip install -e . or pip install iac-code)
    - LLM credentials configured (ran iac-code and executed /auth, or set env vars)
"""

import json
import os
import subprocess
import sys
import time

PROMPT = "帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12，只输出JSON模板内容，不要解释"

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"


def run_headless(output_format: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "-p", PROMPT,
        "--output-format", output_format,
        "--max-turns", "20",
        "--permission-mode", "bypass_permissions",
    ]
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    print(f"{INFO} Running command: {' '.join(cmd[:6])}... --output-format {output_format}")
    start = time.time()
    result = subprocess.run(
        cmd,
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
        "contains VPC-related content": any(kw in stdout.upper() for kw in ["VPC", "VPCNAME", "CIDRBLOCK", "ROSTEMPLATE"]),
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

    checks = {
        "JSON parsed successfully": True,
        "contains text field": "text" in data,
        "text is non-empty": bool(data.get("text")),
        "contains usage field": "usage" in data,
        "usage contains input_tokens": isinstance(data.get("usage", {}).get("input_tokens"), int),
    }

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

    if result.returncode != 0:
        print(f"{FAIL} Process exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stdout is empty")
        return False

    lines = [l for l in stdout.split("\n") if l.strip()]
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

    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print(f"{INFO} Event type distribution: {type_counts}")
    print(f"{PASS if all_pass else FAIL} stream-json output test {'passed' if all_pass else 'failed'}")
    return all_pass


def main():
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

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
