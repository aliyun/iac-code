#!/usr/bin/env python3
"""Real LLM/cloud selector answer after an A2A process restart (Normal/Pipeline)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

E2E_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
for root in (REPO_ROOT, E2E_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

import common  # noqa: E402

from iac_code.config import DEFAULT_MODEL, get_config_dir, load_saved_model  # noqa: E402
from iac_code.services.configuration_readiness import configuration_readiness  # noqa: E402
from scripts.a2a.e2e.resource_selector import run_live_resource_selector as live  # noqa: E402

RESTART_STYLES = ("sigterm", "sigkill", "released")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("normal", "pipeline"), default="normal")
    parser.add_argument("--restart-style", choices=RESTART_STYLES, default="sigterm")
    parser.add_argument("--answer", choices=("selected", "canceled"), default="selected")
    parser.add_argument("--region", default="cn-hangzhou")
    parser.add_argument("--source-config-dir", type=Path, default=get_config_dir())
    parser.add_argument("--server-cwd", type=Path, default=REPO_ROOT)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--server-timeout", type=float, default=60)
    parser.add_argument("--turn-timeout", type=float, default=360)
    parser.add_argument("--state-timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if not args.allow_real_cloud:
        parser.error("--allow-real-cloud is required: this test uses the configured real LLM and cloud account")
    return args


def _execution_state(harness: live._Harness, context_id: str) -> dict[str, Any]:
    request = Request(harness.server_url + "/iac-code/execution/state?" + urlencode({"contextId": context_id}))
    try:
        with urlopen(request, timeout=10) as response:
            return {"httpStatus": response.status, "state": json.load(response)}
    except HTTPError as exc:
        return {"httpStatus": exc.code, "state": json.loads(exc.read())}


def _assert_resumed_execution(snapshot: dict[str, Any], before: dict[str, Any], pending: dict[str, Any]) -> None:
    state = snapshot.get("state", {})
    if snapshot.get("httpStatus") != 200:
        raise AssertionError("execution state unavailable at the first resumed SSE frame")
    if state.get("contextId") != pending["contextId"] or state.get("taskId") != pending["requestTaskId"]:
        raise AssertionError("resumed execution belongs to a different context/task")
    if not state.get("executionId") or state["executionId"] == before["state"].get("executionId"):
        raise AssertionError("cold resume did not register a new execution")
    if state.get("phase") != "running" or state.get("executionStatus") != "working":
        raise AssertionError("resumed execution was not working before its first SSE frame")


def _answer_stream(
    harness: live._Harness, pending: dict[str, Any], reply: dict[str, Any], on_first_event: Callable[[], None]
) -> common.StreamSummary:
    """Use the normal wire payload, with a first-frame execution-control assertion."""
    prompt = live.RESOURCE_SELECTION_QUERY_PREFIX + json.dumps(reply, ensure_ascii=False, separators=(",", ":"))
    payload = common.build_message_stream_payload(
        cwd=str(harness.workspace),
        prompt=prompt,
        context_id=pending["contextId"],
        task_id=pending["requestTaskId"],
        request_id=str(uuid.uuid4()),
        message_id=str(uuid.uuid4()),
    )
    common._append_jsonl(harness.run_dir / "requests.jsonl", {"name": "answer", "payload": payload}, harness.env)
    request = Request(
        harness.server_url + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **common.A2A_VERSION_HEADERS},
        method="POST",
    )
    summary = common.StreamSummary(name="answer", prompt=prompt, request_task_id=pending["requestTaskId"])
    path = harness.run_dir / "answer.events.jsonl"
    try:
        with urlopen(request, timeout=harness.args.turn_timeout) as response:
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                body = json.load(response)
                common._append_jsonl(path, body, harness.env)
                raise AssertionError("selection reply did not return an SSE stream")
            for line in response:
                parsed = common._parse_sse_data_line(line)
                if parsed is None:
                    continue
                common._append_jsonl(path, parsed, harness.env)
                if not summary.event_count:
                    on_first_event()
                if isinstance(parsed, dict) and parsed.get("error"):
                    raise AssertionError("selection reply returned a JSON-RPC error")
                common._apply_event(summary, parsed)
    except HTTPError as exc:
        body = common._redact_sensitive_text(exc.read().decode("utf-8", errors="replace"), harness.env)
        common._append_jsonl(path, {"error": "HTTP {}".format(exc.code), "body": body}, harness.env)
        raise AssertionError("selection reply failed with HTTP {}".format(exc.code)) from exc
    if not summary.event_count:
        raise AssertionError("selection reply returned no SSE frames")
    return summary


def _release_execution(harness: live._Harness, before: dict[str, Any], context_id: str) -> None:
    """Explicit release models a replaced sandbox, not the ROS automatic idle timer."""
    state = before["state"]
    payload = {
        "contextId": context_id,
        "expectedExecutionId": state["executionId"],
        "requestId": str(uuid.uuid4()),
        "connectionEpoch": max(0, state["connectionEpoch"]),
        "reason": "natural_completion",
    }
    request = Request(
        harness.server_url + "/iac-code/execution/terminate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        common._write_json(harness.run_dir / "release-ack.json", json.load(response))
    deadline = time.monotonic() + harness.args.state_timeout
    while True:
        snapshot = _execution_state(harness, context_id)
        if snapshot["httpStatus"] == 200 and snapshot["state"].get("releaseReady") is True:
            if snapshot["state"].get("phase") == "terminated":
                common._write_json(harness.run_dir / "released-execution-state.json", snapshot)
                return
        if time.monotonic() >= deadline:
            raise AssertionError("requested execution release did not finish")
        time.sleep(0.1)


def _assert_answer_acknowledged(path: Path, pending: dict[str, Any], status: str) -> None:
    acknowledgments = live._iac_code_values(path, "inputReceived")
    # Pipeline uses durable input_received envelopes rather than Normal metadata.
    for line in path.read_text(encoding="utf-8").splitlines():
        for envelope in common._extract_pipeline_envelopes(json.loads(line)):
            if envelope.get("eventType") == "input_received":
                acknowledgments.append(envelope.get("data"))
    if not any(
        isinstance(item, dict) and item.get("inputId") == pending["inputId"] and item.get("status") == status
        for item in acknowledgments
    ):
        raise AssertionError("resumed input acknowledgment is missing or has the wrong status")


def _assert_turn_ready(summary: common.StreamSummary, *, name: str) -> None:
    live._assert_turn_ready(summary, name=name)
    if summary.last_status_state not in common.NORMAL_TURN_TERMINAL_STATES:
        raise AssertionError(name + " did not finish in a ready state")
    if "TASK_STATE_FAILED" in summary.status_states or "TASK_STATE_CANCELED" in summary.status_states:
        raise AssertionError(name + " failed or was canceled")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.allow_real_cloud:
        raise ValueError("real-cloud opt-in is required")
    run_dir = args.run_dir.expanduser().resolve()
    source = args.source_config_dir.expanduser().resolve()
    # Never copy credentials or change/delete the user's configuration. OAuth
    # refresh must use the original configuration to preserve rotated tokens.
    if run_dir == source or source in run_dir.parents or run_dir in source.parents:
        raise ValueError("run directory must be separate from the source configuration")
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    previous_config_dir = os.environ.get("IAC_CODE_CONFIG_DIR")
    secrets: set[str] = set()
    harness: live._Harness | None = None
    try:
        os.environ["IAC_CODE_CONFIG_DIR"] = str(source)
        secrets = live._secret_values(source)
        live._refresh_source_cloud_credentials(source)
        readiness = configuration_readiness(model=args.model or load_saved_model() or DEFAULT_MODEL)
        common._write_json(run_dir / "readiness.json", readiness)
        if not readiness["llm"]["ready"] or not readiness["cloud"]["ready"]:
            raise RuntimeError("real LLM and Alibaba Cloud credentials are required")
        harness = live._Harness(
            args,
            run_dir=run_dir,
            config_dir=source,
            pipeline_name="resource_selector_stage" if args.mode == "pipeline" else None,
        )
        workspace_config = harness.workspace / ".iac-code"
        workspace_config.mkdir(mode=0o700)
        # Test-local policy only; do not write settings.yml in the source config.
        live._restrict_runtime_permissions(workspace_config)
        harness.start()
        assert harness.server is not None and harness.server.process is not None
        old_pid = harness.server.process.pid
        initial = harness.stream(
            name="initial",
            prompt=(
                "请使用云资源选择器让我选择一个 {region} 地域的已有 VPC。只选择，不创建、修改或删除任何资源；"
                "不要使用 aliyun_api 预先列举资源。请用简短英文关键词 VPC 解析选择器并调用选择工具。"
                "收到选择或取消后，确认结果并结束本轮，不再打开选择器。"
            ).format(region=args.region),
        )
        live._assert_input_required(initial)
        pending = live._single_pending(run_dir=run_dir, stream_name="initial", summary=initial, region=args.region)
        value, label, count, _values = asyncio.run(live._query_real_vpc(pending))
        before = _execution_state(harness, initial.context_id)
        common._write_json(run_dir / "before-restart-execution-state.json", before)
        if before["httpStatus"] != 200 or not before["state"].get("executionId"):
            raise AssertionError("execution state unavailable before restart")
        if args.restart_style == "released":
            _release_execution(harness, before, initial.context_id)
        if args.restart_style == "sigkill":
            harness.restart_after_crash()
        else:
            harness.server.terminate()
            harness.lifecycle.append({"event": "graceful-stop", "index": harness.server_index, "at": time.time()})
            harness.start()
        assert harness.server is not None and harness.server.process is not None
        new_pid = harness.server.process.pid
        if old_pid == new_pid:
            raise AssertionError("test did not restart the A2A process")
        common._write_json(
            run_dir / "after-restart-execution-state.json", _execution_state(harness, initial.context_id)
        )
        first_frame: dict[str, Any] = {}

        def check_first_frame() -> None:
            first_frame.update(_execution_state(harness, initial.context_id))
            common._write_json(run_dir / "first-frame-execution-state.json", first_frame)
            _assert_resumed_execution(first_frame, before, pending)

        reply = live._selection_response(pending, status=args.answer, value=value, label=label, options_empty=False)
        answer = _answer_stream(harness, pending, reply, check_first_frame)
        _assert_turn_ready(answer, name="selection after restart")
        live._assert_no_new_selector(run_dir, stream_name="answer", previous_input_id=pending["inputId"])
        _assert_answer_acknowledged(run_dir / "answer.events.jsonl", pending, args.answer)
        if args.mode == "pipeline" and not answer.normal_handoff_ready:
            raise AssertionError("Pipeline did not complete and hand off after the selector answer")
        token = "RESTART_NEXT_TURN_OK_" + uuid.uuid4().hex[:8]
        next_turn = harness.stream(
            name="next-turn",
            context_id=initial.context_id,
            prompt="这是同一会话的下一条普通消息。不要调用工具，只回复：" + token,
        )
        _assert_turn_ready(next_turn, name="next turn after restart")
        if token not in next_turn.text:
            raise AssertionError("real LLM did not complete the subsequent message")
        forbidden = [
            marker
            for marker in (*live.FORBIDDEN_LOG_MARKERS, "active in another process")
            if any(
                marker.casefold() in line.casefold()
                and not (marker == "different Context" and "was created in a different Context" in line)
                for line in live._server_logs(run_dir).splitlines()
            )
        ]
        if forbidden:
            raise AssertionError("lifecycle regression log markers: " + ", ".join(forbidden))
        result = {
            "passed": True,
            "mode": args.mode,
            "restartStyle": args.restart_style,
            "answer": args.answer,
            "usedRealLlm": True,
            "usedRealCloudQuery": True,
            "configDirIsOriginal": True,
            "selectorId": live.EXPECTED_SELECTOR_ID,
            "region": args.region,
            "candidateCount": count,
            "selectedValueDigest": live._result_digest(value) if args.answer == "selected" else "",
            "contextId": initial.context_id,
            "taskId": initial.task_id,
            "oldPid": old_pid,
            "newPid": new_pid,
            "firstFrameExecutionStateHttpStatus": first_frame["httpStatus"],
            "newExecutionRegistered": True,
            "nextTurnCompleted": True,
            "pipelineHandoffVerified": answer.normal_handoff_ready,
        }
        common._write_json(run_dir / "summary.json", result)
        return result
    except Exception as exc:
        common._write_json(
            run_dir / "summary.json",
            {
                "passed": False,
                "mode": args.mode,
                "restartStyle": args.restart_style,
                "answer": args.answer,
                "errorType": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise
    finally:
        try:
            if harness is not None:
                harness.stop()
        finally:
            try:
                secrets.update(live._secret_values(source))
                live._scrub_artifacts(run_dir, secrets)
            finally:
                if previous_config_dir is None:
                    os.environ.pop("IAC_CODE_CONFIG_DIR", None)
                else:
                    os.environ["IAC_CODE_CONFIG_DIR"] = previous_config_dir


def main() -> None:
    result = _run(_parse_args())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
