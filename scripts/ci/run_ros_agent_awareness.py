#!/usr/bin/env python3
"""Exercise Step 1 routing with a real LLM, production tools and session storage.

This stops at handoff or the first clarification boundary. It does not replace
model responses or call cloud APIs; transport/UI behavior is outside its scope.
Invoke through run_e2e.py to receive isolated LLM-only configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scenario:
    prompt: str
    region: str | None = "cn-beijing"
    enabled: bool = True
    handoff: bool = True


SCENARIOS = {
    "promotions": Scenario("阿里云现在有什么优惠活动和优惠券？我只想咨询，暂时不部署资源。"),
    "renewal": Scenario("已有的 ECS 快到期了，续费有折扣吗？"),
    "billing": Scenario("我只咨询已有 ECS 的按量账单，不打算部署或创建资源。按量付费如何计费，欠费后怎么办？"),
    "gpu-availability": Scenario("北京地域的 GPU 实例现在有哪些库存和单价？暂时不部署。", region="cn-shanghai"),
    "traffic-offset": Scenario("有没有能抵扣已有 ECS 公网流量费的流量包？"),
    "default-region": Scenario("我只想咨询阿里云的续费优惠，暂时不创建资源。", region=None),
    "disabled": Scenario("我只想咨询已有 ECS 的续费折扣。", enabled=False, handoff=False),
    "deployment-pricing": Scenario(
        "我想在阿里云部署一个网站，请先问清网站类型和访问规模再推荐方案，选型时也帮我比较按量与包月成本。",
        handoff=False,
    ),
}


async def run(args: argparse.Namespace) -> dict:
    # Prefer this checkout even if the interpreter has another editable install.
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "src"))
    from iac_code.config import get_active_provider_key, get_provider_config, load_credentials
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
    from iac_code.providers.manager import ProviderManager
    from iac_code.services.providers.aliyun import DEFAULT_REGION, AliyunCredential, use_aliyun_credential
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import ToolRegistry
    from iac_code.types.stream_events import (
        AskUserQuestionEvent,
        ErrorEvent,
        MessageEndEvent,
        TextDeltaEvent,
        ToolResultEvent,
        ToolUseStartEvent,
    )

    scenario = SCENARIOS[args.scenario]
    os.environ["IAC_CODE_ROS_AGENT_AWARENESS"] = "true" if scenario.enabled else "false"
    os.environ["IAC_CODE_CONFIG_BACKUP_DIR"] = str(args.run_dir / "backup")
    provider = args.provider or get_active_provider_key()
    if not provider:
        raise ValueError("No configured LLM provider")
    config = get_provider_config(provider)
    model = args.model or config.get("model")
    if not model:
        raise ValueError("No configured LLM model")
    manager = ProviderManager(
        model=model,
        credentials=load_credentials(model),
        provider_key_override=provider,
        provider_config_override={**config, "modelFallbackEnabled": False},
        ignore_llm_source=True,
    )
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    flow = PipelineRunner(
        pipeline_dir=repo / "src/iac_code/pipeline/selling_solution_first",
        provider_manager=manager,
        # Step-local tools are registered by the production engine. Deliberately
        # omit cloud tools: routing needs no cloud operation or cloud credential.
        base_tool_registry=ToolRegistry(),
        session_storage=SessionStorage(args.run_dir / "projects"),
        session_id="ros-awareness-e2e-" + uuid.uuid4().hex,
        cwd=str(workspace),
    )
    text: list[str] = []
    tools: list[str] = []
    input_tokens = output_tokens = error_count = 0
    asked_question = False
    credential = AliyunCredential(region_id=scenario.region) if scenario.region is not None else None
    with use_aliyun_credential(credential):
        async with aclosing(flow.run(scenario.prompt)) as events:
            async for event in events:
                if isinstance(event, TextDeltaEvent):
                    text.append(event.text)
                elif isinstance(event, ToolUseStartEvent):
                    tools.append(event.name)
                elif isinstance(event, MessageEndEvent):
                    input_tokens += event.usage.total_input_tokens
                    output_tokens += event.usage.output_tokens
                elif isinstance(event, ErrorEvent) or isinstance(event, ToolResultEvent) and event.is_error:
                    error_count += 1
                elif isinstance(event, AskUserQuestionEvent):
                    asked_question = True
                    await flow.persist_pending_ask_user_question(event)
                    break

    visible = "".join(text)
    conclusion = flow.context.get_conclusion("solution_selection") or {}
    url = f"https://ros.console.aliyun.com/{scenario.region or DEFAULT_REGION}/chat"
    checks = {
        "real model returned token usage": input_tokens > 0 and output_tokens > 0,
        "no model or tool errors": error_count == 0,
        "no deployment selected": flow.context.get_conclusion("selected_plan") is None,
    }
    if scenario.handoff:
        checks.update(
            {
                "same-region assistant link is visible": url in visible,
                "only the expected assistant destination is used": visible.count("https://ros.console.aliyun.com/")
                == visible.count(url)
                > 0,
                "guidance is saved in the early-exit conclusion": conclusion.get("status") == "rejected"
                and conclusion.get("continue_pipeline") is False
                and url in conclusion.get("rejection_reason", ""),
                "handoff skips deployment questions and candidates": tools == ["complete_step"] and not asked_question,
            }
        )
    elif scenario.enabled:
        checks.update(
            {
                "normal clarification remains available": asked_question and "ask_user_question" in tools,
                "no assistant handoff": "https://ros.console.aliyun.com/" not in visible
                and conclusion.get("status") != "rejected",
            }
        )
    else:
        # The unchanged base flow permits clarification or a normal rejection
        # when the user has already confirmed that no deployment is requested.
        checks.update(
            {
                "disabled flow preserves clarification or normal early exit": asked_question
                and "ask_user_question" in tools
                or conclusion.get("status") == "rejected"
                and bool(conclusion.get("rejection_reason")),
                "disabled flow does not add an assistant destination": "https://ros.console.aliyun.com/" not in visible
                and "https://ros.console.aliyun.com/" not in conclusion.get("rejection_reason", ""),
            }
        )
    return {
        "scenario": args.scenario,
        "status": "passed" if all(checks.values()) else "failed",
        "cleanup_status": "not-needed",
        "checks": checks,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--allow-real-llm", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if not args.allow_real_llm:
        parser.error("This calls a real LLM; pass --allow-real-llm")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        summary = asyncio.run(asyncio.wait_for(run(args), timeout=args.timeout))
    except Exception as exc:
        # Do not copy provider messages/HTTP payloads into CI reports.
        summary = {
            "scenario": args.scenario,
            "status": "failed",
            "cleanup_status": "not-needed",
            "checks": {"scenario completed within budget": False},
            "error_type": type(exc).__name__,
        }
    summary["duration_seconds"] = round(time.monotonic() - started, 2)
    (args.run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
