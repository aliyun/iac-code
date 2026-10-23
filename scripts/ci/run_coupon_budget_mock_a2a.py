#!/usr/bin/env python3
"""Real HTTP/SSE A2A + real LLM, with only BSS coupon results substituted.

The 240 CNY compute charge is a test input, not a current GPU quote. Other
read-only cloud calls retain the configured real credential and transport.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    stream_message,
    wait_for_server,
)
from scripts.ci.coupon_fixtures import CASES, coupon  # noqa: E402
from scripts.ci.run_coupon_budget_a2a import tool_inputs  # noqa: E402
from scripts.ci.run_promotional_offer_followup_a2a import (  # noqa: E402
    pipeline_facts,
    visible_pipeline_text,
)
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402

SETUP = (
    "准备在阿里云北京按量租一台 GPU 服务器，指定 ecs.gn6i-c16g1.4xlarge，"
    "显存 16 GiB、内存 62 GiB，只用 8 小时。请给一个方案让我选择，暂不部署，"
    "不创建、修改或删除任何云资源。"
)
QUESTION = (
    "继续刚才北京的 GPU 部署方案，我现在换用另一张面值 300 元的学生券，条件可能不同，"
    "请重新查询当前账号的券和适用商品，不沿用之前的券查询结果。"
    "本次只核对 GPU 计算部分：按量使用 8 小时，控制台给出的计算费合计 240 元；"
    "请按这 240 元分析用券，不另查价格。系统盘、EIP、公网流量不包括在这 240 元内，其他费用另算。"
    "我希望这部分不补现金。请用表格分别列出 GPU 计算费、券的可用余额、"
    "本次计算费可抵扣金额、计算部分需补现金、结论依据；"
    "无法确认的填写待确认，不要猜测。暂不选择方案、不重新规划、不下单或用券。"
)


def has_assistant_reference(text: str) -> bool:
    """Recognize console links by their parsed host, not a substring in an arbitrary URL."""
    for url in re.findall(r"https?://[^\s<>()\[\]]+", text):
        try:
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname == "ros.console.aliyun.com":
                return True
        except ValueError:
            continue
    return False


def answer_cell(text: str, label: str) -> str:
    """Accept vertical or horizontal Markdown tables without using unrelated amounts."""
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines() if line.strip().startswith("|")
    ]
    for index, cells in enumerate(rows):
        # Only a row immediately above a Markdown separator is a horizontal
        # header. A data cell may mention cash without naming the cash column.
        if index + 2 >= len(rows) or not all(
            re.fullmatch(r"[\s:-]+", item) for item in rows[index + 1]
        ):
            continue
        for column, cell in enumerate(cells):
            if re.search(label, cell) and len(rows[index + 2]) > column:
                return rows[index + 2][column]
    for cells in rows:
        if len(cells) >= 2 and re.search(label, cells[0]):
            return cells[1]
    return ""


def cash_cell(text: str) -> str:
    return answer_cell(text, r"现金|自付|自费|差价")


def amount_matches(cell: str, expected: int | None) -> bool:
    uncertainty = re.sub(r"待确认项[：:\s]*无|无待确认项", "", cell)
    if expected is None:
        return bool(re.search(r"待确认|未知|不确定|无法|未确认|不能确认|待核实|如果|若|假设", uncertainty))
    if re.search(r"待确认|未知|不确定|无法确认|未确认", uncertainty) and not re.search(r"预计|估算|预估", cell):
        return False
    amounts = [Decimal(value.replace(",", "")) for value in re.findall(r"\d[\d,]*(?:\.\d+)?", cell)]
    return bool(amounts) and amounts[0] == Decimal(expected)


def evaluate_answer(case: str, text: str) -> dict[str, bool]:
    cash = CASES[case].cash
    coverage = None if cash is None else 240 - cash
    return {
        "cash calculation or unknown matches evidence": amount_matches(cash_cell(text), cash),
        "deduction calculation or unknown matches evidence": amount_matches(
            answer_cell(text, r"抵扣金额|抵扣费用"), coverage,
        ),
    }


def recorded_calls(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(args: argparse.Namespace) -> dict[str, Any]:
    config_dir = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config_dir.resolve().is_relative_to(args.run_dir.resolve()):
        raise ValueError("Run through run_e2e.py with isolated configuration")
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    active = args.run_dir / "coupon-fixture-state.json"
    ledger = args.run_dir / "coupon-fixture-calls.jsonl"
    active.write_text(json.dumps({"case": "applicable"}), encoding="utf-8")
    port = _free_port("127.0.0.1")
    config = _write_server_config(args.run_dir, host="127.0.0.1", port=port, auto_approve_permissions=True)
    observe = ObserveCapture(args.run_dir / "telemetry").start()
    env = dict(os.environ)
    env.update(observe.env)
    env.update({
        "PYTHONPATH": str(REPO / "src"), "IAC_CODE_MODE": "pipeline",
        "IAC_CODE_PIPELINE_NAME": "selling_solution_first", "IAC_CODE_ROS_AGENT_AWARENESS": "true",
        "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1", "IAC_CODE_PROVIDER": args.provider,
        "IAC_CODE_MODEL": args.model, "IAC_CODE_CONFIG_BACKUP_DIR": str(args.run_dir / "backup"),
    })
    server = ManagedServer(
        python_cmd=[sys.executable], config_path=config, process_cwd=str(REPO),
        allowed_cwd=str(workspace), env=env, log_prefix=args.run_dir / "server",
        server_args=[
            "-m", "scripts.ci.coupon_fixture_server", "--state", str(active),
            "--calls", str(ledger), "--config", str(config),
        ],
    )
    checks: dict[str, bool] = {}
    rows = []
    server_url = f"http://127.0.0.1:{port}"
    try:
        server.start()
        wait_for_server(server_url, timeout=60, owned_server=server)
        setup = stream_message(
            server_url=server_url, cwd=str(workspace), prompt=SETUP, name="setup",
            run_dir=args.run_dir, timeout=args.timeout, redaction_env=env,
        )
        checks["real setup waits for selection"] = (
            setup.last_status_state == "TASK_STATE_INPUT_REQUIRED" and not setup.normal_handoff_ready
        )
        if not checks["real setup waits for selection"]:
            raise RuntimeError("Real-model setup did not reach the planning selection boundary")
        context_id, task_id = setup.context_id, setup.task_id
        for case in args.scenario or CASES:
            active.write_text(json.dumps({"case": case}), encoding="utf-8")
            start = time.monotonic()
            before = len(recorded_calls(ledger))
            prior_tool_ids = {call["id"] for call in tool_inputs(config_dir)}
            summary = stream_message(
                server_url=server_url, cwd=str(workspace), prompt=QUESTION,
                context_id=context_id, task_id=task_id, name=case,
                run_dir=args.run_dir, timeout=args.timeout, redaction_env=env,
            )
            context_id, task_id = summary.context_id, summary.task_id
            path = args.run_dir / f"{case}.events.jsonl"
            text = visible_pipeline_text(path)
            # Fixtures contain no real user data. Keep the model answer for human review.
            (args.run_dir / f"{case}.answer.txt").write_text(text, encoding="utf-8")
            conclusion, steps = pipeline_facts(path)
            calls = recorded_calls(ledger)[before:]
            cell = cash_cell(text)
            local = {
                "real HTTP/SSE": summary.response_content_type == "text/event-stream",
                "visible answer returned for the original question": bool(text.strip()),
                "queried current coupon fixture": any(
                    call.get("case") == case and call.get("action") == "DescribeCoupon"
                    and call.get("mocked") is True for call in calls
                ),
                **evaluate_answer(case, text),
                "stays in deployment planning": (
                    summary.last_status_state == "TASK_STATE_INPUT_REQUIRED"
                    and not summary.normal_handoff_ready and conclusion.get("status") != "rejected"
                    and "materialize_selected_candidate" not in steps
                ),
                "no assistant handoff": not summary.normal_handoff_ready and conclusion.get("status") != "rejected",
            }
            if CASES[case].requires_items:
                local["queried item scope"] = any(
                    call.get("case") == case and call.get("action") == "DescribeCouponItemList" for call in calls
                )
                local["item query uses the current coupon ID"] = any(
                    call["id"] not in prior_tool_ids and call["name"] == "aliyun_api"
                    and call["input"].get("action") == "DescribeCouponItemList"
                    and str(call["input"].get("params", {}).get("CouponId")) == str(coupon(case)["CouponId"])
                    and int(call["input"].get("params", {}).get("CurrentPage", 1)) == 1
                    for call in tool_inputs(config_dir)
                )
            if case == "applicable":
                local["compute coverage is quantified"] = "240" in text
                local["settlement caveat remains"] = bool(re.search(r"账单|结算|以实际|实际抵扣", text))
            for key, value in local.items():
                checks[f"{case}: {key}"] = value
            row = {
                "case": case, "title": CASES[case].title, "expected_cash": CASES[case].cash,
                "cash_cell": cell, "passed": all(local.values()), "checks": local,
                "assistant_reference_present": has_assistant_reference(text),
                "seconds": round(time.monotonic() - start, 2),
            }
            rows.append(row)
            print(json.dumps({"model": args.model, **row}, ensure_ascii=False), flush=True)
            if summary.normal_handoff_ready:
                break
        all_calls = recorded_calls(ledger)
        checks["no cloud writes reached transport"] = not any(c.get("blocked_cloud_write") for c in all_calls)
        checks["all selected cases completed"] = len(rows) == len(args.scenario or CASES)
        checks["coupon guide was actually read"] = any(
            call["name"] == "read_file" and "coupons.md" in json.dumps(call["input"])
            for call in tool_inputs(config_dir)
        )
    finally:
        server.terminate()
        records = observe.stop()
        for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
            (config_dir / name).unlink(missing_ok=True)
    observed = sorted({
        str(record.get("attributes", {}).get("gen_ai.request.model"))
        for record in records if record.get("attributes", {}).get("gen_ai.request.model")
    })
    checks["only requested real model ran"] = observed == [args.model]
    return {
        "status": "passed" if all(checks.values()) else "failed", "checks": checks, "cases": rows,
        "model": args.model, "observed_models": observed,
        "coupon_api_mocked": True, "llm_mocked": False, "settlement_verified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model", default="glm-5.2-fast-preview")
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--python")
    parser.add_argument("--scenario", choices=tuple(CASES), action="append")
    parser.add_argument("--timeout", type=float, default=420)
    parser.add_argument("--allow-real-cloud", action="store_true")
    args = parser.parse_args()
    if not args.allow_real_cloud:
        parser.error("--allow-real-cloud is required for the remaining real read-only APIs and LLM")
    args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    result: dict[str, Any] = {"scenario": "coupon-budget-mock-a2a", "cleanup_status": "not-needed"}
    try:
        result.update(run(args))
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
    (args.run_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
