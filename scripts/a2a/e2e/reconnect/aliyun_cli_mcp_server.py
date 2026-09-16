#!/usr/bin/env python3
"""stdio MCP server that runs the real aliyun CLI for reconnect E2E tests."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

TOOL_NAME = "alibabacloud___callcli"
MAX_STDOUT_BYTES = (6 * 64 * 1024 * 1024) + (1024 * 1024)
MAX_STDERR_BYTES = 64 * 1024
EXPECTED_ENDPOINT = "ros-pre.aliyuncs.com"
SUPPORTED_SCENARIOS = {"normal", "first-call-timeout", "reconnect-call-timeout"}


class _JSONValueStream:
    """Incrementally decode concatenated JSON values or a top-level array."""

    def __init__(self) -> None:
        self._buffer = ""
        self._array: bool | None = None
        self._array_complete = False
        self._decoder = json.JSONDecoder()

    def feed(self, text: str, *, final: bool = False) -> list[Any]:
        self._buffer += text
        values: list[Any] = []
        while True:
            self._buffer = self._buffer.lstrip()
            if not self._buffer:
                break
            if self._array is None:
                self._array = self._buffer.startswith("[")
                if self._array:
                    self._buffer = self._buffer[1:]
                    continue
            if self._array:
                self._buffer = self._buffer.lstrip()
                if self._buffer.startswith("]"):
                    self._buffer = self._buffer[1:]
                    self._array_complete = True
                    break
                if self._buffer.startswith(","):
                    self._buffer = self._buffer[1:]
                    continue
            try:
                value, end = self._decoder.raw_decode(self._buffer)
            except ValueError:
                break
            values.append(value)
            self._buffer = self._buffer[end:]
            if self._array_complete:
                break
        if final:
            if self._array and not self._array_complete:
                raise ValueError("real aliyun CLI returned an incomplete JSON array")
            if self._buffer.strip():
                raise ValueError("real aliyun CLI returned incomplete JSON")
        return values


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schemaVersion": 1, "requests": []}
    if not isinstance(value, dict) or not isinstance(value.get("requests"), list):
        raise RuntimeError("E2E scenario state is invalid")
    return value


def _mutate_state(path: Path, mutation) -> Any:
    state = _load_state(path)
    result = mutation(state)
    _atomic_json(path, state)
    return result


def _start_request(path: Path, argv: list[str]) -> tuple[int, dict[str, Any]]:
    initial = argv[:2] == ["ros", "start-chat"]
    stop = argv[:2] == ["ros", "stop-chat"]
    if not initial and not stop:
        raise ValueError("the E2E MCP server accepts only ros start-chat and ros stop-chat")

    endpoint = _option_value(argv, "--endpoint")
    if endpoint != EXPECTED_ENDPOINT:
        raise ValueError("the E2E MCP server requires endpoint {}".format(EXPECTED_ENDPOINT))
    if "--profile" in argv:
        raise ValueError("the remote bridge must not pass a local aliyun CLI Profile")
    if "--stream-options" in argv:
        raise ValueError("reconnect StreamOptions must be sent in --body")

    def add(state: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        requests = state["requests"]
        invocation = 0
        if initial:
            invocation = 1 + sum(item.get("operation") == "start-chat" for item in requests if isinstance(item, dict))
        query = _option_value(argv, "--query")
        session_id = _option_value(argv, "--session-id")
        body = _body_object(argv) if initial else {}
        stream_keys = {"StreamOptions.Action", "StreamOptions.Cursor"}
        if set(body) - stream_keys or not all(isinstance(body.get(key), str) for key in set(body) & stream_keys):
            raise ValueError("the reconnect StreamOptions body is invalid")
        stream = {
            key.removeprefix("StreamOptions."): value
            for key, value in body.items()
            if key in stream_keys
        }
        cursor = stream.get("Cursor")
        cursor_identity = None
        cursor_sequence = None
        if cursor:
            identity, separator, sequence = cursor.rpartition(".")
            if separator and identity and sequence.isdigit():
                cursor_identity = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
                cursor_sequence = int(sequence)
        record: dict[str, Any] = {
            "operation": "start-chat" if initial else "stop-chat",
            "invocation": invocation,
            "hasQuery": query is not None,
            "queryHash": hashlib.sha256(query.encode("utf-8")).hexdigest() if query is not None else None,
            "hasSessionId": session_id is not None,
            "sessionHash": (
                hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16] if session_id is not None else None
            ),
            "hasCursor": cursor is not None,
            "streamOptionsInBody": bool(set(body) & stream_keys),
            "cursorIdentityHash": cursor_identity,
            "cursorSequence": cursor_sequence,
            "action": stream.get("Action"),
            "endpointVerified": endpoint == EXPECTED_ENDPOINT,
            "profileApplied": False,
            "bootstrapDelivered": False,
            "bootstrapAcked": False,
            "outcome": "running",
        }
        requests.append(record)
        return len(requests) - 1, record

    return _mutate_state(path, add)


def _update_request(path: Path, index: int, **updates: Any) -> None:
    def update(state: dict[str, Any]) -> None:
        request = state["requests"][index]
        if not isinstance(request, dict):
            raise RuntimeError("E2E request state is invalid")
        request.update(updates)

    _mutate_state(path, update)


def _valid_start_shape(request: dict[str, Any]) -> bool:
    invocation = request.get("invocation")
    if invocation == 1:
        return (
            request.get("hasQuery") is True
            and request.get("hasSessionId") is False
            and request.get("hasCursor") is False
            and request.get("streamOptionsInBody") is False
            and request.get("action") is None
        )
    return (
        isinstance(invocation, int)
        and invocation > 1
        and request.get("hasQuery") is False
        and request.get("hasSessionId") is True
        and request.get("hasCursor") is True
        and request.get("streamOptionsInBody") is True
        and request.get("action") == "Reconnect"
        and isinstance(request.get("cursorIdentityHash"), str)
        and isinstance(request.get("cursorSequence"), int)
    )


def _option_value(argv: list[str], option: str) -> str | None:
    try:
        index = argv.index(option)
    except ValueError:
        return None
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        return None
    return argv[index + 1]


def _body_object(argv: list[str]) -> dict[str, Any]:
    if argv.count("--body") > 1:
        raise ValueError("the aliyun CLI invocation must contain at most one --body")
    raw = _option_value(argv, "--body")
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("the aliyun CLI --body value is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("the aliyun CLI --body value must be a JSON object")
    return value


def _find_first(value: Any, *keys: str) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, ""):
                return value[key]
        for child in value.values():
            found = _find_first(child, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first(child, *keys)
            if found not in (None, ""):
                return found
    return None


def _envelopes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _envelopes(item)
    elif isinstance(value, dict) and isinstance(value.get("id"), str) and isinstance(value.get("data"), dict):
        yield value


def _error_result(code: str, message: str) -> str:
    return json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False, separators=(",", ":"))


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _call_real_cli(argv: list[str], ctx: Context) -> str:
    real_aliyun = os.environ["IAC_CODE_E2E_REAL_ALIYUN"]
    profile = os.environ.get("IAC_CODE_E2E_CLI_IDENTITY", "test-guima")
    timeout_seconds = float(os.environ.get("IAC_CODE_E2E_MCP_TIMEOUT_SECONDS", "120"))
    scenario = os.environ.get("IAC_CODE_E2E_SCENARIO", "normal")
    state_path = Path(os.environ["IAC_CODE_E2E_SCENARIO_STATE"]).expanduser().resolve()
    if scenario not in SUPPORTED_SCENARIOS:
        return _error_result("InvalidE2EScenario", "the E2E reconnect scenario is invalid")
    if timeout_seconds <= 0:
        return _error_result("InvalidE2ETimeout", "the E2E MCP timeout must be positive")

    try:
        state_index, request = _start_request(state_path, argv)
    except (RuntimeError, ValueError) as exc:
        return _error_result("InvalidCLIInvocation", str(exc))
    if request["operation"] == "start-chat" and not _valid_start_shape(request):
        _update_request(state_path, state_index, outcome="invalid-invocation")
        return _error_result(
            "InvalidCLIInvocation",
            "the initial or reconnect StartChat command shape violated the E2E contract",
        )

    executable = [real_aliyun]
    if Path(real_aliyun).suffix.lower() == ".py":
        executable = [os.environ.get("IAC_CODE_E2E_PYTHON") or sys.executable, real_aliyun]
    command = [*executable, *argv, "--profile", profile]
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        _update_request(
            state_path,
            state_index,
            outcome="real-cli-start-error",
            elapsedSeconds=round(time.monotonic() - started, 3),
        )
        return _error_result("RealAliyunCLIStartFailed", str(exc)[:1000])
    _update_request(state_path, state_index, profileApplied=True)
    assert process.stdout is not None and process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    parser = _JSONValueStream()
    utf8_decoder = codecs.getincrementaldecoder("utf-8")("replace")
    bootstrap_sent = False
    force_disconnect = asyncio.Event()

    async def read_stdout() -> None:
        nonlocal bootstrap_sent
        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                break
            stdout.extend(chunk)
            if len(stdout) > MAX_STDOUT_BYTES:
                raise RuntimeError("real aliyun CLI output exceeded the E2E wrapper limit")
            if bootstrap_sent or request["operation"] != "start-chat":
                continue
            for value in parser.feed(utf8_decoder.decode(chunk)):
                for envelope in _envelopes(value):
                    bootstrap_sent = True
                    session_id = _find_first(envelope["data"], "contextId", "context_id", "SessionId")
                    progress = {"kind": "bootstrap", "event": envelope}
                    if isinstance(session_id, str) and session_id:
                        progress["sessionId"] = session_id
                    _update_request(state_path, state_index, bootstrapDelivered=True)
                    await ctx.report_progress(
                        1,
                        1,
                        json.dumps(progress, ensure_ascii=False, separators=(",", ":")),
                    )
                    _update_request(state_path, state_index, bootstrapAcked=True)
                    if scenario == "reconnect-call-timeout" and request["invocation"] == 1:
                        force_disconnect.set()
                    break
                if bootstrap_sent:
                    break
        if not bootstrap_sent:
            parser.feed(utf8_decoder.decode(b"", final=True), final=True)

    async def read_stderr() -> None:
        while True:
            chunk = await process.stderr.read(16 * 1024)
            if not chunk:
                break
            remaining = MAX_STDERR_BYTES - len(stderr)
            if remaining > 0:
                stderr.extend(chunk[:remaining])

    stdout_task = asyncio.create_task(read_stdout())
    stderr_task = asyncio.create_task(read_stderr())
    process_task = asyncio.create_task(process.wait())
    disconnect_task = asyncio.create_task(force_disconnect.wait())
    outcome = "failed"
    try:
        done, _pending = await asyncio.wait(
            {process_task, disconnect_task},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect_task in done and force_disconnect.is_set():
            await _terminate(process)
            outcome = "connection-reset"
            return _error_result(
                "ConnectionReset",
                "E2E injected a transport disconnect after the first committed bootstrap event.",
            )
        if process_task not in done:
            await _terminate(process)
            outcome = "timeout"
            return _error_result(
                "ExecutorTimeout",
                (
                    "Aliyun MCP CORE CallTool tool.mcp.aliyun.core.{}: "
                    "timeout waiting for SSE response after {:.3f}s"
                ).format(
                    TOOL_NAME,
                    timeout_seconds,
                ),
            )
        await asyncio.gather(stdout_task, stderr_task)
        return_code = process_task.result()
        if return_code != 0:
            outcome = "real-cli-error"
            detail = stderr.decode("utf-8", "replace").strip()[:3000]
            _update_request(
                state_path,
                state_index,
                realCLIReturnCode=return_code,
                realCLIError=detail or "the real aliyun CLI failed",
            )
            return _error_result("RealAliyunCLIFailed", detail or "the real aliyun CLI failed")
        outcome = "success"
        return json.dumps(
            {
                "ok": True,
                "returnCode": 0,
                "stdout": stdout.decode("utf-8", "replace"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        await _terminate(process)
        outcome = "mcp-error"
        return _error_result("MCPExecutorFailed", str(exc)[:1000])
    finally:
        disconnect_task.cancel()
        if not stdout_task.done():
            stdout_task.cancel()
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, disconnect_task, return_exceptions=True)
        _update_request(
            state_path,
            state_index,
            outcome=outcome,
            elapsedSeconds=round(time.monotonic() - started, 3),
        )


server = FastMCP("aliyun-cli-reconnect-e2e", log_level="ERROR")


@server.tool(name=TOOL_NAME)
async def alibabacloud_callcli(argv: list[str], ctx: Context) -> str:
    """Run one validated ROS CLI operation with the E2E Profile and timeout."""

    return await _call_real_cli(argv, ctx)


def main() -> int:
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
