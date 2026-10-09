#!/usr/bin/env python3
"""Opt-in, real LLM/cloud AG-UI HTTP/SSE resource-selector matrix."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

E2E_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
for import_root in (E2E_ROOT, REPO_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from common import ManagedServer, _free_port, _server_env, _write_server_config, wait_for_server  # noqa: E402

from iac_code.a2a.client import A2AClient  # noqa: E402
from iac_code.agui.events import a2a_inputs, a2a_state  # noqa: E402
from iac_code.config import DEFAULT_MODEL, load_saved_model  # noqa: E402
from iac_code.services.configuration_readiness import configuration_readiness  # noqa: E402
from scripts.a2a.e2e.resource_selector.run_live_resource_selector import _query_real_vpc  # noqa: E402

SCENARIOS = (
    "normal-selected",
    "normal-canceled",
    "normal-direct-input",
    "pipeline-selected",
    "pipeline-canceled",
    "pipeline-direct-input",
)
SELECTOR_ID = "vpc.vpc"
PIPELINE_LOCAL_PERMISSION_INSTRUCTIONS = (
    "本次本地权限允许只读工具，以及在当前工作目录内使用 write_file/edit_file 生成方案文件；"
    "不授权具有写入能力的 shell 执行或工作目录外写入。请使用 read_file 读取本地说明，"
    "不要通过 bash 生成、复制或检查文件；在生成模板前先调用云资源选择器。"
)
_FAILURE_MESSAGES = {
    "AG-UI did not publish the A2A session coordinates": "session_coordinates_missing",
    "AG-UI session coordinates are incomplete": "session_coordinates_incomplete",
    "resource selection resumed a different A2A task or context": "task_coordinates_changed",
    "AG-UI did not finish the resource selector tool span": "tool_result_missing",
    "pipeline did not reach the resource selector after five turns": "selector_turn_budget",
    "pipeline exceeded five pre-selector permission rounds": "permission_turn_budget",
    "pipeline repeated resolved permission": "permission_repeated",
    "pipeline permission identity is missing or duplicated": "permission_identity_invalid",
    "pipeline exceeded pre-selector deadline": "selector_wait_deadline",
    "AG-UI did not start an SSE run": "run_not_started",
    "AG-UI SSE run has no terminal event": "stream_not_finished",
    "pipeline did not present one actionable pre-selector interrupt": "preselector_input_shape",
    "expected exactly one resource selector interrupt": "selector_input_shape",
    "the real LLM did not call the cloud resource selector": "selector_missing",
    "the selector contract is not vpc.vpc": "selector_contract_mismatch",
    "AG-UI did not expose a response schema": "response_schema_missing",
    "AG-UI selector coordinates differ from its A2A task": "selector_coordinates_mismatch",
}
_RUN_ERROR_CODES = (
    "A2A_PROTOCOL_ERROR", "A2A_EXECUTION_FAILED", "A2A_UNAVAILABLE", "CANCELLED", "EXECUTION_LOST",
    "INTERNAL_ERROR", "RESUME_PAYLOAD_INVALID", "STATE_PERSISTENCE_FAILED", "INVALID_INPUT", "CONFLICT",
    "RESUME_ALREADY_APPLIED", "SESSION_BACKUP_NOT_READY",
)
AGUI_FAILURE_REASONS = frozenset((
    *_FAILURE_MESSAGES.values(), "agui_run_error", "http_error", "other",
    *("agui_run_error:" + code.lower() for code in _RUN_ERROR_CODES),
))
_STREAM_TYPES = frozenset({"RUN_STARTED", "RUN_FINISHED", "STEP_STARTED", "STEP_FINISHED", "TOOL_CALL_RESULT"})
_PIPELINE_TRACE_TYPES = frozenset({
    "step_failed", "pipeline_error", "pipeline_completed", "pipeline_started",
    "rollback_triggered", "rollback_completed",
})
AGUI_STREAM_TRACE_CODES = frozenset({
    *_STREAM_TYPES, "RUN_ERROR:other", *("RUN_ERROR:" + code for code in _RUN_ERROR_CODES),
    *("pipeline:" + kind for kind in _PIPELINE_TRACE_TYPES),
})


def _agui_stream_trace(events: list[dict[str, Any]]) -> list[str]:
    """Retain only protocol milestones, never messages, identifiers, or error bodies."""
    trace = []
    for event in events:
        kind = event.get("type")
        if isinstance(kind, str) and kind in _STREAM_TYPES:
            trace.append(kind)
        elif kind == "RUN_ERROR":
            code = event.get("code")
            trace.append("RUN_ERROR:" + (code if code in _RUN_ERROR_CODES else "other"))
        elif kind == "CUSTOM" and event.get("name") == "iac-code.pipeline.v1":
            value = event.get("value")
            pipeline_type = value.get("eventType") if isinstance(value, dict) else None
            if isinstance(pipeline_type, str) and pipeline_type in _PIPELINE_TRACE_TYPES:
                trace.append("pipeline:" + pipeline_type)
    return trace[-40:]


def _failure_reason(exc: Exception) -> str:
    message = str(exc)
    if message.startswith("AG-UI run error:"):
        for code in _RUN_ERROR_CODES:
            if message == "AG-UI run error: " + code:
                return "agui_run_error:" + code.lower()
        return "agui_run_error"
    if message.startswith("AG-UI HTTP status "):
        return "http_error"
    return _FAILURE_MESSAGES.get(message, "other")


async def _failed_resume_diagnostics(url: str, task_id: str, run_dir: Path) -> dict[str, Any]:
    """Inspect the failed task read-only, retaining states and counts, never bodies or IDs."""
    client = A2AClient(timeout_seconds=5)
    try:
        task = await client.get_task(url, task_id, history_length=0)
        state = a2a_state(task)
        inputs = a2a_inputs(task)
        applied: set[str] = set()
        for path in list((run_dir / 'agui-state').rglob('*.json'))[:20]:
            if path.is_symlink() or path.stat().st_size > 1_000_000:
                continue
            try:
                document = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict) or document.get('execution', {}).get('taskId') != task_id:
                continue
            execution_id = document['execution'].get('executionId')
            applied.update(row['interruptId'] for row in document.get('appliedResumeDigests', [])
                           if isinstance(row, dict) and row.get('executionId') == execution_id
                           and isinstance(row.get('interruptId'), str))
        return {
            'aguiA2aTaskState': state if state in {
                'submitted', 'working', 'input-required', 'completed', 'failed', 'canceled', 'rejected',
            } else 'unknown',
            'aguiA2aPendingInputCount': len(inputs),
            'aguiA2aPendingAlreadyAppliedCount': sum(value.get('inputId') in applied for value in inputs),
        }
    except Exception:
        return {'aguiA2aTaskState': 'unavailable'}
    finally:
        await client.aclose()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--region", default="cn-hangzhou")
    parser.add_argument("--model", default="")
    parser.add_argument("--turn-timeout", type=float, default=600)
    args = parser.parse_args()
    if not args.allow_real_cloud:
        parser.error("--allow-real-cloud is required: this runner calls a real LLM and Alibaba Cloud")
    return args


def _agui_request(url: str, payload: dict[str, Any], *, timeout: float) -> list[dict[str, Any]]:
    request = Request(
        url.rstrip("/") + "/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    try:
        with urlopen(request, timeout=timeout) as response:
            for line in response:
                if not line.startswith(b"data:"):
                    continue
                event = json.loads(line[5:].decode("utf-8"))
                if isinstance(event, dict):
                    events.append(event)
    except HTTPError as exc:
        raise AssertionError("AG-UI HTTP status {}".format(exc.code)) from exc
    if not events or events[0].get("type") != "RUN_STARTED":
        raise AssertionError("AG-UI did not start an SSE run")
    terminal = events[-1]
    if terminal.get("type") not in {"RUN_FINISHED", "RUN_ERROR"}:
        raise AssertionError("AG-UI SSE run has no terminal event")
    if terminal.get("type") == "RUN_ERROR":
        error = AssertionError("AG-UI run error: {}".format(terminal.get("code")))
        error.agui_stream_trace = _agui_stream_trace(events)
        raise error
    return events


def _run_payload(
    *,
    thread_id: str,
    invocation_id: str,
    cwd: Path,
    run_mode: str,
    prompt: str | None = None,
    resume: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if (prompt is None) == (resume is None):
        raise ValueError("provide either a prompt or a resume")
    props: dict[str, Any] = {
        "schemaVersion": 1,
        "rosInvocationId": invocation_id,
        "cwd": str(cwd),
        "runMode": run_mode,
    }
    if run_mode == "pipeline":
        props["pipelineName"] = "selling_solution_first"
    return {
        "threadId": thread_id,
        "runId": "run-" + uuid.uuid4().hex,
        "state": {},
        "messages": [] if resume is not None else [{"id": uuid.uuid4().hex, "role": "user", "content": prompt}],
        "tools": [],
        "context": [],
        "forwardedProps": {"iacCode": props},
        **({"resume": resume} if resume is not None else {}),
    }


def _interrupts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    terminal = events[-1]
    outcome = terminal.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("type") != "interrupt":
        return []
    values = outcome.get("interrupts")
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _selector_interrupt(interrupts: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in interrupts
            if isinstance(item.get("metadata"), dict) and item["metadata"].get("kind") == "cloud_resource_selection"
        ),
        None,
    )


def _session_coordinates(events: list[dict[str, Any]]) -> tuple[str, str]:
    sessions = [
        item.get("value")
        for item in events
        if item.get("type") == "CUSTOM" and item.get("name") == "iac-code.session.v1"
    ]
    if not sessions or not isinstance(sessions[-1], dict):
        raise AssertionError("AG-UI did not publish the A2A session coordinates")
    task_id, context_id = sessions[-1].get("taskId"), sessions[-1].get("contextId")
    if not isinstance(task_id, str) or not task_id or not isinstance(context_id, str) or not context_id:
        raise AssertionError("AG-UI session coordinates are incomplete")
    return task_id, context_id


def _workspace_file_permission(metadata: dict[str, Any], cwd: Path) -> bool:
    """Allow local materialization within this isolated case workspace."""
    if metadata.get('toolName') not in {'write_file', 'edit_file'}:
        return False
    target = metadata.get('target')
    if (not isinstance(target, str) or not target.strip()
        or any(marker in target for marker in (' · ', '\n', '\x00', '<', '>', '...', '…'))):
        return False
    path = Path(target)
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve().is_relative_to(cwd.resolve())
    except (OSError, ValueError):
        return False


def _advance_pipeline(
    url: str,
    *,
    initial: list[dict[str, Any]],
    thread_id: str,
    invocation_id: str,
    cwd: Path,
    timeout: float,
    progress: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    events = initial
    progress = progress if progress is not None else {}
    conversation_turns = permission_turns = 0
    # Permissions are transport-level handshakes, not candidate/question turns.
    # Bound them by the existing deadline and reject replayed resolutions; a
    # legitimate sequence of distinct reads need not contain only five calls.
    deadline = time.monotonic() + timeout
    resolved_permissions: set[str] = set()
    while True:
        count = conversation_turns + permission_turns
        progress["leadInTurns"] = count
        interrupts = _interrupts(events)
        if _selector_interrupt(interrupts) is not None:
            return events, count
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("pipeline exceeded pre-selector deadline")
        permissions = [item for item in interrupts if isinstance(item.get("metadata"), dict)
                       and item["metadata"].get("kind") == "permission"]
        if permissions and len(permissions) == len(interrupts):
            # Record the blocked round's shape too, without commands, paths or IDs.
            progress["leadInPendingReadOnlyPermissions"] = sum(
                item["metadata"].get("isReadOnly") is True for item in permissions)
            progress["leadInPendingShellPermissions"] = sum(
                item["metadata"].get("toolName") == "bash" for item in permissions)
            progress["leadInPendingWorkspaceWrites"] = sum(
                _workspace_file_permission(item["metadata"], cwd) for item in permissions)
            identities = [item.get('id') for item in permissions]
            if (any(not isinstance(identity, str) or not identity for identity in identities)
                or len(set(identities)) != len(identities)):
                raise AssertionError("pipeline permission identity is missing or duplicated")
            if resolved_permissions.intersection(identities):
                raise AssertionError("pipeline repeated resolved permission")
            resolved_permissions.update(identities)
            permission_turns += 1
            progress["leadInPermissionTurns"] = permission_turns
            # Materialization writes local templates even when cloud operations
            # are read-only. Keep those writes inside the case workspace.
            responses = [{"interruptId": item["id"], "status": "resolved",
                          "payload": {"decision": "allow_once" if item["metadata"].get("isReadOnly") is True
                                      or _workspace_file_permission(item['metadata'], cwd)
                                      else "deny"}} for item in permissions]
            progress['leadInAllowedWorkspaceFilePermissions'] = progress.get(
                'leadInAllowedWorkspaceFilePermissions', 0) + sum(
                    _workspace_file_permission(item['metadata'], cwd) for item in permissions)
            progress["leadInDeniedShellPermissions"] = progress.get("leadInDeniedShellPermissions", 0) + sum(
                item["metadata"].get("toolName") == "bash" and item["metadata"].get("isReadOnly") is not True
                for item in permissions)
            progress["leadInDeniedLocalFilePermissions"] = progress.get("leadInDeniedLocalFilePermissions", 0) + sum(
                item["metadata"].get("toolName") in {"write_file", "edit_file", "write", "edit"}
                and item["metadata"].get("isReadOnly") is not True
                and not _workspace_file_permission(item['metadata'], cwd) for item in permissions)
            events = _agui_request(
                url, _run_payload(thread_id=thread_id, invocation_id=invocation_id, cwd=cwd,
                                  run_mode="pipeline", resume=responses), timeout=remaining,
            )
            continue
        if conversation_turns >= 5:
            raise AssertionError("pipeline did not reach the resource selector after five turns")
        if len(interrupts) != 1:
            raise AssertionError("pipeline did not present one actionable pre-selector interrupt")
        interrupt = interrupts[0]
        metadata = interrupt.get("metadata")
        kind = metadata.get("kind") if isinstance(metadata, dict) else None
        if kind == "candidate_selection":
            progress["leadInCandidateTurns"] = progress.get("leadInCandidateTurns", 0) + 1
            options = metadata.get("standardOptions")
            if not isinstance(options, list) or not options:
                raise AssertionError("candidate selection has no options")
            answer = {"selectedId": options[0]["id"]}
        elif kind == "ask_user_question":
            progress["leadInQuestionTurns"] = progress.get("leadInQuestionTurns", 0) + 1
            answer = {"freeText": "只引用杭州已有 VPC，不创建任何云资源。"}
        else:
            raise AssertionError("unexpected pre-selector interrupt: {}".format(kind))
        conversation_turns += 1
        progress["leadInConversationTurns"] = conversation_turns
        events = _agui_request(
            url,
            _run_payload(
                thread_id=thread_id,
                invocation_id=invocation_id,
                cwd=cwd,
                run_mode="pipeline",
                resume=[{"interruptId": interrupt["id"], "status": "resolved", "payload": answer}],
            ),
            timeout=remaining,
        )


def _wait_agui(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("AG-UI server exited with code {}".format(process.returncode))
        try:
            with urlopen(url + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (TimeoutError, URLError, OSError):
            time.sleep(0.1)
    raise TimeoutError("AG-UI HTTP server did not become healthy")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.expanduser().resolve()
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = run_dir / "workspace"
    workspace.mkdir(mode=0o700)
    model = args.model or load_saved_model() or DEFAULT_MODEL
    readiness = configuration_readiness(model=model)
    if not readiness["llm"]["ready"] or not readiness["cloud"]["ready"]:
        raise RuntimeError("LLM and Alibaba Cloud credentials are both required")

    a2a_port = _free_port("127.0.0.1")
    agui_port = _free_port("127.0.0.1")
    a2a_url = "http://127.0.0.1:{}".format(a2a_port)
    agui_url = "http://127.0.0.1:{}".format(agui_port)
    mode, action = args.scenario.split("-", 1)
    env = _server_env(os.environ.copy(), provider="", model=args.model, api_base="")
    env["IAC_CODE_A2A_RESOURCE_SELECTOR_ENABLED"] = "true"
    env["IAC_CODE_A2A_SAFE_MODE"] = "true" if mode == "normal" else "false"
    env["IAC_CODE_PIPELINE_NAME"] = "selling_solution_first"
    a2a = ManagedServer(
        python_cmd=[sys.executable],
        config_path=_write_server_config(run_dir, host="127.0.0.1", port=a2a_port, auto_approve_permissions=False),
        process_cwd=str(REPO_ROOT),
        allowed_cwd=str(workspace),
        env=env,
        log_prefix=run_dir / "a2a",
        run_mode=mode,
    )
    agui: subprocess.Popen[str] | None = None
    agui_stdout = (run_dir / "agui.stdout.log").open("w", encoding="utf-8")
    agui_stderr = (run_dir / "agui.stderr.log").open("w", encoding="utf-8")
    checks: dict[str, bool] = {}
    progress: dict[str, int] = {}
    selector_task_id: str | None = None
    stage = "AGUI servers ready"
    try:
        a2a.start()
        wait_for_server(a2a_url, timeout=60)
        agui_env = dict(env)
        agui_env["IAC_CODE_AGUI_ALLOWED_CWDS"] = str(workspace)
        agui = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "iac_code.cli.main",
                "agui",
                "--host",
                "127.0.0.1",
                "--port",
                str(agui_port),
                "--a2a-url",
                a2a_url,
                "--state-dir",
                str(run_dir / "agui-state"),
            ],
            cwd=REPO_ROOT,
            env=agui_env,
            stdout=agui_stdout,
            stderr=agui_stderr,
            text=True,
        )
        _wait_agui(agui_url, agui)
        checks[stage] = True

        thread_id = "agui-selector-" + uuid.uuid4().hex
        invocation_id = "e2e-" + uuid.uuid4().hex
        if mode == "pipeline":
            prompt = (
                "请做一个杭州地域的最小 ROS 方案：仅引用一个已有 VPC，不创建任何云资源。"
                "先给我选择方案，再在生成模板之前使用云资源选择器让我选择 VPC；"
                "不部署，不调用任何云写接口。"
            ) + PIPELINE_LOCAL_PERMISSION_INSTRUCTIONS
            if action == "canceled":
                prompt += "如果我取消 VPC 选择，我仍要继续这个方案；请返回已有方案列表，让我重新选择方案。"
        else:
            prompt = (
                "请使用云资源选择器让我选择一个 {region} 地域的已有 VPC。"
                "只选择，不创建、修改或删除资源；解析时使用简短英文关键词 VPC。"
            ).format(region=args.region)
        stage = "AGUI selector interrupt"
        initial = _agui_request(
            agui_url,
            _run_payload(
                thread_id=thread_id,
                invocation_id=invocation_id,
                cwd=workspace,
                run_mode=mode,
                prompt=prompt,
            ),
            timeout=args.turn_timeout,
        )
        events, lead_in_turns = (
            _advance_pipeline(
                agui_url,
                initial=initial,
                thread_id=thread_id,
                invocation_id=invocation_id,
                cwd=workspace,
                timeout=args.turn_timeout,
                progress=progress,
            )
            if mode == "pipeline"
            else (initial, 0)
        )
        interrupts = _interrupts(events)
        if len(interrupts) != 1:
            raise AssertionError("expected exactly one resource selector interrupt")
        interrupt = _selector_interrupt(interrupts)
        if interrupt is None:
            raise AssertionError("the real LLM did not call the cloud resource selector")
        checks[stage] = True
        stage = "AGUI selector contract"
        metadata = interrupt["metadata"]
        selector = metadata.get("selector")
        if not isinstance(selector, dict) or selector.get("id") != SELECTOR_ID:
            raise AssertionError("the selector contract is not vpc.vpc")
        if not isinstance(interrupt.get("responseSchema"), dict):
            raise AssertionError("AG-UI did not expose a response schema")
        selector_task_id, selector_context_id = _session_coordinates(events)
        if (
            metadata.get("requestTaskId") != selector_task_id
            or metadata.get("contextId") != selector_context_id
            or not metadata.get("toolUseId")
        ):
            raise AssertionError("AG-UI selector coordinates differ from its A2A task")
        checks[stage] = True
        stage = "AGUI live VPC query"
        value, label, candidate_count, _values = asyncio.run(_query_real_vpc(metadata))
        checks[stage] = True
        if action == "selected":
            status, answer = "resolved", {"value": value, "label": label}
        elif action == "direct-input":
            status, answer = "resolved", {"freeText": value}
        else:
            status, answer = "cancelled", {"optionsEmpty": False}
        stage = "AGUI same task resumed"
        resumed = _agui_request(
            agui_url,
            _run_payload(
                thread_id=thread_id,
                invocation_id=invocation_id,
                cwd=workspace,
                run_mode=mode,
                resume=[{"interruptId": interrupt["id"], "status": status, "payload": answer}],
            ),
            timeout=args.turn_timeout,
        )
        if _session_coordinates(resumed) != (selector_task_id, selector_context_id):
            raise AssertionError("resource selection resumed a different A2A task or context")
        checks[stage] = True
        stage = "AGUI tool completed"
        if not any(
            event.get("type") == "TOOL_CALL_RESULT" and event.get("toolCallId") == metadata["toolUseId"]
            for event in resumed
        ):
            raise AssertionError("AG-UI did not finish the resource selector tool span")
        checks[stage] = True
        stage = "AGUI continuation observed"
        if not any(event.get("type") in {"TEXT_MESSAGE_CONTENT", "STEP_FINISHED", "RUN_FINISHED"} for event in resumed):
            raise AssertionError("conversation or pipeline did not continue after selection")
        checks[stage] = True
        result = {
            "passed": True,
            "scenario": args.scenario,
            "checks": checks,
            "selectorId": SELECTOR_ID,
            "candidateCount": candidate_count,
            "leadInTurns": lead_in_turns,
            "interruptIssued": True,
            "toolCompleted": True,
            "continuationObserved": True,
            "resumeOutcome": resumed[-1].get("outcome", {}).get("type"),
            "usedRealLlm": True,
            "usedRealCloudQuery": True,
            "usedAguiHttpSse": True,
            "aguiStreamTrace": _agui_stream_trace(resumed),
            **progress,
        }
        (run_dir / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    except Exception as exc:
        checks[stage] = False
        failure_diagnostics = (
            asyncio.run(_failed_resume_diagnostics(a2a_url, selector_task_id, run_dir))
            if selector_task_id and _failure_reason(exc) == 'agui_run_error:a2a_execution_failed' else {}
        )
        (run_dir / "summary.json").write_text(
            json.dumps({"passed": False, "scenario": args.scenario, "checks": checks,
                        "error_type": type(exc).__name__, "agui_failure_reason": _failure_reason(exc),
                        "aguiStreamTrace": getattr(exc, "agui_stream_trace", []),
                        **failure_diagnostics,
                        **progress}, indent=2),
            encoding="utf-8",
        )
        raise
    finally:
        if agui is not None and agui.poll() is None:
            agui.terminate()
            try:
                agui.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agui.kill()
                agui.wait(timeout=10)
        agui_stdout.close()
        agui_stderr.close()
        a2a.terminate()


def main() -> None:
    args = _args()
    result = _run(args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
