#!/usr/bin/env python3
"""Verify refreshed Step 1 totals over real LLM + A2A HTTP/SSE, with explicit reference prices.

Prices are user-supplied test inputs, not live cloud quotations. No mocked model,
cloud credentials or resource writes. Stop at candidate selection on each turn.
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

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from scripts.a2a.debugger import _extract_pipeline_envelopes  # noqa: E402
from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    stream_message,
    wait_for_server,
)
from scripts.ci.run_coupon_budget_a2a import tool_inputs  # noqa: E402
from scripts.ci.run_promotional_offer_followup_a2a import pipeline_facts  # noqa: E402
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402

PROMPTS = (
    "在阿里云杭州部署一个临时 nginx 静态网站，只用2天（48小时），一台2核2GiB ECS、"
    "40GiB系统盘、一个按流量计费的EIP，VPC、交换机和安全组按需创建。"
    "本次评估采用以下连续按量参考价，均按实际小时等比例折算，没有最低计费时长或向上取整；不再查询价格：每台ECS计算费0.125～0.25元/小时，"
    "每台系统盘1～2元/48小时，每个EIP保有费0.25元/天，流量每GB 1元，预计1～5GB。"
    "只给一个最小架构方案和费用明细让我选择，不生成模板、不部署。",
    "改成两台相同的ECS、每台一个EIP，都只用12小时，继续按实际小时等比例折算。参考单价不变："
    "每台计算费0.125～0.25元/小时，每台系统盘1～2元/48小时，每个EIP保有费0.25元/天，"
    "全站合计流量仍预计1～5GB、每GB 1元。更新一个方案供我选择，不进入下一步。",
)


def price_bounds(value: str) -> tuple[Decimal, Decimal] | None:
    """Read displayed bounds for test assertions only; production forwards the model's text."""
    match = re.search(
        r"(?:[¥￥]|CNY|RMB)\s*(\d+(?:\.\d+)?)"
        r"(?:\s*(?:～|~|[-–—]|至|到)\s*(?:[¥￥]|CNY|RMB)?\s*(\d+(?:\.\d+)?))?",
        value,
    )
    if match is None:
        return None
    return Decimal(match[1]), Decimal(match[2] or match[1])


def matches_estimate(value: str, expected: tuple[Decimal, Decimal]) -> bool:
    # Step 1 is a rough estimate; allow display rounding to one decimal place.
    # This still rejects included traffic charges or invented minimum billing periods.
    bounds = price_bounds(value)
    return bounds is not None and all(
        abs(actual - target) <= Decimal("0.05") for actual, target in zip(bounds, expected)
    )


def envelopes(path: Path) -> list[dict[str, Any]]:
    return [
        envelope
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for envelope in _extract_pipeline_envelopes(json.loads(line))
    ]


def run(args: argparse.Namespace) -> dict[str, Any]:
    config_dir = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config_dir.resolve().is_relative_to(args.run_dir.resolve()):
        raise ValueError("Run through run_e2e.py with isolated configuration")
    if (config_dir / ".cloud-credentials.yml").exists():
        raise ValueError("This case uses supplied reference prices and LLM-only configuration")
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    port = _free_port("127.0.0.1")
    config = _write_server_config(args.run_dir, host="127.0.0.1", port=port, auto_approve_permissions=True)
    observe = ObserveCapture(args.run_dir / "telemetry").start()
    env = dict(os.environ)
    env.update(observe.env)
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "IAC_CODE_MODE": "pipeline",
            "IAC_CODE_PIPELINE_NAME": "selling_solution_first",
            "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1",
            "IAC_CODE_PROVIDER": args.provider,
            "IAC_CODE_MODEL": args.model,
            "IAC_CODE_CONFIG_BACKUP_DIR": str(args.run_dir / "backup"),
        }
    )
    server = ManagedServer(
        python_cmd=[sys.executable],
        config_path=config,
        process_cwd=str(REPO),
        allowed_cwd=str(workspace),
        env=env,
        log_prefix=args.run_dir / "server",
    )
    checks: dict[str, bool] = {}
    turns = []
    context_id = task_id = ""
    try:
        server.start()
        wait_for_server(f"http://127.0.0.1:{port}", timeout=60, owned_server=server)
        for index, prompt in enumerate(PROMPTS):
            started = time.monotonic()
            summary = stream_message(
                server_url=f"http://127.0.0.1:{port}",
                cwd=str(workspace),
                prompt=prompt,
                context_id=context_id,
                task_id=task_id,
                name=f"turn-{index + 1}",
                run_dir=args.run_dir,
                timeout=args.timeout,
                redaction_env=env,
            )
            context_id, task_id = summary.context_id, summary.task_id
            path = args.run_dir / f"turn-{index + 1}.events.jsonl"
            conclusion, steps = pipeline_facts(path)
            candidates = conclusion.get("candidates") or []
            displays = [
                event["data"]["detail"]
                for event in envelopes(path)
                if event.get("eventType") == "candidate_detail_shown"
                and event.get("data", {}).get("detailStage") == "detail"
            ]
            expected = (Decimal("7.5"), Decimal("14.5")) if index == 0 else (Decimal("3.75"), Decimal("7.25"))
            label = f"turn {index + 1}"
            checks[label + ": real HTTP/SSE and awaiting selection"] = (
                summary.response_content_type == "text/event-stream"
                and summary.last_status_state == "TASK_STATE_INPUT_REQUIRED"
                and conclusion.get("status") == "awaiting_selection"
                and "materialize_selected_candidate" not in steps
                and not summary.normal_handoff_ready
            )
            checks[label + ": one candidate with correct fixed-charge total"] = len(candidates) == 1 and all(
                matches_estimate(str(candidate.get("rough_cost", {}).get("cost_range", "")), expected)
                for candidate in candidates
            )
            checks[label + ": displayed and saved totals agree"] = (
                bool(displays)
                and len(candidates) == 1
                and (displays[-1].get("totalMonthlyCost") == candidates[0]["rough_cost"]["cost_range"])
            )
            inventories = [row for candidate in candidates for row in candidate.get("resource_inventory", [])]
            # Usage may be explained next to the total or in the inventory; both are visible in the existing UI.
            visible_prices = " ".join(
                str(display.get("totalMonthlyCost", ""))
                + " "
                + " ".join(str(item.get("monthly_cost", "")) for item in display.get("costItems", []))
                for display in displays
            )
            checks[label + ": usage reference and exclusion remain visible"] = bool(
                re.search(r"流量|traffic", visible_prices, re.I)
                and re.search(r"另计|不计入|excluded|extra", visible_prices, re.I)
                and re.search(r"[¥￥]\s*1\s*(?:～|~|[-–—]|至|到)\s*[¥￥]?\s*5", visible_prices)
            )
            checks[label + ": all displayed resource prices equal saved inventory"] = (
                bool(displays)
                and bool(displays[-1].get("costItems"))
                and all(
                    str(item.get("monthly_cost") or "").strip()
                    in {str(row.get("rough_cost") or "").strip() for row in inventories}
                    for item in displays[-1].get("costItems", [])
                )
            )
            period = r"2\s*(?:天|days?)|48\s*(?:小时|hours?|h\b)" if index == 0 else r"12\s*(?:小时|hours?|h\b)"
            checks[label + ": total uses requested duration"] = bool(candidates) and all(
                re.search(period, candidate["rough_cost"]["cost_range"], re.I) for candidate in candidates
            )
            details = [call["input"] for call in tool_inputs(config_dir) if call["name"] == "show_candidate_detail"]
            submitted_total = details[-1].get("total_cost") if details else None
            checks[label + ": detail total is forwarded without recalculating"] = (
                isinstance(submitted_total, str)
                and len(candidates) == 1
                and candidates[0]["rough_cost"]["cost_range"] == submitted_total.strip()
            )
            turns.append(
                {
                    "turn": index + 1,
                    "seconds": round(time.monotonic() - started, 2),
                    "totals": [candidate["rough_cost"]["cost_range"] for candidate in candidates],
                }
            )
            print(json.dumps({"model": args.model, **turns[-1]}, ensure_ascii=False), flush=True)
    finally:
        server.terminate()
        records = observe.stop()
        checks["server stopped"] = server.process is not None and server.process.poll() is not None
        for filename in (".credentials.yml", "settings.yml"):
            (config_dir / filename).unlink(missing_ok=True)
        checks["credential copies removed"] = not (config_dir / ".credentials.yml").exists()
    observed = sorted(
        {
            str(record.get("attributes", {}).get("gen_ai.request.model"))
            for record in records
            if record.get("attributes", {}).get("gen_ai.request.model")
        }
    )
    checks["only requested real model ran"] = observed == [args.model]
    return {
        "scenario": "plan-cost-refresh-a2a",
        "model": args.model,
        "checks": checks,
        "turns": turns,
        "observed_models": observed,
        "cleanup_status": "not-needed",
        "status": "passed" if all(checks.values()) else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--allow-real-llm", action="store_true")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    if not args.allow_real_llm:
        parser.error("--allow-real-llm is required")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = run(args)
    except Exception as exc:
        summary = {"status": "failed", "error_type": type(exc).__name__, "cleanup_status": "not-needed"}
    (args.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
