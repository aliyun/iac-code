#!/usr/bin/env python3
"""Verify coupon guide APIs and period-aware deployment planning with real services.

Use run_e2e.py for isolated real-LLM HTTP/SSE runs. --api-only exercises every
documented API recipe with the configured cloud identity, without an LLM.
No cloud resource writes, purchases, coupon changes or model fixtures are used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    fetch_pipeline_state,
    stream_message,
    wait_for_server,
)
from scripts.ci.run_promotional_offer_followup_a2a import (  # noqa: E402
    completion_inputs,
    pipeline_facts,
    visible_pipeline_text,
    walk,
)
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402

PROMPTS = (
    "在阿里云北京给我推荐一个 GPU 云服务器方案，显存 8–16 GiB，内存至少 32 GiB，"
    "用于推理测试。请展示方案等我选择，当前不要部署或创建任何云资源。",
    "我不是说按小时租吗？只租 8 小时，GPU 显存和内存要求不变。先更新方案估算，"
    "不要选择方案或进入部署。",
    "我有 300 元学生优惠券，想用于刚才这个 GPU 部署方案，能抵扣按量付费吗？"
    "我不想再补现金。请查一下当前账号的券，说明哪些适用条件能确认、哪些还不清楚。"
    "不要替我领券、用券、下单或部署。",
)


def hourly_costs_match(outlines: list[str], details: list[str]) -> bool:
    """Priced items need the requested unit; free topology resources have no rate."""
    hour_unit = r"小时|(?<![a-z])(?:hours?|hourly|h)(?![a-z])"
    period_pattern = rf"{hour_unit}|月|months?"

    def hourly(value: str) -> bool:
        # Check the stated estimate period, not a later explanation such as "非月费".
        period = re.search(period_pattern, value, re.I)
        if period is None:
            return False
        if re.fullmatch(hour_unit, period.group(), re.I):
            return True
        # A monthly reference is valid only when followed by an explicit hourly quote.
        converted = re.split(r"→|->|=>|折合|折算|换算", value, maxsplit=1)
        return len(converted) == 2 and bool(re.search(
            rf"(?:[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*元)\s*/\s*(?:\d+\s*)?(?:{hour_unit})",
            converted[1], re.I,
        ))

    def free(value: str) -> bool:
        amounts = re.findall(r"[¥￥]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*元", value)
        if amounts:
            return all(Decimal(yen or yuan) == 0 for yen, yuan in amounts)
        if re.search(r"月|months?|未包?含|不包?含|not\s+included", value, re.I):
            return False
        return bool(
            re.fullmatch(r"\s*0(?:\.0+)?\s*", value)
            or re.search(r"免费|无费用|免收|(?:包含|含)在|included\s+in", value, re.I)
        )

    return bool(outlines) and all(hourly(value) for value in outlines) and all(
        hourly(value) or free(value) for value in details
    )


async def verify_apis(run_dir: Path) -> dict[str, Any]:
    from iac_code.services.providers.aliyun import AliyunCredentials
    from iac_code.tools.base import ToolContext
    from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services

    runtime = create_aliyun_runtime_services(cache_dir=run_dir / "api-cache")
    runtime.credential_provider = lambda: AliyunCredentials.refresh_oauth_if_needed(AliyunCredentials.load())
    evidence: list[dict[str, Any]] = []

    async def call(action: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await runtime.internal_caller.call(
            tool_input={
                "product": "BssOpenApi", "version": "2023-09-30", "action": action,
                "region_id": "cn-hangzhou", "params": params,
            },
            context=ToolContext(cwd=str(run_dir), pipeline_mode=True),
        )
        if result.is_error:
            raise RuntimeError(f"{action} did not return a successful business result")
        body = json.loads(result.content)
        if not isinstance(body.get("Data"), list):
            raise ValueError(f"{action} returned an unexpected Data shape")
        evidence.append({
            "action": action, "version": "2023-09-30", "page": params["CurrentPage"],
            "count": len(body["Data"]), "total": body["TotalCount"],
            "fields": sorted({key for item in body["Data"] for key in item}),
        })
        return body

    try:
        coupons = await call("DescribeCoupon", {"CurrentPage": 1, "PageSize": 20})
        required = {
            "CouponId", "CouponType", "CouponTypeName", "Amount", "RemainAmount", "Currency",
            "Status", "StartTime", "EndTime", "YhOrderTypes", "MoneyLimit", "OrderTimeRule",
            "FirstBuy", "Remark", "ItemNames", "SuitItemType",
        }
        if not coupons["Data"]:
            raise RuntimeError("A real coupon is required to verify the documented fields and item query")
        present = {key for coupon in coupons["Data"] for key in coupon}
        if not required <= present:
            raise ValueError("Documented coupon fields have not all been observed in real responses")
        for coupon in coupons["Data"]:
            Decimal(coupon["Amount"])
            Decimal(coupon["RemainAmount"])
        if coupons["TotalCount"] > len(coupons["Data"]):
            await call("DescribeCoupon", {"CurrentPage": 2, "PageSize": 20})
        items = await call("DescribeCouponItemList", {
            "CouponId": int(coupons["Data"][0]["CouponId"]), "CurrentPage": 1, "PageSize": 20,
        })
        if not items["Data"] or not all({"Code", "Name"} <= item.keys() for item in items["Data"]):
            raise ValueError("A non-empty real coupon item list is required to verify Code and Name")
        if items["TotalCount"] > len(items["Data"]):
            await call("DescribeCouponItemList", {
                "CouponId": int(coupons["Data"][0]["CouponId"]), "CurrentPage": 2, "PageSize": 20,
            })
        return {"passed": True, "calls": evidence, "settlement_verified": False}
    finally:
        await runtime.aclose()


def tool_inputs(config_dir: Path) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in sorted((config_dir / "projects").rglob("session.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            for node in walk(json.loads(line)):
                if node.get("type") == "tool_use":
                    found[str(node["id"])] = {
                        "id": node["id"], "name": node.get("name"), "input": node.get("input", {}),
                    }
    return list(found.values())


def successful_coupon_queries(config_dir: Path) -> set[str]:
    successful = set()
    for path in (config_dir / "projects").rglob("session.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            for node in walk(json.loads(line)):
                if node.get("type") != "tool_result" or node.get("is_error") is not False:
                    continue
                try:
                    body = json.loads(node.get("content", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(body, dict) and isinstance(body.get("Data"), list) and "TotalCount" in body:
                    successful.add(node["tool_use_id"])
    return successful


def run_a2a(args: argparse.Namespace) -> dict[str, Any]:
    config_dir = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config_dir.resolve().is_relative_to(args.run_dir.resolve()):
        raise ValueError("Run through run_e2e.py with isolated configuration")
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
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
    )
    checks: dict[str, bool] = {}
    turns = []
    context_id = task_id = ""
    try:
        server.start()
        wait_for_server(f"http://127.0.0.1:{port}", timeout=60, owned_server=server)
        for index, prompt in enumerate(PROMPTS):
            started = time.monotonic()
            before = len(tool_inputs(config_dir))
            summary = stream_message(
                server_url=f"http://127.0.0.1:{port}", cwd=str(workspace), prompt=prompt,
                context_id=context_id, task_id=task_id, name=f"turn-{index + 1}",
                run_dir=args.run_dir, timeout=args.timeout, redaction_env=env,
            )
            context_id, task_id = summary.context_id, summary.task_id
            fetch_pipeline_state(
                server_url=f"http://127.0.0.1:{port}", context_id=context_id, task_id=task_id,
                run_dir=args.run_dir, name=f"turn-{index + 1}", redaction_env=env,
            )
            conclusion, steps = pipeline_facts(args.run_dir / f"turn-{index + 1}.events.jsonl")
            completions = completion_inputs(config_dir)
            conclusion = conclusion or (completions[-1] if completions else {})
            calls = tool_inputs(config_dir)[before:]
            outline_costs = [
                str(candidate.get("total_cost", candidate.get("total_monthly_cost", "")))
                for call in calls if call["name"] == "show_architecture_plan"
                for candidate in call["input"].get("candidates", [])
            ]
            outline_costs.extend(
                str(call["input"].get("total_cost", ""))
                for call in calls if call["name"] == "show_candidate_detail"
            )
            detail_costs = [
                str(resource.get("rough_cost", resource.get("rough_monthly_cost", "")))
                for call in calls if call["name"] == "show_candidate_detail"
                for resource in call["input"].get("resource_inventory", [])
                if resource.get("rough_cost", resource.get("rough_monthly_cost"))
            ]
            text = visible_pipeline_text(args.run_dir / f"turn-{index + 1}.events.jsonl")
            checks[f"turn {index + 1}: real HTTP/SSE response"] = summary.response_content_type == "text/event-stream"
            checks[f"turn {index + 1}: remains in deployment planning"] = (
                not summary.normal_handoff_ready and conclusion.get("status") != "rejected"
                and "materialize_selected_candidate" not in steps
                and summary.last_status_state == "TASK_STATE_INPUT_REQUIRED"
            )
            if index == 0:
                checks["default estimate is monthly"] = bool(outline_costs) and all(
                    re.search(r"月|month", cost, re.I) for cost in outline_costs
                )
            elif index == 1:
                checks["explicit hourly request updates total and details"] = hourly_costs_match(
                    outline_costs, detail_costs,
                )
                checks["eight-hour duration is explained"] = bool(re.search(
                    r"8\s*(?:个)?\s*小时|8\s*hours?|八小时", text + json.dumps(calls, ensure_ascii=False), re.I,
                ))
            else:
                checks["coupon guide was read on demand"] = any(
                    call["name"] == "read_file" and "coupons.md" in json.dumps(call["input"])
                    for call in calls
                )
                checks["coupon inquiry uses a real cloud API"] = any(
                    call["name"] == "aliyun_api" and call["input"].get("action") == "DescribeCoupon"
                    and call["id"] in successful_coupon_queries(config_dir)
                    for call in calls
                )
                checks["coupon inquiry does not redirect to assistant"] = "ros.console.aliyun.com/" not in text
            turns.append({
                "turn": index + 1, "seconds": round(time.monotonic() - started, 2),
                "task_state": summary.last_status_state, "outline_costs": outline_costs,
                "detail_costs": detail_costs, "started_steps": steps,
                "tools": [call["name"] for call in calls],
            })
            print(json.dumps({"model": args.model, **turns[-1]}, ensure_ascii=False), flush=True)
        calls = tool_inputs(config_dir)
        legacy_cost_fields = {"total_monthly_cost", "rough_monthly_cost", "monthly_range", "monthly_cost"}
        checks["planning tool inputs use neutral cost names"] = all(
            not legacy_cost_fields.intersection(node)
            for call in calls if call["name"] in {"show_architecture_plan", "show_candidate_detail"}
            for node in walk(call["input"])
        )
        checks["no cloud writes or deployment tools"] = all(
            call["input"].get("action", "").startswith(("Describe", "Get", "List", "Query"))
            for call in calls if call["name"] == "aliyun_api"
        ) and not any(call["name"].startswith("ros_deploy") for call in calls)
    finally:
        server.terminate()
        records = observe.stop()
        for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
            (config_dir / name).unlink(missing_ok=True)
    observed = sorted({
        str(record.get("attributes", {}).get("gen_ai.request.model"))
        for record in records if record.get("attributes", {}).get("gen_ai.request.model")
    })
    checks["requested real model only"] = observed == [args.model]
    return {"checks": checks, "turns": turns, "observed_models": observed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--python")
    parser.add_argument("--timeout", type=float, default=420)
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--allow-real-cloud", action="store_true")
    args = parser.parse_args()
    if not args.allow_real_cloud:
        parser.error("--allow-real-cloud is required")
    args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logging.disable(logging.CRITICAL)
    from loguru import logger
    logger.remove()
    result: dict[str, Any] = {"scenario": "coupon-budget-a2a", "cleanup_status": "not-needed"}
    try:
        result["api_verification"] = asyncio.run(verify_apis(args.run_dir))
        if not args.api_only:
            result.update(run_a2a(args))
        result["status"] = "passed" if all(result.get("checks", {}).values()) else "failed"
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
    (args.run_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
