#!/usr/bin/env python3
"""Fake aliyun executable that routes validated CLI calls through stdio MCP."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOL_NAME = "alibabacloud___callcli"
BOOTSTRAP_CAPABILITY = "startchat-reconnect-bootstrap-v1"


def _write_error(code: str, message: str) -> int:
    print(json.dumps({"code": code, "message": message}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
    return 1


def _load_ack(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def _wait_for_ack(path: Path, invocation_id: str, event_id: str, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        ack = _load_ack(path)
        if (
            isinstance(ack, dict)
            and ack.get("invocationId") == invocation_id
            and ack.get("eventId") == event_id
            and ack.get("committed") is True
        ):
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("the ROS Agent bridge did not commit the MCP bootstrap event")


def _result_text(result: Any) -> str:
    pieces = []
    for item in getattr(result, "content", []):
        if getattr(item, "type", None) == "text" and isinstance(getattr(item, "text", None), str):
            pieces.append(item.text)
    return "".join(pieces)


async def _call_mcp(argv: list[str]) -> dict[str, Any]:
    server_path = Path(os.environ["IAC_CODE_E2E_MCP_SERVER"]).expanduser().resolve()
    python = os.environ.get("IAC_CODE_E2E_PYTHON") or sys.executable
    mcp_timeout = float(os.environ.get("IAC_CODE_E2E_MCP_TIMEOUT_SECONDS", "120"))
    ack_timeout = float(os.environ.get("IAC_CODE_E2E_BOOTSTRAP_ACK_TIMEOUT_SECONDS", "15"))
    invocation_id = os.environ.get("ALICLOUD_ROS_AGENT_INVOCATION_ID", "")
    ack_file = os.environ.get("ALICLOUD_ROS_AGENT_BOOTSTRAP_ACK_FILE", "")
    protocol = os.environ.get("ALICLOUD_ROS_AGENT_BOOTSTRAP_PROTOCOL", "")
    bootstrap_forwarded = False

    async def progress_callback(_progress: float, _total: float | None, message: str | None) -> None:
        nonlocal bootstrap_forwarded
        if bootstrap_forwarded or not message:
            return
        try:
            progress = json.loads(message)
        except ValueError:
            return
        if not isinstance(progress, dict):
            return
        event = progress.get("event")
        event_id = event.get("id") if isinstance(event, dict) else None
        event_data = event.get("data") if isinstance(event, dict) else None
        if (
            progress.get("kind") != "bootstrap"
            or not isinstance(event_id, str)
            or not event_id
            or not isinstance(event_data, dict)
        ):
            return
        if protocol != BOOTSTRAP_CAPABILITY or not invocation_id or not ack_file:
            raise RuntimeError("the bridge did not provide the bootstrap/ack contract")
        envelope: dict[str, Any] = {"invocationId": invocation_id, "event": event}
        session_id = progress.get("sessionId")
        if isinstance(session_id, str) and session_id:
            envelope["sessionId"] = session_id
        print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), flush=True)
        await _wait_for_ack(Path(ack_file), invocation_id, event_id, ack_timeout)
        bootstrap_forwarded = True

    log_path = Path(os.environ["IAC_CODE_E2E_MCP_STDERR_LOG"]).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    parameters = StdioServerParameters(
        command=python,
        args=[str(server_path)],
        env=dict(os.environ),
        cwd=str(server_path.parent),
    )
    with log_path.open("a", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    TOOL_NAME,
                    {"argv": argv},
                    read_timeout_seconds=timedelta(seconds=mcp_timeout + ack_timeout + 15),
                    progress_callback=progress_callback,
                )
    text = _result_text(result)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise RuntimeError("the E2E MCP server returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("the E2E MCP server returned an invalid result")
    return value


async def _main(argv: list[str]) -> int:
    if argv[:2] not in (["ros", "start-chat"], ["ros", "stop-chat"]):
        return _write_error("UnsupportedCommand", "the fake aliyun CLI accepts only ROS StartChat and StopChat")
    try:
        result = await _call_mcp(argv)
    except TimeoutError as exc:
        return _write_error("BootstrapAckTimeout", str(exc))
    except Exception as exc:
        return _write_error("MCPCallFailed", str(exc)[:1000])
    if result.get("ok") is not True:
        return _write_error(str(result.get("code") or "MCPCallFailed"), str(result.get("message") or "MCP call failed"))
    stdout = result.get("stdout")
    if not isinstance(stdout, str):
        return _write_error("MCPCallFailed", "the E2E MCP result did not contain CLI stdout")
    sys.stdout.write(stdout)
    sys.stdout.flush()
    return int(result.get("returnCode") or 0)


def main() -> int:
    return asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
