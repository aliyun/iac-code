"""HTTP/SSE test driving and provider-request evidence; no application overrides."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.a2a.e2e.common import stream_message
from scripts.a2a.e2e.permission_wait.run_permission_wait_restart import (
    _permission_response_query,
    _unique_permissions,
)
from scripts.ci.run_coupon_budget_a2a import tool_inputs


def may_read_generated_result(permission: dict, calls: list[dict], run_dir: Path) -> bool:
    if permission.get("isReadOnly") is not True or permission.get("toolName") not in {"grep", "read_file"}:
        return False
    call = next((c for c in calls if c["id"] == permission.get("toolUseId")), None)
    if call is None:
        return False
    path = Path(str(call["input"].get("path", ""))).resolve()
    return path.is_relative_to(run_dir.resolve()) and "tool-results" in path.parts and path.is_file()


def finish_permissions(response, *, name, run_dir, config, server_url, cwd, timeout, env, allow_ecs_dry_run=False):
    """Approve owned result files and, when explicitly enabled, ECS DryRun checkpoints."""
    combined = run_dir / f"{name}.events.jsonl"
    latest = combined
    decisions = []
    for number in range(8):
        if response.last_status_state != "TASK_STATE_INPUT_REQUIRED":
            break
        raw = [json.loads(line) for line in latest.read_text(encoding="utf-8").splitlines() if line.strip()]
        pending = _unique_permissions(raw)
        if not pending:
            break
        permission = pending[-1]
        calls = tool_inputs(config)
        dry_run = False
        if allow_ecs_dry_run:
            from scripts.ci.ecs_dryrun_guard import explicit_ecs_dry_run

            dry_run = permission.get("toolName") == "aliyun_api" and any(
                call["id"] == permission.get("toolUseId") and explicit_ecs_dry_run(call) for call in calls
            )
        decision = "allow_once" if may_read_generated_result(permission, calls, run_dir) or dry_run else "deny"
        decisions.append({"tool": permission.get("toolName"), "decision": decision})
        resume_name = f"{name}-permission-{number + 1}"
        response = stream_message(
            server_url=server_url,
            cwd=cwd,
            prompt=_permission_response_query(permission, decision),
            context_id=response.context_id,
            task_id=response.task_id,
            name=resume_name,
            run_dir=run_dir,
            timeout=timeout,
            redaction_env=env,
        )
        latest = run_dir / f"{resume_name}.events.jsonl"
        with combined.open("a", encoding="utf-8") as destination:
            destination.write(latest.read_text(encoding="utf-8"))
    (run_dir / f"{name}.permissions.json").write_text(json.dumps(decisions, indent=2), encoding="utf-8")
    if response.last_status_state == "TASK_STATE_INPUT_REQUIRED":
        raw = [json.loads(line) for line in latest.read_text(encoding="utf-8").splitlines() if line.strip()]
        if _unique_permissions(raw):
            raise RuntimeError("Permission requests remain unresolved after {} responses".format(len(decisions)))
    return response


def main_requests(records: list[dict]) -> list[dict[str, Any]]:
    """Use provider chat spans, excluding keyword side calls and agent wrapper spans."""
    requests = []
    for record in records:
        attrs = record.get("attributes", {})
        if record.get("kind") != "span" or not str(record.get("name", "")).startswith("chat "):
            continue
        if "Wiki keyword extraction" in str(attrs.get("gen_ai.system_instructions", "")):
            continue
        try:
            messages = json.loads(attrs.get("gen_ai.input.messages", "[]"))
        except (TypeError, ValueError):
            continue
        parts = [part for message in messages for part in message.get("parts", [])]
        texts = [
            part.get("content", "")
            for message in messages
            if message.get("role") == "user"
            for part in message.get("parts", [])
            if part.get("type") == "text"
        ]
        requests.append(
            {
                "start_ns": int(record.get("timestamp_unix_nano", 0)),
                "texts": texts,
                "message_count": len(messages),
                "response_ids": {part.get("id") for part in parts if part.get("type") == "tool_call_response"},
                "call_ids": {part.get("id") for part in parts if part.get("type") == "tool_call"},
            }
        )
    return sorted(requests, key=lambda request: request["start_ns"])


def request_window(requests: list[dict], turn: dict) -> list[dict]:
    return [request for request in requests if turn["start_ns"] <= request["start_ns"] <= turn["end_ns"]]


def rolled_back_to(events: list[dict], target: str) -> bool:
    return any(event.get("eventType") == "rollback_completed" and target in {
        event.get("data", {}).get("toStep"), event.get("data", {}).get("toStepId"),
    } for event in events)


def retained_history(request: dict, *, prompts: list[str], response_ids: set[str]) -> dict[str, bool]:
    return {
        "prior and current user prompts occur once": all(
            sum(text.count(prompt) for text in request["texts"]) == 1 for prompt in prompts
        ),
        "prior successful tool results retained": bool(response_ids) and response_ids <= request["response_ids"],
    }


def fresh_history(request: dict, *, prompts: list[str], old_ids: set[str]) -> bool:
    return not old_ids.intersection(request["response_ids"] | request["call_ids"]) and not any(
        prompt in text for prompt in prompts for text in request["texts"]
    )
