#!/usr/bin/env python3
"""ECS retention regression gaps: real LLM, production HTTP/SSE A2A, read-only cloud.

No answer hints, replaced model/tool results, deployments or scenario retries.
Detailed messages stay in the private run directory; summaries contain checks.
Invoke through scripts/ci/run_e2e.py for isolated credentials/model settings.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from iac_code.pipeline.engine.ui_contract import encode_selected_candidate  # noqa: E402
from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    stream_message,
    wait_for_server,
)
from scripts.ci.a2a_context_evidence import finish_permissions, may_read_generated_result  # noqa: E402, F401
from scripts.ci.run_coupon_budget_a2a import tool_inputs  # noqa: E402
from scripts.ci.run_knowledge_wiki_a2a import configure_permissions, final_text, wiki_evidence  # noqa: E402
from scripts.ci.run_plan_cost_refresh_a2a import envelopes  # noqa: E402
from scripts.ci.run_promotional_offer_followup_a2a import visible_pipeline_text  # noqa: E402
from scripts.ci.run_ros_agent_awareness import SCENARIOS as ROUTES  # noqa: E402
from scripts.ci.run_step_followup_a2a import results, stable  # noqa: E402
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402


@dataclass(frozen=True)
class Scenario:
    prompt: str
    mode: str = "pipeline"
    select: bool = False
    awareness: bool = True
    product: str = ""


SCENARIOS = {
    "n29-swas": Scenario(
        "部署 Next.js、PostgreSQL、Redis 和定时任务，优先轻量应用服务器，2核4GiB、60GB、4–5Mbps，"
        "预算300–600元/年，先考虑杭州，给方案和费用参考，先不部署。", product="swas"),
    "n56-price-chain": Scenario(
        "在杭州部署网站，4核16GiB ECS，40GiB系统盘、公网用按流量计费EIP，按量付费连续运行30天。"
        "先给一个方案和费用估计，不部署或创建资源。", select=True, product="ecs"),
    "n59-price-chain": Scenario(
        "把现有网站迁到阿里云杭州，同等配置4核16GiB ECS、40GiB系统盘和200GiB数据盘，"
        "ESSD PL0，公网用按流量EIP，按量付费连续运行30天。先给一个方案和费用估计，不部署。",
        select=True, product="ecs"),
    "purchase-routing": Scenario("我要购买按量付费的tdx，可信计算服务器", product="ecs"),
    "noncloud-purchase": Scenario("我要购买两斤苹果，帮我推荐一下。"),
    "sizing": Scenario(
        "阿里云经济型 e 和突发性能型 t 有什么区别？它们都是靠 CPU 积分控制性能的吗？", mode="normal"),
    "pricing-normal": Scenario(
        "只查询、不购买：阿里云杭州 ecs.e-c1m2.large、40GiB cloud_essd_entry 系统盘，"
        "分别查询按量小时价和包月1个月的价格，说明实付价、原价和周期。", mode="normal", product="ecs"),
    "api-aliases": Scenario(
        "只做只读API兼容性验证，不购买：杭州 ecs.e-c1m2.large，分别调用 ECS DescribePrice 和 DescribePrices，"
        "资源类型instance，Period=1，PriceUnit=Hour，系统盘cloud_essd_entry、40GiB。"
        "第二次用SystemDisk.1.Category和SystemDisk.Capacity传相同系统盘。"
        "再分别用PriceUnit=Month、PriceUnit=Monthly和PeriodType=Monthly查询同一配置1个月，比较各组实付价是否一致。",
        mode="normal", product="ecs"),
    **{f"route-{name}": Scenario(spec.prompt, awareness=spec.enabled) for name, spec in ROUTES.items()},
}


def successful_api(calls: list[dict], outcomes: dict[str, dict]) -> list[dict]:
    return [call for call in calls if call["name"] == "aliyun_api"
            and outcomes.get(call["id"], {}).get("is_error") is False]


def is_readonly_call(call: dict) -> bool:
    name, action = call["name"], str(call["input"].get("action", ""))
    query = action.startswith(("Describe", "List", "Get", "Query"))
    if name == "aliyun_api":
        return not action or query  # Missing action is a rejected malformed input, not an executed mutation.
    if name == "ros_resource_type_registration":
        return query
    if name.startswith("ros_"):
        return name in {"ros_estimate_template_cost", "ros_preview_stack", "ros_preview_template",
                        "ros_validate_template", "ros_get_template_parameter_constraints"}
    return True


def routing_checks(
    answer: str, *, handoff: bool, expected_region: str = "",
) -> dict[str, bool]:
    links = re.findall(r"https://ros\.console\.aliyun\.com/[^\s)<>\]]+", answer)
    if not handoff:
        return {"no unsolicited assistant handoff": not links}
    decoded = [parse_qs(urlsplit(link).query).get("prompt", [""])[0] for link in links]
    return {
        "visible regional assistant link carries a prompt": bool(decoded) and all(decoded),
        "assistant link shown once": len(links) == 1,
        "assistant destination uses configured region": not expected_region or bool(links) and all(
            urlsplit(link).path == f"/{expected_region}/chat" for link in links),
    }


def routing_observations(answer: str, expected_context: str = "") -> dict[str, bool]:
    """Literal identifier retention is diagnostic; a useful prompt may reasonably summarize it."""
    if not expected_context:
        return {}
    links = re.findall(r"https://ros\.console\.aliyun\.com/[^\s)<>\]]+", answer)
    decoded = [parse_qs(urlsplit(link).query).get("prompt", [""])[0] for link in links]
    return {"literal_request_context_preserved": any(expected_context in prompt for prompt in decoded)}


def pricing_checks(calls: list[dict], outcomes: dict[str, dict]) -> dict[str, bool]:
    prices = [c for c in successful_api(calls, outcomes) if c["input"].get("action") in {
        "DescribePrice", "DescribePrices"}]
    return {
        "real ECS quote succeeded": bool(prices),
        "successful quote includes system disk": any(
            re.search(r"SystemDisk|system_disk", json.dumps(c["input"])) for c in prices),
    }


def alias_checks(calls: list[dict], outcomes: dict[str, dict]) -> dict[str, bool]:
    from iac_code.tools.cloud.aliyun.api_hooks import normalize_parameters

    groups: dict[str, dict[str, list[Any]]] = {unit: {"standard": [], "alias": []} for unit in ("hour", "month")}
    exercised = dict.fromkeys((
        "DescribePrices", "SystemDisk.1.Category", "SystemDisk.Capacity", "PriceUnit=Monthly", "PeriodType=Monthly",
    ), False)
    baseline_dimensions = None
    for call in successful_api(calls, outcomes):
        if call["input"].get("action") not in {"DescribePrice", "DescribePrices"}:
            continue
        raw = call["input"].get("params", {})
        aliases = {
            "DescribePrices": call["input"]["action"] == "DescribePrices",
            "SystemDisk.1.Category": "SystemDisk.1.Category" in raw,
            "SystemDisk.Capacity": "SystemDisk.Capacity" in raw,
            "PriceUnit=Monthly": str(raw.get("PriceUnit", "")).lower() == "monthly",
            "PeriodType=Monthly": str(raw.get("PeriodType", "")).lower() == "monthly",
        }
        params = dict(raw)
        normalize_parameters("ecs", "DescribePrice", params)
        unit = str(params.get("PriceUnit", params.get("PeriodType", "Hour"))).lower()
        dimensions = {k: v for k, v in params.items() if k != "PriceUnit"}
        if baseline_dimensions is None:
            baseline_dimensions = dimensions
        if dimensions != baseline_dimensions:
            continue  # An additional different-capacity quote is not the requested alias comparison.
        try:
            body = json.loads(outcomes[call["id"]]["content"])
            price = body.get("PriceInfo", {}).get("Price", {}).get("TradePrice")
        except (ValueError, TypeError, AttributeError):
            continue
        if price is not None and unit in groups:
            groups[unit]["alias" if any(aliases.values()) else "standard"].append(price)
            exercised = {name: exercised[name] or seen for name, seen in aliases.items()}
    return {
        **{f"{unit} standard and alias quotes agree": bool(values["standard"] and values["alias"])
           and len({str(v) for prices in values.values() for v in prices}) == 1 for unit, values in groups.items()},
        **{f"successful alias exercised: {name}": value for name, value in exercised.items()},
    }


def requested_data_disk_preserved(conclusion: dict, workspace: Path) -> bool:
    """N59 explicitly requests one 200GiB ESSD PL0 data disk, not ESSD Entry."""
    from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load

    path = (workspace / str(conclusion.get("template_url", ""))).resolve()
    if not path.is_relative_to(workspace.resolve()) or not path.is_file():
        return False
    template = ros_yaml_load(path.read_text(encoding="utf-8"))
    parameters = {k: v.get("Default") for k, v in template.get("Parameters", {}).items()}
    parameters.update(conclusion.get("effective_deployment_parameters", {}))

    def resolve(value):
        if isinstance(value, dict) and "Ref" in value:
            return parameters.get(value["Ref"])
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    disks = []
    for resource in template.get("Resources", {}).values():
        props = resolve(resource.get("Properties", {}))
        disks.extend(props.get("DiskMappings", props.get("DataDisks", [])))
        if resource.get("Type") == "ALIYUN::ECS::Disk":
            disks.append(props)
    return any(str(d.get("Size")) in {"200", "200.0"} and d.get("Category") == "cloud_essd"
               and d.get("PerformanceLevel") == "PL0" for d in disks)


def run(args: argparse.Namespace) -> dict[str, Any]:
    spec = SCENARIOS[args.scenario]
    config = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config.resolve().is_relative_to(args.run_dir.resolve()):
        raise ValueError("Use run_e2e.py with an isolated configuration")
    configure_permissions(config)
    if args.scenario.startswith("route-"):
        # Region is a test credential preference, never a mutation of the cloud account.
        path = config / ".cloud-credentials.yml"
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
        region = ROUTES[args.scenario.removeprefix("route-")].region
        content["aliyun"].pop("region_id", None)
        if region:
            content["aliyun"]["region_id"] = region
        path.write_text(yaml.safe_dump(content), encoding="utf-8")
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    home = args.run_dir / "home"
    home.mkdir(exist_ok=True, mode=0o700)
    capture = ObserveCapture(args.run_dir / "telemetry").start()
    env = {
        **os.environ, **capture.env, "PYTHONPATH": str(REPO / "src"),
        "IAC_CODE_MODE": spec.mode, "IAC_CODE_PIPELINE_NAME": "selling_solution_first",
        "IAC_CODE_ROS_AGENT_AWARENESS": "true" if spec.awareness else "false",
        "IAC_CODE_WIKI_AUTO_SEARCH": "true", "IAC_CODE_PROVIDER": args.provider,
        "IAC_CODE_MODEL": args.model, "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1",
        "IAC_CODE_CONFIG_BACKUP_DIR": str(args.run_dir / "backup"),
        "HOME": str(home), "USERPROFILE": str(home),
    }
    port = _free_port("127.0.0.1")
    url = f"http://127.0.0.1:{port}"
    server = ManagedServer(
        python_cmd=[args.python],
        config_path=_write_server_config(args.run_dir, host="127.0.0.1", port=port,
                                         auto_approve_permissions=False),
        process_cwd=str(REPO), allowed_cwd=str(workspace), env=env,
        log_prefix=args.run_dir / "server", run_mode=spec.mode,
    )
    checks, turns, answers = {}, [], []
    context = task = ""
    all_calls = []

    def turn(prompt: str) -> dict:
        nonlocal context, task
        name = f"turn-{len(turns) + 1}"
        before = {c["id"] for c in tool_inputs(config)}
        started = time.monotonic()
        response = stream_message(server_url=url, cwd=str(workspace), prompt=prompt,
                                  context_id=context, task_id=task, name=name, run_dir=args.run_dir,
                                  timeout=args.timeout, redaction_env=env)
        response = finish_permissions(response, name=name, run_dir=args.run_dir, config=config,
                                      server_url=url, cwd=str(workspace), timeout=args.timeout, env=env)
        context, task = response.context_id, response.task_id
        path = args.run_dir / f"{name}.events.jsonl"
        events = envelopes(path)
        completed = [e for e in events if e.get("eventType") == "step_completed"]
        conclusion = next((e.get("data", {}).get("conclusion") for e in reversed(completed)
                           if isinstance(e.get("data", {}).get("conclusion"), dict)), {})
        calls = [c for c in tool_inputs(config) if c["id"] not in before]
        all_calls.extend(calls)
        outcomes = results(config)
        answer = visible_pipeline_text(path) or final_text(path) or response.text
        answers.append({"turn": name, "text": answer, "conclusion": conclusion})
        checks[name + ": real HTTP/SSE and completed turn"] = (
            response.response_content_type == "text/event-stream" and response.last_status_state in {
                "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED"})
        checks[name + ": no cloud writes or deployment"] = all(is_readonly_call(c) for c in calls)
        item = {"turn": name, "seconds": round(time.monotonic() - started, 2),
                "state": response.last_status_state, "status": conclusion.get("status"),
                "tools": [c["name"] for c in calls],
                "successful_cloud_actions": [c["input"].get("action") for c in successful_api(calls, outcomes)],
                "tool_errors": [{"name": c["name"], "action": c["input"].get("action")}
                                for c in calls if outcomes.get(c["id"], {}).get("is_error") is True],
                "wiki": wiki_evidence(calls, outcomes),
                "unchanged": any(e.get("data", {}).get("conclusion_unchanged") is True for e in completed)}
        turns.append(item)
        print(json.dumps({"scenario": args.scenario, "model": args.model,
                          "turn": name, "seconds": item["seconds"], "state": item["state"]}), flush=True)
        if response.normal_handoff_ready or response.last_status_state == "TASK_STATE_COMPLETED":
            task = ""
        return {"conclusion": conclusion, "answer": answer, "events": events, "calls": calls,
                "outcomes": outcomes, "response": response, "item": item}

    try:
        server.start()
        wait_for_server(url, timeout=60, owned_server=server)
        first = turn(spec.prompt)
        if args.scenario.startswith("route-"):
            from iac_code.services.providers.aliyun import DEFAULT_REGION

            route = ROUTES[args.scenario.removeprefix("route-")]
            checks.update(routing_checks(first["answer"], handoff=route.handoff,
                                         expected_region=route.region or DEFAULT_REGION))
            first["item"]["routing_observations"] = routing_observations(first["answer"], route.expected_context)
        elif args.scenario == "noncloud-purchase":
            checks["noncloud purchase does not propose ECS resources"] = not first["conclusion"].get("candidates")
        elif args.scenario == "purchase-routing":
            checks["explicit cloud purchase stays in planning"] = (
                first["conclusion"].get("status") == "awaiting_selection" or any(
                    e.get("eventType") == "input_required" and e.get("data", {}).get("kind") == "ask_user_question"
                    for e in first["events"]))
            checks.update(routing_checks(first["answer"], handoff=False))
        elif args.scenario == "sizing":
            checks["correct CPU credit distinction"] = bool(
                re.search(r"(?:经济型|\be\b).{0,100}(?:不|无|非).{0,15}积分", first["answer"], re.S | re.I))
            checks["burstable credits explained"] = bool(re.search(r"突发.{0,150}积分", first["answer"], re.S))
        elif args.scenario == "n29-swas":
            calls = successful_api(all_calls, results(config))
            checks["real SWAS package query succeeded"] = any(c["input"].get("action") == "ListPlans" for c in calls)
            checks["SWAS Wiki actually read"] = any("/swas/" in p for p in first["item"]["wiki"]["pages"])
            turn("地域改为深圳，因为已有OSS在那里，其他目标及300–600元年预算不变。先更新方案，不部署。")
        else:
            checks.update(pricing_checks(all_calls, results(config)))
            if args.scenario == "api-aliases":
                compared = alias_checks(all_calls, results(config))
                checks.update(compared)
                first["item"]["alias_observations"] = {
                    "not_exercised": [key.removeprefix("successful alias exercised: ")
                                      for key, ok in compared.items()
                                      if key.startswith("successful alias exercised: ") and not ok],
                }
        if spec.select:
            candidates = first["conclusion"].get("candidates") or []
            checks["Step1 reached candidate selection"] = bool(candidates)
            if candidates:
                second = turn(encode_selected_candidate(candidates[0]["name"], 0))
                checks["Step2 reached confirmation with real quote"] = (
                    second["conclusion"].get("status") == "awaiting_confirmation" and bool(
                        second["conclusion"].get("selected_candidate_result", {}).get("cost")))
                if args.scenario == "n59-price-chain":
                    checks["requested 200GiB PL0 data disk preserved"] = requested_data_disk_preserved(
                        second["conclusion"], workspace)
                discussion = turn("解释当前费用构成及流量费是否包含，暂时不调整方案，不部署。")
                checks["Step2 consultation preserves conclusion"] = discussion["item"]["unchanged"] and stable(
                    discussion["conclusion"]) == stable(second["conclusion"])
        checks["only read-only cloud tools used"] = all(is_readonly_call(c) for c in all_calls)
    except Exception as exc:
        checks["scenario completed"] = False
        turns.append({"error_type": type(exc).__name__})
    finally:
        server.terminate()
        records = capture.stop()
        observed = sorted({str(r.get("attributes", {}).get("gen_ai.request.model")) for r in records
                           if r.get("attributes", {}).get("gen_ai.request.model")})
        checks["requested real model observed without fallback"] = observed == [args.model]
        checks["owned server stopped"] = server.process is not None and server.process.poll() is not None
        (args.run_dir / "answers.json").write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
            (config / name).unlink(missing_ok=True)
    return {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "scenario": args.scenario, "model": args.model, "effort": "low", "turns": turns,
            "observed_models": observed, "cleanup_status": "not-needed",
            "manual_review_required": ["price arithmetic and period", "discount attribution", "EIP exemptions",
                                       "read-before-use order", "hardware prose", "quote response projection"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--allow-real-cloud", action="store_true")
    args = parser.parse_args()
    if not args.allow_real_cloud:
        parser.error("--allow-real-cloud is required")
    args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary = run(args)
    (args.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
