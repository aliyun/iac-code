#!/usr/bin/env python3
"""Real-provider A2A server backed by the small live selector test pipelines."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def _followup_request_facts(payload: Any) -> dict[str, bool] | None:
    """Observe fixed facts from the actual wire payload, never its contents."""
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return None
    messages = payload["messages"]

    def text(message):
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        return (
            "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if isinstance(content, list)
            else ""
        )

    def has_marker(message):
        return "RESTART_NEXT_TURN_OK_" in text(message)

    if not any(has_marker(message) for message in messages):
        return None
    users = [message for message in messages if message.get("role") == "user"]
    system = "\n".join(text(message) for message in messages if message.get("role") in {"system", "developer"})
    choice = payload.get("tool_choice")
    original = ""
    for message in users:
        content = text(message)
        if not content.startswith("[Pipeline Handoff Context]\n") or "Included context:\n" not in content:
            continue
        try:
            context, _ = json.JSONDecoder().raw_decode(content.split("Included context:\n", 1)[1])
        except ValueError:
            continue
        if isinstance(context, dict) and isinstance(context.get("original_user_request"), str):
            original = context["original_user_request"]
    return {
        "selector_next_turn_wire_request_observed": True,
        "selector_next_turn_wire_last_user_has_marker": bool(users and has_marker(users[-1])),
        "selector_next_turn_wire_last_message_has_marker": bool(messages and has_marker(messages[-1])),
        "selector_next_turn_wire_step_prompt_present": "This is a live, read-only A2A resource-selector test step."
        in system,
        "selector_next_turn_wire_forced_tool": choice == "required" or isinstance(choice, dict),
        "selector_next_turn_wire_selector_tools_available": any(
            tool.get("function", {}).get("name") == "select_cloud_resource"
            for tool in payload.get("tools", [])
            if isinstance(tool, dict)
        ),
        "selector_next_turn_wire_adjacent_user_messages": len(messages) >= 2
        and messages[-1].get("role") == messages[-2].get("role") == "user",
        "selector_next_turn_wire_original_request_in_handoff": bool(original),
        "selector_next_turn_wire_original_request_standalone": bool(original)
        and any(text(message) == original for message in users),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--persistence-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--pipeline-root", type=Path, required=True)
    parser.add_argument("--pipeline-name", required=True)
    parser.add_argument("--model", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config_dir = args.config_dir.expanduser().resolve()
    persistence_dir = args.persistence_dir.expanduser().resolve()
    artifact_dir = args.artifact_dir.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve()
    pipeline_root = args.pipeline_root.expanduser().resolve()
    pipeline_dir = pipeline_root / args.pipeline_name
    if not (pipeline_dir / "pipeline.yaml").is_file():
        raise ValueError("live resource-selector pipeline is missing: {}".format(pipeline_dir))
    for path in (config_dir, persistence_dir, artifact_dir, workspace):
        path.mkdir(parents=True, exist_ok=True)

    os.environ["IAC_CODE_CONFIG_DIR"] = str(config_dir)
    os.environ["IAC_CODE_MODE"] = "pipeline"
    os.environ["IAC_CODE_PIPELINE_NAME"] = args.pipeline_name
    os.environ["IACCODE_A2A_ALLOWED_CWDS"] = str(workspace)

    import uvicorn

    from iac_code.a2a import pipeline_executor as pipeline_executor_module
    from iac_code.a2a.app import create_app
    from iac_code.config import DEFAULT_MODEL, load_saved_model
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    def discover_live_pipelines() -> dict[str, Path]:
        return {
            path.name: path for path in pipeline_root.iterdir() if path.is_dir() and (path / "pipeline.yaml").is_file()
        }

    def create_live_pipeline(name: str, *unused_args: Any, **kwargs: Any) -> PipelineRunner:
        del unused_args
        selected = discover_live_pipelines().get(name)
        if selected is None:
            raise ValueError("unknown live resource-selector pipeline: {}".format(name))
        return PipelineRunner(pipeline_dir=selected, **kwargs)

    # Patch only pipeline discovery/creation. Agent runtime, provider, tools,
    # credential resolution, checkpointing and transport remain production code.
    pipeline_executor_module.discover_pipelines = discover_live_pipelines
    pipeline_executor_module.create_pipeline = create_live_pipeline

    import httpx

    original_send = httpx.AsyncClient.send

    async def observe_model_request(client, request, *send_args, **send_kwargs):
        try:
            facts = _followup_request_facts(json.loads(request.content))
        except (ValueError, RuntimeError, TypeError):
            facts = None
        if facts is not None and facts["selector_next_turn_wire_selector_tools_available"]:
            with (persistence_dir.parent / "model-request-facts.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(facts) + "\n")
        return await original_send(client, request, *send_args, **send_kwargs)

    # Read-only observation; the production request and response are unchanged.
    httpx.AsyncClient.send = observe_model_request

    app = create_app(
        host=args.host,
        port=args.port,
        token=None,
        model=args.model or load_saved_model() or DEFAULT_MODEL,
        persistence_dir=persistence_dir,
        artifact_dir=artifact_dir,
        auto_approve_permissions=False,
    )
    # ManagedServer verifies the socket-bind receipt from this child's pipe
    # before accepting its agent card. Warning hides Uvicorn's bind receipt.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
