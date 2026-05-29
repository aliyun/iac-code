#!/usr/bin/env python3
"""
Windows Compatibility Verification Script — A2A Mode
=====================================================

Starts the A2A HTTP server, then sends a "create VPC" prompt via
``iac-code a2a-client call`` to verify the full server <-> client flow.

Usage:
    python scripts/test_a2a_vpc.py

Prerequisites:
    - iac-code[a2a] installed (pip install -e ".[a2a]" or pip install iac-code[a2a])
    - LLM credentials configured
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"

A2A_HOST = "127.0.0.1"
A2A_PORT = 41299  # Use non-default port to avoid conflicts
A2A_URL = f"http://{A2A_HOST}:{A2A_PORT}"
TIMEOUT_SECONDS = 300


def wait_for_server(url: str, timeout: float = 30) -> bool:
    """Wait for the A2A HTTP service to become ready."""
    card_url = f"{url}/.well-known/agent-card.json"
    health_url = f"{url}/health"
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            req = urllib.request.Request(card_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return True
                last_error = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code} {e.reason}"
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            if body:
                last_error += f" body={body}"
        except (urllib.error.URLError, OSError, ConnectionRefusedError) as e:
            last_error = str(e)
        time.sleep(1)

    print(f"{INFO} agent.json last error: {last_error}")
    # Try /health endpoint for comparison diagnostics
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"{INFO} /health returned: HTTP {resp.status}")
    except Exception as e:
        print(f"{INFO} /health also failed: {e}")
    return False


def _stream_stderr(proc: subprocess.Popen) -> None:
    """Background thread to print server stderr logs in real-time."""
    assert proc.stderr
    for line in proc.stderr:
        line = line.rstrip()
        if line:
            print(f"{INFO} [A2A Server] {line}")


def start_a2a_server(config_path: str) -> subprocess.Popen:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "a2a",
        "--config", config_path,
        "--host", A2A_HOST,
        "--port", str(A2A_PORT),
    ]
    print(f"{INFO} Starting A2A server: {' '.join(cmd)}")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    threading.Thread(target=_stream_stderr, args=(proc,), daemon=True).start()
    return proc


def run_a2a_client_call(prompt: str, *, stream: bool = False, cwd: str = ".") -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "a2a-client", "call",
        "--url", A2A_URL,
        "--prompt", prompt,
        "--cwd", cwd,
        "--timeout", str(TIMEOUT_SECONDS),
    ]
    if stream:
        cmd.append("--stream")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    print(f"{INFO} Running client: a2a-client call {'--stream ' if stream else ''}--prompt \"{prompt[:30]}...\"")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS + 30,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def run_a2a_client_discover() -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "a2a-client", "discover",
        "--url", A2A_URL,
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    print(f"{INFO} Running client: a2a-client discover")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def test_discover(checks: dict[str, bool]) -> bool:
    print(f"\n{INFO} Step 1: Agent Card Discovery")
    result = run_a2a_client_discover()

    if result.returncode != 0:
        print(f"{FAIL} discover exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["discover succeeded"] = False
        return False

    stdout = result.stdout.strip()
    try:
        card = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"{FAIL} discover output is not JSON: {stdout[:200]}")
        checks["discover succeeded"] = False
        return False

    checks["discover succeeded"] = True
    name = card.get("name", "")
    print(f"{INFO} Agent name: {name}")
    checks["Agent Card name is iac-code"] = name == "iac-code"
    return True


def test_call_sync(checks: dict[str, bool]) -> bool:
    print(f"\n{INFO} Step 2: Synchronous call (create VPC)")
    prompt = "帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12，只输出JSON模板"
    result = run_a2a_client_call(prompt)

    if result.returncode != 0:
        print(f"{FAIL} call exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["sync call succeeded"] = False
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} call output is empty")
        checks["sync call succeeded"] = False
        return False

    checks["sync call succeeded"] = True
    print(f"{INFO} Output length: {len(stdout)} chars")
    print(f"{INFO} First 200 chars: {stdout[:200]}")
    checks["output contains VPC-related content"] = any(
        kw in stdout.upper() for kw in ["VPC", "TEMPLATE", "CIDR", "ROSTEMPLATE"]
    )
    return True


def test_call_stream(checks: dict[str, bool]) -> bool:
    print(f"\n{INFO} Step 3: Streaming call --stream (create VPC)")
    prompt = "帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12，只输出JSON模板"
    result = run_a2a_client_call(prompt, stream=True)

    if result.returncode != 0:
        print(f"{FAIL} stream call exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["stream call succeeded"] = False
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stream call output is empty")
        checks["stream call succeeded"] = False
        return False

    checks["stream call succeeded"] = True
    lines = stdout.split("\n")
    print(f"{INFO} Received {len(lines)} lines of streaming output")
    print(f"{INFO} First 3 lines:")
    for line in lines[:3]:
        print(f"  {line[:120]}")

    combined = stdout.upper()
    checks["stream output contains VPC-related content"] = any(
        kw in combined for kw in ["VPC", "TEMPLATE", "CIDR"]
    )
    return True


def main():
    print("=" * 60)
    print("  iac-code A2A Mode Windows Compatibility Test")
    print("=" * 60)

    # Create A2A config file (enable auto-approve to avoid permission blocking)
    config_content = "auto-approve-permissions: true\n"
    config_fd, config_path = tempfile.mkstemp(suffix=".yml", prefix="a2a_test_")
    os.write(config_fd, config_content.encode("utf-8"))
    os.close(config_fd)

    server_proc = None
    checks: dict[str, bool] = {}

    try:
        # Start A2A server
        server_proc = start_a2a_server(config_path)
        time.sleep(2)

        if server_proc.poll() is not None:
            print(f"{FAIL} A2A server exited immediately after start, exit code: {server_proc.returncode}")
            stderr = server_proc.stderr.read() if server_proc.stderr else ""
            if stderr:
                print(f"{INFO} stderr: {stderr[:500]}")
            checks["A2A server started"] = False
        else:
            print(f"{INFO} A2A server PID: {server_proc.pid}")
            print(f"{INFO} Waiting for server to become ready...")

            if not wait_for_server(A2A_URL, timeout=30):
                print(f"{FAIL} A2A server not ready within 30s")
                checks["A2A server started"] = True
                checks["A2A server ready"] = False
            else:
                checks["A2A server started"] = True
                checks["A2A server ready"] = True
                print(f"{PASS} A2A server is ready ({A2A_URL})")

                test_discover(checks)
                test_call_sync(checks)
                test_call_stream(checks)

    except Exception as e:
        print(f"{FAIL} Exception: {e}")
        checks["test execution"] = False
        import traceback
        traceback.print_exc()
    finally:
        # Stop A2A server
        if server_proc:
            print(f"\n{INFO} Stopping A2A server...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()

        # Clean up config file
        try:
            os.unlink(config_path)
        except OSError:
            pass

    # Summary
    print(f"\n{'=' * 60}")
    print("  Test Results Summary")
    print("=" * 60)
    all_pass = bool(checks)
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print(f"{PASS} All A2A tests passed!")
    else:
        print(f"{FAIL} Some tests failed, check output above")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
