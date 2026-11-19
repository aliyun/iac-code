"""Test-only A2A launcher: real cloud responses, with a dispatch fence against writes."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from pathlib import Path
from urllib.parse import parse_qsl

READ_PREFIXES = ("Describe", "List", "Get", "Query")


def explicit_ecs_dry_run(call: dict) -> bool:
    value = call.get("input", {})
    flag = value.get("params", {}).get("DryRun")
    return (
        call.get("name") == "aliyun_api" and str(value.get("product", "")).casefold() == "ecs"
        and value.get("action") == "RunInstances" and (flag is True or flag == "true")
    )


def wire_kind(product: str, action: str, query, body: bytes | None, content_type: str) -> str:
    if action.startswith(READ_PREFIXES):
        return "read"
    values = list(query)
    if body:
        if "json" in content_type:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                return "blocked"
            values.extend((key, "true" if value is True else str(value)) for key, value in payload.items())
        else:
            values.extend(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    flags = [value for key, value in values if key == "DryRun"]
    allowed = product.casefold() == "ecs" and action == "RunInstances" and flags == ["true"]
    return "ecs_dry_run" if allowed else "blocked"


def install_guard(audit: Path) -> None:
    from iac_code.tools.cloud.aliyun.acs3_transport import PreparedTransportCall

    def record(value):
        with audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), **value}) + "\n")

    original = PreparedTransportCall.execute

    async def guarded(self, *, budget):
        kind = wire_kind(
            self.contract.product, self.contract.action, self.request.canonical_query, self.request.body,
            self.request.headers.get("content-type", ""),
        )
        record({"event": "blocked" if kind == "blocked" else "dispatch", "kind": kind,
                "product": self.contract.product, "action": self.contract.action,
                "tool_use_id": self.context.tool_use_id})
        if kind == "blocked":
            raise RuntimeError("E2E cloud dispatch blocked: only reads or explicit ECS RunInstances DryRun=true")
        return await original(self, budget=budget)

    PreparedTransportCall.execute = guarded
    record({"event": "guard_installed"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    install_guard(args.audit)
    sys.argv = ["iac_code.cli.main", "a2a", "--config", args.config]
    runpy.run_module("iac_code.cli.main", run_name="__main__")


if __name__ == "__main__":
    main()
