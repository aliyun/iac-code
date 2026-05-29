#!/usr/bin/env python3
"""
Windows Compatibility Verification Script — ACP Mode
=====================================================

Starts the ACP stdio server and sends JSON-RPC messages via stdin/stdout
to simulate a full initialize -> new_session -> prompt("create VPC") -> close flow.

Usage:
    python scripts/test_acp_vpc.py

Prerequisites:
    - iac-code installed (pip install -e . or pip install iac-code)
    - LLM credentials configured
"""

import json
import os
import subprocess
import sys
import threading
import time

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"

TIMEOUT_SECONDS = 300


def make_jsonrpc(method: str, params: dict, id: int) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id})


def make_notification(method: str, params: dict | None = None) -> str:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


class ACPStdioClient:
    """Manages the ACP stdio subprocess lifecycle, sends JSON-RPC requests and collects responses."""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.responses: list[dict] = []
        self.notifications: list[dict] = []
        self._reader_thread: threading.Thread | None = None
        self._stop = False

    def start(self):
        cmd = [sys.executable, "-m", "iac_code.cli.main", "acp"]
        print(f"{INFO} Starting ACP server: {' '.join(cmd)}")
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()
        time.sleep(1)

    def _read_stdout(self):
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in msg and "id" in msg:
                self._handle_server_request(msg)
            elif "id" in msg:
                self.responses.append(msg)
            else:
                self.notifications.append(msg)
            if self._stop:
                break

    def _handle_server_request(self, msg: dict):
        """Auto-approve permission requests and other server-to-client requests."""
        method = msg["method"]
        request_id = msg["id"]
        if method == "session/request_permission":
            params = msg.get("params", {})
            tool_call = params.get("toolCall", params.get("tool_call", {}))
            title = tool_call.get("title", "unknown")
            print(f"{INFO} [Permission request] id={request_id}, tool={title} -> auto-approved")
            response = json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": "allow_once",
                    }
                },
            })
            self.send(response)
        else:
            print(f"{INFO} [Server request] method={method}, id={request_id} (not handled)")

    def send(self, message: str):
        assert self.process and self.process.stdin
        self.process.stdin.write(message + "\n")
        self.process.stdin.flush()

    def wait_response(self, request_id: int, timeout: float = 60) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for resp in self.responses:
                if resp.get("id") == request_id:
                    return resp
            time.sleep(0.3)
        return None

    def wait_notifications(self, min_count: int = 1, timeout: float = 120) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.notifications) < min_count:
            time.sleep(0.3)
        return list(self.notifications)

    def stop(self):
        self._stop = True
        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=10)
            except Exception:
                self.process.kill()
        stderr_output = ""
        if self.process and self.process.stderr:
            try:
                stderr_output = self.process.stderr.read()
            except Exception:
                pass
        return stderr_output


def test_acp_lifecycle():
    print("\n=== Test: ACP stdio Full Lifecycle ===")
    client = ACPStdioClient()
    checks: dict[str, bool] = {}

    try:
        client.start()
        if client.process and client.process.poll() is not None:
            print(f"{FAIL} ACP process exited immediately after start, exit code: {client.process.returncode}")
            return False

        checks["ACP process started successfully"] = True
        print(f"{INFO} ACP process PID: {client.process.pid if client.process else 'N/A'}")

        # 1. initialize
        print(f"\n{INFO} Step 1: Send initialize request")
        client.send(make_jsonrpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "test-script", "version": "0.1"},
        }, id=1))

        init_resp = client.wait_response(1, timeout=30)
        if init_resp is None:
            print(f"{FAIL} initialize response timed out")
            checks["initialize response"] = False
            return False

        checks["initialize response"] = True
        result = init_resp.get("result", {})
        agent_info = result.get("agentInfo", {})
        print(f"{INFO} agentInfo: {agent_info}")
        checks["agentInfo.name == iac-code"] = agent_info.get("name") == "iac-code"

        # initialized notification
        client.send(make_notification("notifications/initialized"))
        time.sleep(0.5)

        # 2. new_session
        print(f"\n{INFO} Step 2: Send new_session request")
        cwd = os.path.abspath(".")
        client.send(make_jsonrpc("session/new", {"cwd": cwd, "mcpServers": []}, id=2))

        session_resp = client.wait_response(2, timeout=30)
        if session_resp is None:
            print(f"{FAIL} new_session response timed out")
            checks["new_session response"] = False
            return False

        checks["new_session response"] = True
        session_result = session_resp.get("result", {})
        session_id = session_result.get("sessionId", "")
        print(f"{INFO} sessionId: {session_id}")
        checks["obtained sessionId"] = bool(session_id)

        if not session_id:
            print(f"{FAIL} Unable to get sessionId, skipping subsequent steps")
            if session_resp.get("error"):
                print(f"{INFO} Error: {session_resp['error']}")
            return False

        # Wait for initialization notifications like available_commands
        time.sleep(2)
        init_notifications = len(client.notifications)
        print(f"{INFO} Received {init_notifications} initialization notifications")

        # 3. prompt — create VPC
        print(f"\n{INFO} Step 3: Send prompt request (create VPC)")
        client.notifications.clear()
        client.send(make_jsonrpc("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12，只输出JSON模板"}],
        }, id=3))

        prompt_resp = client.wait_response(3, timeout=TIMEOUT_SECONDS)
        if prompt_resp is None:
            print(f"{FAIL} prompt response timed out ({TIMEOUT_SECONDS}s)")
            checks["prompt response"] = False
            return False

        checks["prompt response"] = True
        prompt_result = prompt_resp.get("result", {})
        stop_reason = prompt_result.get("stopReason", "")
        print(f"{INFO} stopReason: {stop_reason}")
        checks["stopReason is end_turn"] = stop_reason == "end_turn"

        # Check session_update notifications
        prompt_notifications = client.notifications
        print(f"{INFO} Received {len(prompt_notifications)} notifications during prompt")

        text_chunks = []
        tool_calls = []
        for n in prompt_notifications:
            params = n.get("params", {})
            update = params.get("update", {})
            update_type = update.get("sessionUpdate", update.get("session_update", ""))
            if update_type == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    text_chunks.append(content.get("text", ""))
            elif update_type == "tool_call":
                tool_calls.append(update.get("title", ""))

        combined_text = "".join(text_chunks)
        print(f"{INFO} Text chunks: {len(text_chunks)}")
        print(f"{INFO} Tool calls: {len(tool_calls)}")
        if combined_text:
            print(f"{INFO} Combined text first 200 chars: {combined_text[:200]}")

        checks["received text output"] = len(combined_text) > 0
        checks["text contains VPC-related content"] = any(
            kw in combined_text.upper() for kw in ["VPC", "TEMPLATE", "CIDR"]
        )

        # 4. close_session
        print(f"\n{INFO} Step 4: Close session")
        client.send(make_jsonrpc("session/close", {"sessionId": session_id}, id=4))

        close_resp = client.wait_response(4, timeout=15)
        checks["close_session response"] = close_resp is not None
        if close_resp:
            print(f"{INFO} close response: {json.dumps(close_resp.get('result', {}))}")

    except Exception as e:
        print(f"{FAIL} Exception: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        stderr = client.stop()
        if stderr and stderr.strip():
            print(f"\n{INFO} ACP process stderr (first 3000 chars):")
            print(f"  {stderr[:3000]}")

    # Summary
    print(f"\n--- Check Items ---")
    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print(f"\n{PASS if all_pass else FAIL} ACP lifecycle test {'passed' if all_pass else 'failed'}")
    return all_pass


def main():
    print("=" * 60)
    print("  iac-code ACP Mode Windows Compatibility Test")
    print("=" * 60)

    passed = test_acp_lifecycle()

    print()
    if passed:
        print(f"{PASS} ACP test passed!")
    else:
        print(f"{FAIL} ACP test failed, check output above")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
