#!/usr/bin/env python3
"""Check image-analysis intent clarification over real LLM + local A2A HTTP/SSE.

Run through run_e2e.py for LLM-only isolated credentials. Each branch starts a
fresh context; no cloud credentials, resource writes or candidate selection.
--image permits private replay of an original image without committing it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    stream_message,
    wait_for_server,
)
from scripts.ci.a2a_context_evidence import finish_permissions  # noqa: E402
from scripts.ci.run_coupon_budget_a2a import tool_inputs  # noqa: E402
from scripts.ci.run_knowledge_wiki_a2a import configure_permissions  # noqa: E402
from scripts.ci.run_plan_cost_refresh_a2a import envelopes  # noqa: E402
from scripts.ci.run_promotional_offer_followup_a2a import (  # noqa: E402
    pipeline_facts,
    visible_pipeline_text,
)
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402

AMBIGUOUS = "请分析上传的图片内容。"
DEPLOY = "请按图生成阿里云杭州的部署方案供我选择，按图中参考月价粗估；不生成模板、不部署资源。"
CONSULT = "仅分析这张图的配置和成本，不需要生成部署方案，也不部署资源。"
BRANCHES = ("clarify-deploy", "clarify-consult", "explicit-deploy", "explicit-consult")
DEPLOY_WORDS = re.compile(r"部署|生成.{0,6}方案|deploy", re.I)
CONSULT_WORDS = re.compile(r"分析|咨询|解读|了解|analysis|consult", re.I)
DESTINATION = "https://ros.console.aliyun.com/"
STEP1 = "solution_planning_and_selection"


def fixture_image(path: Path) -> Path:
    """Deterministic, non-private image containing facts, without an intent hint."""
    image = Image.new("RGB", (780, 300), "white")
    draw = ImageDraw.Draw(image)
    lines = (
        "Cloud resource configuration / reference monthly cost (CNY)",
        "VPC x1; VSwitch x2; Security Group x1",
        "ECS x2: 4 vCPU, 8 GiB RAM, 40 GiB system + 100 GiB data",
        "ALB x1; RDS 2 vCPU / 4 GiB HA / 100 GiB ESSD",
        "Redis 1 GiB; OSS; CDN; NAT Gateway; EIP; SSL; DNS",
        "Reference costs: ECS 600; ALB 150; RDS 300; Redis 80",
        "OSS/CDN/NAT/EIP: charged separately by usage",
    )
    for index, line in enumerate(lines):
        draw.text((20, 20 + index * 36), line, fill="black", font_size=18)
    image.save(path)
    return path


def pending_question(events: list[dict]) -> dict:
    for event in reversed(events):
        data = event.get("data", {})
        if event.get("eventType") == "input_required" and data.get("kind") == "ask_user_question":
            return data
    return {}


def intent_question(question: dict) -> bool:
    # A permission prompt, region question, or generic "continue?" is insufficient.
    if question.get("kind") != "ask_user_question":
        return False
    text = str(question.get("question", "")) + json.dumps(question.get("options", []), ensure_ascii=False)
    return bool(DEPLOY_WORDS.search(text) and CONSULT_WORDS.search(text))


def question_answer(question: dict, *, deploy: bool) -> str:
    # Select actual option IDs; do not assume positions or manufacture answers.
    for option in question.get("options", []):
        label = str(option.get("label", ""))
        generates_plan = any(
            not re.search(r"(?:不|无需|不用|不要|暂不)\s*$", label[: match.start()])
            for match in DEPLOY_WORDS.finditer(label)
        )
        is_consult = bool(CONSULT_WORDS.search(label)) and not generates_plan
        if (deploy and generates_plan) or (not deploy and is_consult):
            return str(option.get("id") or label)
    if question.get("allowFreeText"):
        return DEPLOY if deploy else CONSULT
    raise ValueError("No matching intent option or free-text input")


def candidates_ready(conclusion: dict) -> bool:
    return conclusion.get("status") == "awaiting_selection" and bool(conclusion.get("candidates"))


def turn_checks(events: list[dict], *, state: str, handoff: bool, branch: str, first: bool) -> dict[str, bool]:
    conclusions = [
        event.get("data", {}).get("normalizedConclusion") or event.get("data", {}).get("conclusion") or {}
        for event in events
        if event.get("eventType") == "step_completed"
    ]
    rejected = any(value.get("status") == "rejected" for value in conclusions)
    advanced = any(
        event.get("eventType") == "step_started" and event.get("step", {}).get("id") != STEP1 for event in events
    )
    checks = {
        "task paused or completed": state in {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED"},
        "did not enter implementation": not advanced,
    }
    if first and branch.startswith("clarify-"):
        question = pending_question(events)
        before_question = []
        for event in events:
            if event.get("eventType") == "input_required" and event.get("data", {}).get("kind") == "ask_user_question":
                break
            if event.get("eventType") == "text_delta":
                before_question.append(str(event.get("data", {}).get("text", "")))
        checks.update(
            {
                "analyzed resource content before asking": bool(
                    re.search(r"ECS|云服务器", "".join(before_question), re.I)
                ),
                "asked whether to analyze or generate deployment plans": intent_question(question),
                "did not reject before clarification": not rejected and not handoff,
            }
        )
    elif "consult" in branch:
        checks["explicit consultation hands off"] = rejected and handoff
    else:
        checks["deployment stays in Step 1"] = not rejected and not handoff
        if branch == "explicit-deploy":
            checks["no redundant deployment-intent question"] = not intent_question(pending_question(events))
    return checks


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config.resolve().is_relative_to(args.run_dir.resolve()) or (config / ".cloud-credentials.yml").exists():
        raise ValueError("Use run_e2e.py with isolated LLM-only configuration")
    configure_permissions(config)
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    image_path = args.image or fixture_image(args.run_dir / "resource-list.png")
    image_part = {
        "filename": image_path.name,
        "mediaType": "image/png",
        "bytes": base64.b64encode(image_path.read_bytes()).decode("ascii"),
    }
    port = _free_port("127.0.0.1")
    server_url = f"http://127.0.0.1:{port}"
    server_config = _write_server_config(args.run_dir, host="127.0.0.1", port=port, auto_approve_permissions=False)
    observe = ObserveCapture(args.run_dir / "telemetry").start()
    env = {
        **os.environ,
        **observe.env,
        "PYTHONPATH": str(REPO / "src"),
        "IAC_CODE_MODE": "pipeline",
        "IAC_CODE_PIPELINE_NAME": "selling_solution_first",
        "IAC_CODE_ROS_AGENT_AWARENESS": "true",
        "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1",
        "IAC_CODE_PROVIDER": args.provider,
        "IAC_CODE_MODEL": args.model,
        "IAC_CODE_CONFIG_BACKUP_DIR": str(args.run_dir / "backup"),
    }
    server = ManagedServer(
        python_cmd=[sys.executable],
        config_path=server_config,
        process_cwd=str(REPO),
        allowed_cwd=str(workspace),
        env=env,
        log_prefix=args.run_dir / "server",
    )
    checks: dict[str, bool] = {}
    try:
        server.start()
        wait_for_server(server_url, timeout=60, owned_server=server)
        for branch in args.branches:
            context_id = task_id = ""
            prompt = AMBIGUOUS if branch.startswith("clarify-") else DEPLOY if branch.endswith("deploy") else CONSULT
            for turn in range(4):
                name = f"{branch}-{turn + 1}"
                response = stream_message(
                    server_url=server_url,
                    cwd=str(workspace),
                    prompt=prompt,
                    context_id=context_id,
                    task_id=task_id,
                    images=[image_part] if turn == 0 else None,
                    name=name,
                    run_dir=args.run_dir,
                    timeout=args.timeout,
                    redaction_env=env,
                )
                response = finish_permissions(
                    response,
                    name=name,
                    run_dir=args.run_dir,
                    config=config,
                    server_url=server_url,
                    cwd=str(workspace),
                    timeout=args.timeout,
                    env=env,
                )
                context_id, task_id = response.context_id, response.task_id
                path = args.run_dir / f"{name}.events.jsonl"
                events = envelopes(path)
                current = turn_checks(
                    events,
                    state=response.last_status_state,
                    handoff=response.normal_handoff_ready,
                    branch=branch,
                    first=turn == 0,
                )
                current["production HTTP/SSE"] = response.response_content_type == "text/event-stream"
                conclusion, _ = pipeline_facts(path)
                text = visible_pipeline_text(path)
                if "consult" in branch and (turn > 0 or branch.startswith("explicit-")):
                    current["visible assistant link"] = DESTINATION in text
                checks.update({f"{name}: {key}": value for key, value in current.items()})
                # Preserve original answers separately from public boolean diagnostics.
                (args.run_dir / f"{name}.answer.md").write_text(text, encoding="utf-8")
                if not all(current.values()):
                    break
                if turn == 0 and branch.startswith("clarify-"):
                    prompt = question_answer(pending_question(events), deploy=branch.endswith("deploy"))
                    continue
                if "consult" in branch:
                    break
                if candidates_ready(conclusion):
                    checks[f"{branch}: candidates await user selection"] = True
                    break
                # Answer only real clarification, never select a candidate or confirm deployment.
                question = pending_question(events)
                if not question or not question.get("allowFreeText"):
                    break
                prompt = DEPLOY
            if branch.endswith("deploy"):
                checks.setdefault(f"{branch}: candidates await user selection", False)
    finally:
        server.terminate()
        records = observe.stop()
        checks["server stopped"] = server.process is not None and server.process.poll() is not None
        for filename in (".credentials.yml", "settings.yml"):
            (config / filename).unlink(missing_ok=True)
    # With no cloud credentials, cloud tools must not have been executed at all.
    checks["no cloud operations"] = not any(
        call["name"] == "aliyun_api" or str(call["name"]).startswith("ros_") for call in tool_inputs(config)
    )
    models = sorted(
        {
            str(record.get("attributes", {}).get("gen_ai.request.model"))
            for record in records
            if record.get("attributes", {}).get("gen_ai.request.model")
        }
    )
    checks["only requested model ran"] = models == [args.model]
    return {
        "scenario": "resource-plan-intent-a2a",
        "model": args.model,
        "observed_models": models,
        "status": "passed" if all(checks.values()) else "failed",
        "cleanup_status": "not-needed",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--allow-real-llm", action="store_true")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--branches", nargs="+", choices=BRANCHES, default=BRANCHES)
    args = parser.parse_args()
    if not args.allow_real_llm:
        parser.error("--allow-real-llm is required")
    args.run_dir = args.run_dir.resolve()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = run(args)
    except Exception as exc:
        stack = "".join(traceback.format_tb(exc.__traceback__))
        (args.run_dir / "failure-stack.txt").write_text(stack, encoding="utf-8")
        summary = {
            "scenario": "resource-plan-intent-a2a",
            "model": args.model,
            "status": "failed",
            "cleanup_status": "not-needed",
            "error_type": type(exc).__name__,
        }
    (args.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
