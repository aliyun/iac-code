#!/usr/bin/env python3
"""Verify minimum specs and promotional offer followups over real HTTP/SSE A2A.

Run via run_e2e.py for private LLM-only configuration. No scripted model or
tool results are injected. Cloud credentials are not supplied to this case.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.a2a.debugger import (  # noqa: E402
    A2A_VERSION_HEADERS,
    _extract_pipeline_envelopes,
    build_task_cancel_payload,
)
from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    fetch_pipeline_state,
    stream_message,
    wait_for_server,
)
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402

PROMPTS = (
    "我自己在做一个 agent 小项目，是旅游规划。建议腾讯云或者阿里云，"
    "这次用阿里云杭州，一台服务器，给我推荐一个方案。"
    "算力要求：2核4G（最低2核2G，推荐4G内存以避免运行时内存不足）。"
    "系统镜像要选 CentOS，防火墙开放端口：10010，9090，3000，59000，54328，需要公网访问。",
    "为什么不能选那个99元的？ecs.e-c1m1.large 热门推荐，经济型 e，2 vCPU，2 GiB，"
    "76个可用区，X86计算-共享型，￥45.14/月，惠￥19.86/月。",
    "我现在只想确认这个99元活动我的账号能不能买、优惠包含什么、从哪里买，暂时不继续部署。",
)
DESTINATION = "https://ros.console.aliyun.com/cn-hangzhou/chat"


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def completion_inputs(config_dir: Path) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    paths = (config_dir / "projects").rglob("session.jsonl")
    for path in sorted(paths, key=lambda item: item.stat().st_mtime_ns):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            for node in walk(json.loads(line)):
                if node.get("type") == "tool_use" and node.get("name") == "complete_step":
                    value = node.get("input", {}).get("conclusion")
                    if isinstance(value, dict):
                        found[str(node.get("id", len(found)))] = value
    return list(found.values())


def memory_constraints(value: Any) -> list[dict[str, Any]]:
    return [
        item
        for node in walk(value)
        for item in node.get("hard_constraints", [])
        if isinstance(item, dict) and str(item.get("property", "")).casefold() in {"memory", "memory_gib"}
    ]


def visible_pipeline_text(path: Path) -> str:
    """Use only user-visible pipeline deltas, excluding tool arguments and thinking."""
    text = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for envelope in _extract_pipeline_envelopes(json.loads(line)):
            if envelope.get("eventType") == "text_delta":
                value = envelope.get("data", {}).get("text")
                if isinstance(value, str):
                    text.append(value)
    return "".join(text)


def pipeline_facts(path: Path) -> tuple[dict[str, Any], list[str]]:
    conclusion: dict[str, Any] = {}
    steps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for envelope in _extract_pipeline_envelopes(json.loads(line)):
            data = envelope.get("data", {})
            value = data.get("normalizedConclusion") or data.get("conclusion")
            if isinstance(value, dict) and value.get("status"):
                conclusion = value
            step = envelope.get("step", {})
            if envelope.get("eventType") == "step_started" and isinstance(step, dict) and step.get("id"):
                steps.append(step["id"])
    return conclusion, steps


def run(args: argparse.Namespace) -> dict[str, Any]:
    config_dir = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config_dir.resolve().is_relative_to(args.run_dir.resolve()):
        raise ValueError("Run via run_e2e.py with isolated configuration")
    if (config_dir / ".cloud-credentials.yml").exists():
        raise ValueError("This case requires LLM-only configuration")
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    port = _free_port("127.0.0.1")
    config = _write_server_config(args.run_dir, host="127.0.0.1", port=port, auto_approve_permissions=True)
    observe = ObserveCapture(args.run_dir / "telemetry").start()
    env = dict(os.environ)
    env.update(observe.env)
    env.update({
        "PYTHONPATH": str(REPO / "src"),
        "IAC_CODE_MODE": "pipeline",
        "IAC_CODE_PIPELINE_NAME": "selling_solution_first",
        "IAC_CODE_ROS_AGENT_AWARENESS": "true",
        "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1",
        "IAC_CODE_PROVIDER": args.provider or "dashscope",
        "IAC_CODE_MODEL": args.model,
        "IAC_CODE_CONFIG_BACKUP_DIR": str(args.run_dir / "backup"),
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
            canceled_unselected_implementation = False

            def stop_unselected_implementation(event: Any) -> None:
                nonlocal canceled_unselected_implementation
                if index != 1 or canceled_unselected_implementation:
                    return
                for envelope in _extract_pipeline_envelopes(event):
                    if (
                        envelope.get("eventType") == "step_started"
                        and envelope.get("step", {}).get("id") == "materialize_selected_candidate"
                    ):
                        request = Request(
                            f"http://127.0.0.1:{port}/",
                            data=json.dumps(build_task_cancel_payload(task_id=task_id)).encode("utf-8"),
                            headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
                        )
                        with urlopen(request, timeout=30) as response:
                            canceled_unselected_implementation = not bool(json.load(response).get("error"))

            summary = stream_message(
                server_url=f"http://127.0.0.1:{port}", cwd=str(workspace), prompt=prompt,
                context_id=context_id, task_id=task_id, name=f"turn-{index + 1}",
                run_dir=args.run_dir, timeout=args.timeout, redaction_env=env,
                on_event=stop_unselected_implementation,
            )
            context_id, task_id = summary.context_id, summary.task_id
            state = fetch_pipeline_state(
                server_url=f"http://127.0.0.1:{port}", context_id=context_id, task_id=task_id,
                run_dir=args.run_dir, name=f"turn-{index + 1}", redaction_env=env,
            )
            completions = completion_inputs(config_dir)
            latest, steps = pipeline_facts(args.run_dir / f"turn-{index + 1}.events.jsonl")
            latest = latest or (completions[-1] if completions else {})
            constraints = memory_constraints(latest or state)
            evidence = {
                "turn": index + 1, "seconds": round(time.monotonic() - started, 2),
                "http_sse": summary.response_content_type == "text/event-stream",
                "task_state": summary.last_status_state,
                "pipeline_handoff": summary.normal_handoff_ready,
                "step": summary.last_input_required_step_id,
                "assistant_link_visible": DESTINATION in visible_pipeline_text(
                    args.run_dir / f"turn-{index + 1}.events.jsonl"
                ),
                "completion_status": latest.get("status"),
                "memory_constraints": constraints,
                "started_steps": steps,
                "unselected_implementation_canceled": canceled_unselected_implementation,
            }
            turns.append(evidence)
            checks[f"turn {index + 1}: production HTTP/SSE"] = evidence["http_sse"]
            checks[f"turn {index + 1}: no failed task"] = summary.last_status_state not in {
                "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "",
            }
            if index == 0:
                checks["minimum 2 GiB remains the lower bound"] = bool(constraints) and all(
                    item.get("operator") == "gte" and item.get("value") == 2 for item in constraints
                )
                checks["initial deployment remains in Step 1"] = (
                    summary.last_input_required_step_id == "solution_planning_and_selection"
                    and not summary.normal_handoff_ready
                )
            elif index == 1:
                checks["activity followup offers the same-region assistant"] = evidence["assistant_link_visible"]
                checks["activity followup saves the linked handoff"] = (
                    latest.get("status") == "rejected" and DESTINATION in latest.get("rejection_reason", "")
                )
                checks["activity followup does not advance to implementation"] = (
                    "materialize_selected_candidate" not in steps and latest.get("status") != "selected"
                )
            else:
                checks["explicit activity inquiry has a visible assistant link"] = evidence["assistant_link_visible"]
                checks["explicit activity handoff saves the linked reply"] = (
                    latest.get("status") == "rejected" and DESTINATION in latest.get("rejection_reason", "")
                )
            print(json.dumps({"model": args.model, **evidence}, ensure_ascii=False), flush=True)
            if summary.normal_handoff_ready:
                context_id = task_id = ""
    finally:
        server.terminate()
        records = observe.stop()
        checks["server stopped"] = server.process is not None and server.process.poll() is not None
        for name in (".credentials.yml", "settings.yml"):
            (config_dir / name).unlink(missing_ok=True)
        checks["credential copies removed"] = not (config_dir / ".credentials.yml").exists()
    observed = sorted({
        str(record.get("attributes", {}).get("gen_ai.request.model"))
        for record in records
        if record.get("attributes", {}).get("gen_ai.request.model")
    })
    checks["only the requested real model ran"] = observed == [args.model]
    return {
        "scenario": "promotional-offer-followup-a2a", "model": args.model,
        "status": "passed" if all(checks.values()) else "failed",
        "cleanup_status": "not-needed", "checks": checks, "turns": turns,
        "observed_models": observed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--allow-real-llm", action="store_true")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if not args.allow_real_llm:
        parser.error("--allow-real-llm is required")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = run(args)
    except Exception as exc:
        summary = {
            "scenario": "promotional-offer-followup-a2a", "model": args.model, "status": "failed",
            "cleanup_status": "not-needed", "error_type": type(exc).__name__,
        }
    (args.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
