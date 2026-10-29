#!/usr/bin/env python3
"""Read-only, real-model multi-turn Step1/Step2 followup acceptance over HTTP/SSE."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from iac_code.pipeline.engine.ui_contract import encode_deployment_confirmation, encode_selected_candidate  # noqa: E402
from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load  # noqa: E402
from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    fetch_pipeline_state,
    stream_message,
    wait_for_server,
)
from scripts.ci.run_coupon_budget_a2a import tool_inputs  # noqa: E402
from scripts.ci.run_plan_cost_refresh_a2a import envelopes  # noqa: E402
from scripts.ci.run_promotional_offer_followup_a2a import visible_pipeline_text, walk  # noqa: E402
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402

INITIAL = (
    "在阿里云杭州用一台按量付费的 ECS 部署一个简单 nginx 静态网站，系统盘至少40GiB（选择最小适用容量），"
    "公网用按流量计费的EIP。给我一个最小方案，先不要部署，不创建任何云资源。"
)


def results(config: Path) -> dict[str, dict[str, Any]]:
    found = {}
    for path in (config / "projects").rglob("session.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                for node in walk(json.loads(line)):
                    if node.get("type") == "tool_result":
                        found[str(node.get("tool_use_id"))] = node
    return found


def stable(conclusion: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(conclusion)
    value.pop("user_input", None)
    return value


def disk_size(conclusion: dict[str, Any], workspace: Path) -> int:
    template = ros_yaml_load((workspace / conclusion["template_url"]).read_text(encoding="utf-8"))
    parameters = conclusion.get("effective_deployment_parameters", {})
    for resource in template.get("Resources", {}).values():
        if str(resource.get("Type", "")) not in {"ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"}:
            continue
        props = resource.get("Properties", {})
        value = props.get("SystemDisk", {}).get("Size") or props.get("SystemDiskSize")
        if isinstance(value, dict) and "Ref" in value:
            name = value["Ref"]
            value = parameters.get(name, template.get("Parameters", {}).get(name, {}).get("Default"))
        if value is not None:
            return int(value)
    raise ValueError("Cannot resolve the actual baseline ECS system disk size")


def run(args: argparse.Namespace) -> dict[str, Any]:
    config_dir = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config_dir.resolve().is_relative_to(args.run_dir.resolve()):
        raise ValueError("Use run_e2e.py with isolated test credentials")
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    port = _free_port("127.0.0.1")
    config = _write_server_config(args.run_dir, host="127.0.0.1", port=port, auto_approve_permissions=True)
    capture = ObserveCapture(args.run_dir / "telemetry").start()
    env = {
        **os.environ,
        **capture.env,
        "PYTHONPATH": str(REPO / "src"),
        "IAC_CODE_MODE": "pipeline",
        "IAC_CODE_PIPELINE_NAME": "selling_solution_first",
        "IAC_CODE_PROVIDER": args.provider,
        "IAC_CODE_MODEL": args.model,
        "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1",
        "IAC_CODE_CONFIG_BACKUP_DIR": str(args.run_dir / "backup"),
    }
    server = ManagedServer(
        python_cmd=[args.python],
        config_path=config,
        process_cwd=str(REPO),
        allowed_cwd=str(workspace),
        env=env,
        log_prefix=args.run_dir / "server",
    )
    url = f"http://127.0.0.1:{port}"
    checks: dict[str, bool] = {}
    turns = []
    context_id = task_id = ""
    snapshots = []
    template_hash = None

    def turn(prompt: str, *, unchanged=False, terminal=False) -> dict[str, Any]:
        nonlocal context_id, task_id, template_hash
        index = len(turns) + 1
        before = {item["id"] for item in tool_inputs(config_dir)}
        prior = snapshots[-1] if snapshots else None
        started = time.monotonic()
        response = stream_message(
            server_url=url,
            cwd=str(workspace),
            prompt=prompt,
            context_id=context_id,
            task_id=task_id,
            name=f"turn-{index}",
            run_dir=args.run_dir,
            timeout=args.timeout,
            redaction_env=env,
        )
        context_id, task_id = response.context_id, response.task_id
        events = envelopes(args.run_dir / f"turn-{index}.events.jsonl")
        completed = [e for e in events if e.get("eventType") == "step_completed"]
        conclusion = next(
            (
                e.get("data", {}).get("conclusion")
                for e in reversed(completed)
                if isinstance(e.get("data", {}).get("conclusion"), dict)
            ),
            {},
        )
        calls = [item for item in tool_inputs(config_dir) if item["id"] not in before]
        outcomes = results(config_dir)
        success = [item for item in calls if outcomes.get(item["id"], {}).get("is_error") is False]
        label = f"turn {index}"
        checks[label + ": HTTP/SSE"] = response.response_content_type == "text/event-stream"
        checks[label + ": no deployment"] = not any(
            e.get("eventType") == "step_started" and e.get("step", {}).get("id") == "deploying" for e in events
        ) and not any(item["name"] in {"ros_deploy", "ros_stack", "ros_stack_instances"} for item in calls)
        if not terminal:
            checks[label + ": valid waiting result"] = (
                response.last_status_state == "TASK_STATE_INPUT_REQUIRED"
                and bool(conclusion)
                and conclusion.get("status") in {"awaiting_selection", "awaiting_confirmation"}
                and not response.normal_handoff_ready
            )
        if unchanged:
            checks[label + ": visible answer"] = bool(
                visible_pipeline_text(args.run_dir / f"turn-{index}.events.jsonl").strip()
            )
            checks[label + ": current successful empty completion"] = any(
                item["name"] == "complete_step"
                and item["input"] in ({"conclusion": {}}, {"conclusion": {"conclusion": {}}})
                for item in success
            )
            checks[label + ": full unchanged conclusion"] = bool(prior) and stable(conclusion) == stable(prior)
            checks[label + ": unchanged event"] = (
                bool(completed) and completed[-1].get("data", {}).get("conclusion_unchanged") is True
            )
        path = conclusion.get("template_url")
        current_hash = hashlib.sha256((workspace / path).read_bytes()).hexdigest() if path else None
        if unchanged and path:
            checks[label + ": template preserved"] = template_hash == current_hash
        if path:
            template_hash = current_hash
        fetch_pipeline_state(
            server_url=url,
            context_id=context_id,
            task_id=task_id,
            run_dir=args.run_dir,
            name=f"turn-{index}",
            redaction_env=env,
        )
        snapshots.append(conclusion)
        turns.append(
            {
                "turn": index,
                "seconds": round(time.monotonic() - started, 2),
                "status": conclusion.get("status"),
                "successful_tool_names": [i["name"] for i in success],
            }
        )
        print(json.dumps({"model": args.model, **turns[-1]}, ensure_ascii=False), flush=True)
        if not terminal and not checks[label + ": valid waiting result"]:
            raise RuntimeError("Precondition did not reach the required waiting state")
        return conclusion

    try:
        server.start()
        wait_for_server(url, timeout=60, owned_server=server)
        first = turn(INITIAL)
        if args.scenario == "step1":
            turn("这个方案里，系统盘和计算费分别包含什么？先解释，不改方案。", unchanged=True)
            turn("按量付费停机后，还有哪些费用可能继续产生？先解释，不改方案。", unchanged=True)
            updated = turn("给刚才的方案增加一个 RDS MySQL 数据库，其他目标不变，更新方案让我选择。")
            checks["changed candidates use new full batch"] = bool(updated.get("candidate_set_id")) and updated.get(
                "candidate_set_id"
            ) != first.get("candidate_set_id")
            checks["RDS actually added"] = any(
                "rds" in json.dumps(c.get("resource_inventory", [])).lower() for c in updated.get("candidates", [])
            )
            turn("为什么加数据库后费用会上升？先解释，不改方案。", unchanged=True)
            final = turn("取消这次购买，不需要继续了。", terminal=True)
            checks["cancel ends step1"] = final.get("status") == "rejected"
        else:
            candidates = first.get("candidates") or []
            if not candidates:
                raise RuntimeError("No valid initial candidate")
            baseline = turn(encode_selected_candidate(candidates[0]["name"], 0))
            checks["step2 real template, preview and quote"] = (
                baseline.get("status") == "awaiting_confirmation"
                and baseline.get("preview_ready_for_create") is True
                and bool(baseline.get("selected_candidate_result", {}).get("cost"))
            )
            turn("解释一下当前方案的费用构成，先不要调整。", unchanged=True)
            turn(
                "只做比较：系统盘换成80GiB大概贵多少？请查询最新云端价格作为依据，不只按已有金额推算，暂时不要改当前方案。",
                unchanged=True,
            )
            comparison_calls = turns[-1]["successful_tool_names"]
            checks["comparison called a real pricing tool"] = any(
                name in comparison_calls for name in ("aliyun_api", "ros_estimate_template_cost")
            )
            before_hash = template_hash
            target_disk_size = disk_size(baseline, workspace) + 40
            updated = turn(f"现在明确调整当前方案：系统盘改为{target_disk_size}GiB，其他不变，请重新估价，仍不要部署。")
            checks["disk update has requested capacity"] = disk_size(updated, workspace) == target_disk_size
            checks["disk update actually changed template or effective parameters"] = (
                updated.get("effective_deployment_parameters") != baseline.get("effective_deployment_parameters")
                or template_hash != before_hash
            )
            turn("解释调整后的方案和费用，不再修改。", unchanged=True)
            final = turn(encode_deployment_confirmation("cancel"), terminal=True)
            checks["cancel ends step2"] = final.get("status") == "cancelled"
    except Exception as exc:
        checks["scenario completed"] = False
        turns.append({"error_type": type(exc).__name__})
    finally:
        server.terminate()
        records = capture.stop()
        observed = sorted(
            {
                str(r.get("attributes", {}).get("gen_ai.request.model"))
                for r in records
                if r.get("attributes", {}).get("gen_ai.request.model")
            }
        )
        checks["only requested real model ran"] = observed == [args.model]
        checks["server stopped"] = server.process is not None and server.process.poll() is not None
        for filename in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
            (config_dir / filename).unlink(missing_ok=True)
        checks["credential copies removed"] = not (config_dir / ".credentials.yml").exists()
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "scenario": args.scenario,
        "model": args.model,
        "observed_models": observed,
        "checks": checks,
        "turns": turns,
        "cleanup_status": "not-needed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=("step1", "step2"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=700)
    parser.add_argument("--allow-real-cloud", action="store_true")
    args = parser.parse_args()
    if not args.allow_real_cloud:
        parser.error("--allow-real-cloud is required")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    summary = run(args)
    (args.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
