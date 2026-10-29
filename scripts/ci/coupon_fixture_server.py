"""Test-process-only A2A bootstrap: substitute BSS results, preserve the real LLM."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.ci.coupon_fixtures import CASES, response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--calls", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    from iac_code.cli.main import app
    from iac_code.tools.base import ToolResult
    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi

    original = AliyunApi._execute_runtime_unattached

    async def execute(tool, *, api_input, **kwargs):
        product = str(api_input.get("product", "")).casefold()
        action = str(api_input.get("action", ""))
        if product in {"bssopenapi", "bss-openapi", "bss"}:
            case = json.loads(args.state.read_text(encoding="utf-8"))["case"]
            if case not in CASES:
                return ToolResult.error("Unknown coupon fixture")
            params = api_input.get("params") or {}
            with args.calls.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"case": case, "action": action, "mocked": True}) + "\n")
            if case == "query_denied":
                return ToolResult.error("AccessDenied: not authorized to query the current account's coupons.")
            if api_input.get("version") != "2023-09-30":
                return ToolResult.error("Coupon fixtures require the documented 2023-09-30 API version.")
            if action not in {"DescribeCoupon", "DescribeCouponItemList"}:
                return ToolResult.error("Only documented coupon read APIs are supported by this fixture.")
            return ToolResult.success(json.dumps(response(case, action, params), ensure_ascii=False))
        if not action.startswith(("Describe", "Get", "List", "Query")):
            with args.calls.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"blocked_cloud_write": True, "action": action}) + "\n")
            return ToolResult.error("Cloud writes are blocked by this read-only test server.")
        return await original(tool, api_input=api_input, **kwargs)

    # This entry point is never imported by the application or activated by a production flag.
    with patch.object(AliyunApi, "_execute_runtime_unattached", execute):
        sys.argv = [sys.argv[0], "a2a", "--config", str(args.config)]
        app()


if __name__ == "__main__":
    main()
