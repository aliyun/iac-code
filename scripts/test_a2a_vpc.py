#!/usr/bin/env python3
"""
Windows 兼容性验证脚本 — A2A 模式
=================================

启动 A2A HTTP 服务端，然后通过 ``iac-code a2a-client call`` 发送
"创建VPC" prompt，验证整个 server ↔ client 流程。

用法:
    python scripts/test_a2a_vpc.py

前提:
    - 已安装 iac-code[a2a] (pip install -e ".[a2a]" 或 pip install iac-code[a2a])
    - 已配置好 LLM 凭证
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
A2A_PORT = 41299  # 用非默认端口避免冲突
A2A_URL = f"http://{A2A_HOST}:{A2A_PORT}"
TIMEOUT_SECONDS = 300


def wait_for_server(url: str, timeout: float = 30) -> bool:
    """等待 A2A HTTP 服务就绪。"""
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

    print(f"{INFO} agent.json 最后错误: {last_error}")
    # 尝试 /health 端点作为对比诊断
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"{INFO} /health 返回: HTTP {resp.status}")
    except Exception as e:
        print(f"{INFO} /health 也失败: {e}")
    return False


def _stream_stderr(proc: subprocess.Popen) -> None:
    """后台线程实时打印服务端 stderr 日志。"""
    assert proc.stderr
    for line in proc.stderr:
        line = line.rstrip()
        if line:
            print(f"{INFO} [A2A服务] {line}")


def start_a2a_server(config_path: str) -> subprocess.Popen:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "a2a",
        "--config", config_path,
        "--host", A2A_HOST,
        "--port", str(A2A_PORT),
    ]
    print(f"{INFO} 启动 A2A 服务: {' '.join(cmd)}")
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
    print(f"{INFO} 运行客户端: a2a-client call {'--stream ' if stream else ''}--prompt \"{prompt[:30]}...\"")
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
    print(f"{INFO} 运行客户端: a2a-client discover")
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
    print(f"\n{INFO} 步骤 1: Agent Card 发现")
    result = run_a2a_client_discover()

    if result.returncode != 0:
        print(f"{FAIL} discover 退出码非零: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["discover 成功"] = False
        return False

    stdout = result.stdout.strip()
    try:
        card = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"{FAIL} discover 输出非 JSON: {stdout[:200]}")
        checks["discover 成功"] = False
        return False

    checks["discover 成功"] = True
    name = card.get("name", "")
    print(f"{INFO} Agent name: {name}")
    checks["Agent Card name 为 iac-code"] = name == "iac-code"
    return True


def test_call_sync(checks: dict[str, bool]) -> bool:
    print(f"\n{INFO} 步骤 2: 同步 call (创建VPC)")
    prompt = "帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12，只输出JSON模板"
    result = run_a2a_client_call(prompt)

    if result.returncode != 0:
        print(f"{FAIL} call 退出码非零: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["同步 call 成功"] = False
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} call 输出为空")
        checks["同步 call 成功"] = False
        return False

    checks["同步 call 成功"] = True
    print(f"{INFO} 输出长度: {len(stdout)} 字符")
    print(f"{INFO} 输出前200字符: {stdout[:200]}")
    checks["输出包含VPC相关内容"] = any(
        kw in stdout.upper() for kw in ["VPC", "TEMPLATE", "模板", "CIDR", "ROSTEMPLATE"]
    )
    return True


def test_call_stream(checks: dict[str, bool]) -> bool:
    print(f"\n{INFO} 步骤 3: 流式 call --stream (创建VPC)")
    prompt = "帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12，只输出JSON模板"
    result = run_a2a_client_call(prompt, stream=True)

    if result.returncode != 0:
        print(f"{FAIL} stream call 退出码非零: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["流式 call 成功"] = False
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stream call 输出为空")
        checks["流式 call 成功"] = False
        return False

    checks["流式 call 成功"] = True
    lines = stdout.split("\n")
    print(f"{INFO} 收到 {len(lines)} 行流式输出")
    print(f"{INFO} 前3行:")
    for line in lines[:3]:
        print(f"  {line[:120]}")

    combined = stdout.upper()
    checks["流式输出包含VPC相关内容"] = any(
        kw in combined for kw in ["VPC", "TEMPLATE", "模板", "CIDR"]
    )
    return True


def main():
    print("=" * 60)
    print("  iac-code A2A 模式 Windows 兼容性测试")
    print("=" * 60)

    # 创建 A2A 配置文件 (启用 auto-approve 避免权限阻塞)
    config_content = "auto-approve-permissions: true\n"
    config_fd, config_path = tempfile.mkstemp(suffix=".yml", prefix="a2a_test_")
    os.write(config_fd, config_content.encode("utf-8"))
    os.close(config_fd)

    server_proc = None
    checks: dict[str, bool] = {}

    try:
        # 启动 A2A 服务
        server_proc = start_a2a_server(config_path)
        time.sleep(2)

        if server_proc.poll() is not None:
            print(f"{FAIL} A2A 服务启动后立即退出，退出码: {server_proc.returncode}")
            stderr = server_proc.stderr.read() if server_proc.stderr else ""
            if stderr:
                print(f"{INFO} stderr: {stderr[:500]}")
            checks["A2A 服务启动"] = False
        else:
            print(f"{INFO} A2A 服务 PID: {server_proc.pid}")
            print(f"{INFO} 等待服务就绪...")

            if not wait_for_server(A2A_URL, timeout=30):
                print(f"{FAIL} A2A 服务未在 30s 内就绪")
                checks["A2A 服务启动"] = True
                checks["A2A 服务就绪"] = False
            else:
                checks["A2A 服务启动"] = True
                checks["A2A 服务就绪"] = True
                print(f"{PASS} A2A 服务已就绪 ({A2A_URL})")

                test_discover(checks)
                test_call_sync(checks)
                test_call_stream(checks)

    except Exception as e:
        print(f"{FAIL} 异常: {e}")
        checks["测试执行"] = False
        import traceback
        traceback.print_exc()
    finally:
        # 停止 A2A 服务
        if server_proc:
            print(f"\n{INFO} 停止 A2A 服务...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()

        # 清理配置文件
        try:
            os.unlink(config_path)
        except OSError:
            pass

    # 汇总
    print(f"\n{'=' * 60}")
    print("  测试结果汇总")
    print("=" * 60)
    all_pass = bool(checks)
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print(f"{PASS} 所有 A2A 测试通过!")
    else:
        print(f"{FAIL} 部分测试失败，请检查上方输出")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
