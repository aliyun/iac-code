#!/usr/bin/env python3
"""Read-only real-cloud hook/projection and official-document smoke (no LLM)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]


async def run(root: Path) -> dict:
    from iac_code.services.providers.aliyun import AliyunCredentials
    from iac_code.tools.base import ToolContext
    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
    from iac_code.tools.cloud.aliyun.contract_store import canonical_input_sha256
    from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services
    from iac_code.tools.web_fetch import WebFetchTool
    from iac_code.types.permissions import InvocationBinding, ToolPermissionContext

    runtime = create_aliyun_runtime_services(cache_dir=root / "api-cache")
    runtime.credential_provider = lambda: AliyunCredentials.load()
    tool = AliyunApi(services=runtime)
    base = {"RegionId": "cn-hangzhou", "ResourceType": "instance", "InstanceType": "ecs.e-c1m2.large",
            "SystemDisk.Category": "cloud_essd_entry", "SystemDisk.Size": 40, "Period": 1, "PriceUnit": "Hour"}
    inputs = [
        ("normal", "DescribePrice", base, False),
        ("step", "DescribePrice", base, True),
        ("handoff-context", "DescribePrice", base, False),
        ("disk-alias", "DescribePrices", {k: v for k, v in base.items() if not k.startswith("SystemDisk")}
         | {"SystemDisk.1.Category": "cloud_essd_entry", "SystemDisk.Capacity": 40}, False),
        ("month", "DescribePrice", base | {"PriceUnit": "Month"}, False),
        ("monthly-value", "DescribePrice", base | {"PriceUnit": "Monthly"}, False),
        ("periodtype-monthly", "DescribePrice", {k: v for k, v in base.items() if k != "PriceUnit"}
         | {"PeriodType": "Monthly"}, False),
        ("week", "DescribePrice", base | {"PriceUnit": "Week"}, False),
        ("year", "DescribePrice", base | {"PriceUnit": "Year"}, False),
    ]
    bodies, errors = {}, []
    os.environ["IAC_CODE_MODE"] = "pipeline"  # A handed-off normal invocation still needs the full body.
    try:
        for name, action, params, pipeline in inputs:
            try:
                data = tool.prepare_invocation_input({"product": "ecs", "action": action,
                                                      "region_id": "cn-hangzhou", "params": params})
                identifier = uuid.uuid4().hex
                binding = InvocationBinding(runtime_nonce="test-runtime", session_id="test-session",
                                            tool_use_id=identifier, tool_name="aliyun_api",
                                            canonical_input_sha256=canonical_input_sha256(data))
                permission = await tool.check_permissions(data, ToolPermissionContext(
                    cwd=str(root), trusted_read_directories=[str(root)], invocation_binding=binding))
                if permission.behavior != "allow":
                    raise RuntimeError("Read-only permission was not allowed")
                result = await tool.execute(tool_input=data, context=ToolContext(
                    cwd=str(root), tool_use_id=identifier, pipeline_mode=pipeline, invocation_binding=binding,
                    snapshot_id=permission.snapshot_id, security_digest=permission.security_digest,
                    execution_class=permission.execution_class, trusted_read_directories=[str(root)]))
                if result.is_error:
                    errors.append({"case": name, "error": "tool_error"})
                    (root / f"{name}-error.txt").write_text(result.content, encoding="utf-8")
                else:
                    bodies[name] = json.loads(result.content)
                    (root / f"{name}.json").write_text(json.dumps(bodies[name], ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                errors.append({"case": name, "error": type(exc).__name__})
    finally:
        await runtime.aclose()

    def price(name):
        body = bodies.get(name, {})
        return body.get("TradePrice", body.get("PriceInfo", {}).get("Price", {}).get("TradePrice"))

    checks = {
        "all requested real queries succeeded": not errors and len(bodies) == len(inputs),
        "step only exposes payable price and components": bool(bodies.get("step")) and set(bodies["step"]) == {
            "Currency", "PriceUnit", "Period", "TradePrice", "DetailInfos"},
        "normal and handoff retain full raw fields": all("PriceInfo" in bodies.get(n, {})
                                                         for n in ("normal", "handoff-context")),
        "hour and action/disk aliases keep the actual payable price": price("normal") is not None and all(
            price(n) == price("normal") for n in ("step", "handoff-context", "disk-alias")),
        "monthly key and value aliases agree": price("month") is not None and all(
            price(n) == price("month") for n in ("monthly-value", "periodtype-monthly")),
    }
    document = await WebFetchTool().execute(tool_input={
        "url": "https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-describeprice",
        "max_length": 50000,
    }, context=ToolContext(cwd=str(root)))
    (root / "describeprice.md").write_text(document.content, encoding="utf-8")
    checks["official document extracted as article Markdown"] = (
        not document.is_error and "PriceUnit" in document.content
        and ("##" in document.content or "|" in document.content))
    return {"checks": checks, "errors": errors, "trade_prices": {n: price(n) for n in bodies},
            "status": "passed" if all(checks.values()) else "failed", "model_mocked": False,
            "llm_used": False, "cloud_mutations": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--allow-real-cloud", action="store_true", required=True)
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    summary = asyncio.run(run(args.run_dir))
    (args.run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return int(summary["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
