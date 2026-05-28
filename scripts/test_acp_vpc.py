#!/usr/bin/env python3
"""
Windows 兼容性验证脚本 — ACP 模式
=================================

启动 ACP stdio 服务端，通过 stdin/stdout 发送 JSON-RPC 消息模拟
一次完整的 initialize → new_session → prompt("创建VPC") → close 流程。

用法:
    python scripts/test_acp_vpc.py

前提:
    - 已安装 iac-code (pip install -e . 或 pip install iac-code)
    - 已配置好 LLM 凭证
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
    """管理 ACP stdio 子进程的生命周期，发送 JSON-RPC 请求并收集响应。"""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.responses: list[dict] = []
        self.notifications: list[dict] = []
        self._reader_thread: threading.Thread | None = None
        self._stop = False

    def start(self):
        cmd = [sys.executable, "-m", "iac_code.cli.main", "acp"]
        print(f"{INFO} 启动 ACP 服务: {' '.join(cmd)}")
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
            print(f"{INFO} [权限请求] id={request_id}, tool={title} -> 自动批准")
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
            print(f"{INFO} [服务端请求] method={method}, id={request_id} (未处理)")

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
    print("\n=== 测试: ACP stdio 完整生命周期 ===")
    client = ACPStdioClient()
    checks: dict[str, bool] = {}

    try:
        client.start()
        if client.process and client.process.poll() is not None:
            print(f"{FAIL} ACP 进程启动后立即退出，退出码: {client.process.returncode}")
            return False

        checks["ACP 进程启动成功"] = True
        print(f"{INFO} ACP 进程 PID: {client.process.pid if client.process else 'N/A'}")

        # 1. initialize
        print(f"\n{INFO} 步骤 1: 发送 initialize 请求")
        client.send(make_jsonrpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "test-script", "version": "0.1"},
        }, id=1))

        init_resp = client.wait_response(1, timeout=30)
        if init_resp is None:
            print(f"{FAIL} initialize 响应超时")
            checks["initialize 响应"] = False
            return False

        checks["initialize 响应"] = True
        result = init_resp.get("result", {})
        agent_info = result.get("agentInfo", {})
        print(f"{INFO} agentInfo: {agent_info}")
        checks["agentInfo.name == iac-code"] = agent_info.get("name") == "iac-code"

        # initialized notification
        client.send(make_notification("notifications/initialized"))
        time.sleep(0.5)

        # 2. new_session
        print(f"\n{INFO} 步骤 2: 发送 new_session 请求")
        cwd = os.path.abspath(".")
        client.send(make_jsonrpc("session/new", {"cwd": cwd, "mcpServers": []}, id=2))

        session_resp = client.wait_response(2, timeout=30)
        if session_resp is None:
            print(f"{FAIL} new_session 响应超时")
            checks["new_session 响应"] = False
            return False

        checks["new_session 响应"] = True
        session_result = session_resp.get("result", {})
        session_id = session_result.get("sessionId", "")
        print(f"{INFO} sessionId: {session_id}")
        checks["获得 sessionId"] = bool(session_id)

        if not session_id:
            print(f"{FAIL} 无法获取 sessionId，跳过后续步骤")
            if session_resp.get("error"):
                print(f"{INFO} 错误: {session_resp['error']}")
            return False

        # 等待 available_commands 等初始化通知
        time.sleep(2)
        init_notifications = len(client.notifications)
        print(f"{INFO} 收到 {init_notifications} 条初始化通知")

        # 3. prompt — 创建 VPC
        print(f"\n{INFO} 步骤 3: 发送 prompt 请求 (创建VPC)")
        client.notifications.clear()  # 清空旧通知，方便统计 prompt 产生的事件
        client.send(make_jsonrpc("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12，只输出JSON模板"}],
        }, id=3))

        prompt_resp = client.wait_response(3, timeout=TIMEOUT_SECONDS)
        if prompt_resp is None:
            print(f"{FAIL} prompt 响应超时 ({TIMEOUT_SECONDS}s)")
            checks["prompt 响应"] = False
            return False

        checks["prompt 响应"] = True
        prompt_result = prompt_resp.get("result", {})
        stop_reason = prompt_result.get("stopReason", "")
        print(f"{INFO} stopReason: {stop_reason}")
        checks["stopReason 为 end_turn"] = stop_reason == "end_turn"

        # 检查 session_update 通知
        prompt_notifications = client.notifications
        print(f"{INFO} prompt 期间收到 {len(prompt_notifications)} 条通知")

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
        print(f"{INFO} 文本块数: {len(text_chunks)}")
        print(f"{INFO} 工具调用数: {len(tool_calls)}")
        if combined_text:
            print(f"{INFO} 合并文本前200字符: {combined_text[:200]}")

        checks["收到文本输出"] = len(combined_text) > 0
        checks["文本包含VPC相关内容"] = any(
            kw in combined_text.upper() for kw in ["VPC", "TEMPLATE", "模板", "CIDR"]
        )

        # 4. close_session
        print(f"\n{INFO} 步骤 4: 关闭 session")
        client.send(make_jsonrpc("session/close", {"sessionId": session_id}, id=4))

        close_resp = client.wait_response(4, timeout=15)
        checks["close_session 响应"] = close_resp is not None
        if close_resp:
            print(f"{INFO} close 响应: {json.dumps(close_resp.get('result', {}))}")

    except Exception as e:
        print(f"{FAIL} 异常: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        stderr = client.stop()
        if stderr and stderr.strip():
            print(f"\n{INFO} ACP 进程 stderr (前3000字符):")
            print(f"  {stderr[:3000]}")

    # 汇总
    print(f"\n--- 检查项 ---")
    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print(f"\n{PASS if all_pass else FAIL} ACP 生命周期测试{'通过' if all_pass else '失败'}")
    return all_pass


def main():
    print("=" * 60)
    print("  iac-code ACP 模式 Windows 兼容性测试")
    print("=" * 60)

    passed = test_acp_lifecycle()

    print()
    if passed:
        print(f"{PASS} ACP 测试通过!")
    else:
        print(f"{FAIL} ACP 测试失败，请检查上方输出")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
