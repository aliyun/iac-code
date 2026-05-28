#!/usr/bin/env python3
"""
Windows 兼容性验证脚本 — Headless 模式
======================================

通过 ``iac-code -p "..." --output-format json`` 以非交互方式运行，
验证 headless 模式能正常完成一轮对话并输出结果。

用法:
    python scripts/test_headless_vpc.py

前提:
    - 已安装 iac-code (pip install -e . 或 pip install iac-code)
    - 已配置好 LLM 凭证 (运行过 iac-code 并执行 /auth，或设置了环境变量)
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

    print(f"{INFO} 运行命令: {' '.join(cmd[:6])}... --output-format {output_format}")
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
    print(f"{INFO} 耗时: {elapsed:.1f}s, 退出码: {result.returncode}")
    return result


def test_text_output():
    print("\n=== 测试 1: headless text 输出 ===")
    result = run_headless("text")

    if result.returncode != 0:
        print(f"{FAIL} 进程退出码非零: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stdout 为空")
        return False

    print(f"{INFO} 输出长度: {len(stdout)} 字符")
    print(f"{INFO} 输出前200字符: {stdout[:200]}")

    checks = {
        "有输出内容": len(stdout) > 10,
        "包含VPC相关内容": any(kw in stdout.upper() for kw in ["VPC", "VPCNAME", "CIDRBLOCK", "ROSTEMPLATE"]),
    }

    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print(f"{PASS if all_pass else FAIL} text 输出测试{'通过' if all_pass else '失败'}")
    return all_pass


def test_json_output():
    print("\n=== 测试 2: headless JSON 输出 ===")
    result = run_headless("json")

    if result.returncode != 0:
        print(f"{FAIL} 进程退出码非零: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stdout 为空")
        return False

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        print(f"{FAIL} JSON 解析失败: {e}")
        print(f"{INFO} 原始输出前300字符: {stdout[:300]}")
        return False

    checks = {
        "JSON 解析成功": True,
        "包含 text 字段": "text" in data,
        "text 非空": bool(data.get("text")),
        "包含 usage 字段": "usage" in data,
        "usage 包含 input_tokens": isinstance(data.get("usage", {}).get("input_tokens"), int),
    }

    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    if data.get("text"):
        print(f"{INFO} text 前200字符: {data['text'][:200]}")
    if data.get("usage"):
        print(f"{INFO} usage: {data['usage']}")

    print(f"{PASS if all_pass else FAIL} JSON 输出测试{'通过' if all_pass else '失败'}")
    return all_pass


def test_stream_json_output():
    print("\n=== 测试 3: headless stream-json 输出 ===")
    result = run_headless("stream-json")

    if result.returncode != 0:
        print(f"{FAIL} 进程退出码非零: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stdout 为空")
        return False

    lines = [l for l in stdout.split("\n") if l.strip()]
    print(f"{INFO} 共 {len(lines)} 行 NDJSON")

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
        "至少有 3 行事件": len(lines) >= 3,
        "所有行均为合法 JSON": parse_errors == 0,
        "包含 text_delta 事件": type_counts.get("text_delta", 0) > 0,
        "包含 message_end 事件": type_counts.get("message_end", 0) > 0,
    }

    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print(f"{INFO} 事件类型分布: {type_counts}")
    print(f"{PASS if all_pass else FAIL} stream-json 输出测试{'通过' if all_pass else '失败'}")
    return all_pass


def main():
    print("=" * 60)
    print("  iac-code Headless 模式 Windows 兼容性测试")
    print("=" * 60)

    results = {
        "text 输出": test_text_output(),
        "JSON 输出": test_json_output(),
        "stream-json 输出": test_stream_json_output(),
    }

    print("\n" + "=" * 60)
    print("  测试结果汇总")
    print("=" * 60)
    all_pass = True
    for name, passed in results.items():
        status = PASS if passed else FAIL
        print(f"  {status} {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print(f"{PASS} 所有 headless 测试通过!")
    else:
        print(f"{FAIL} 部分测试失败，请检查上方输出")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
