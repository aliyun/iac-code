#!/usr/bin/env python3
"""Real LLM + HTTP/SSE A2A selection recovery and Step context boundaries.

No model/tool substitutes, deployments, injected answers or application patches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from iac_code.pipeline.engine.ui_contract import encode_deployment_confirmation, encode_selected_candidate  # noqa: E402
from scripts.a2a.e2e.common import (  # noqa: E402
    ManagedServer,
    _free_port,
    _write_server_config,
    fetch_pipeline_state,
    stream_message,
    wait_for_server,
)
from scripts.ci.a2a_context_evidence import (  # noqa: E402
    finish_permissions,
    fresh_history,
    main_requests,
    request_window,
    retained_history,
    rolled_back_to,
)
from scripts.ci.run_coupon_budget_a2a import tool_inputs  # noqa: E402
from scripts.ci.run_ecs_retention_a2a import is_readonly_call  # noqa: E402
from scripts.ci.run_knowledge_wiki_a2a import configure_permissions  # noqa: E402
from scripts.ci.run_plan_cost_refresh_a2a import envelopes  # noqa: E402
from scripts.ci.run_promotional_offer_followup_a2a import visible_pipeline_text  # noqa: E402
from scripts.ci.run_step_followup_a2a import INITIAL, results, stable  # noqa: E402
from scripts.observability.local_observe.e2e_audit import ObserveCapture  # noqa: E402

QUESTION = "为什么推荐这个方案？解释一下当前配置和费用，先不调整。"
STEP2_QUESTION = "解释一下已经生成的模板和当前报价，先不改参数，也不部署。"
RESELECT = (
    "重新选择方案：仍在杭州，改为两台按量 ECS 部署 nginx，每台至少40GiB系统盘，"
    "需要公网访问，给我新的双机方案，先不要部署，也不要创建云资源。"
)


def run(args):
    config = Path(os.environ["IAC_CODE_CONFIG_DIR"])
    if not config.resolve().is_relative_to(args.run_dir.resolve()):
        raise ValueError("Use isolated configuration through run_e2e.py")
    configure_permissions(config)
    workspace = args.run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    port = _free_port("127.0.0.1")
    server_config = _write_server_config(args.run_dir, host="127.0.0.1", port=port, auto_approve_permissions=False)
    capture = ObserveCapture(args.run_dir / "telemetry").start()
    env = {
        **os.environ,
        **capture.env,
        "PYTHONPATH": str(REPO / "src"),
        "IAC_CODE_MODE": "pipeline",
        "IAC_CODE_PIPELINE_NAME": "selling_solution_first",
        "IAC_CODE_PROVIDER": args.provider,
        "IAC_CODE_MODEL": args.model,
        "IAC_CODE_WIKI_AUTO_SEARCH": "true",
        "IAC_CODE_ROS_AGENT_AWARENESS": "true",
        "IAC_CODE_AUTO_MEMORY": "false",
        "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1",
        "IAC_CODE_CONFIG_BACKUP_DIR": str(args.run_dir / "backup"),
    }
    servers, turns, audits, checks, observations = [], [], [], {}, {}
    context = task = ""
    conclusion = {}

    def start_server():
        server = ManagedServer(
            python_cmd=[args.python],
            config_path=server_config,
            process_cwd=str(REPO),
            allowed_cwd=str(workspace),
            env=env,
            log_prefix=args.run_dir / f"server-{len(servers) + 1}",
        )
        servers.append(server)
        server.start()
        wait_for_server(f"http://127.0.0.1:{port}", timeout=60, owned_server=server)
        return server

    def turn(prompt, *, unchanged=False, terminal=False, invalid_selection=False):
        nonlocal context, task, conclusion
        before = {call["id"] for call in tool_inputs(config)}
        prior = json.loads(json.dumps(conclusion))
        name = f"turn-{len(turns) + 1}"
        start_ns = time.time_ns()
        response = stream_message(
            server_url=f"http://127.0.0.1:{port}",
            cwd=str(workspace),
            prompt=prompt,
            context_id=context,
            task_id=task,
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
            server_url=f"http://127.0.0.1:{port}",
            cwd=str(workspace),
            timeout=args.timeout,
            env=env,
        )
        context, task = response.context_id, response.task_id
        path = args.run_dir / f"{name}.events.jsonl"
        events = envelopes(path)
        completed = [event for event in events if event.get("eventType") == "step_completed"]
        value = next(
            (
                event.get("data", {}).get("conclusion")
                for event in reversed(completed)
                if isinstance(event.get("data", {}).get("conclusion"), dict)
            ),
            {},
        )
        calls = [call for call in tool_inputs(config) if call["id"] not in before]
        outcomes = results(config)
        success = [call for call in calls if outcomes.get(call["id"], {}).get("is_error") is False]
        checks[name + ": real HTTP/SSE"] = response.response_content_type == "text/event-stream"
        checks[name + ": no cloud mutations"] = all(is_readonly_call(call) for call in calls)
        checks[name + ": never enters deployment"] = not any(
            event.get("eventType") == "step_started" and event.get("step", {}).get("id") == "deploying"
            for event in events
        )
        if invalid_selection:
            checks[name + ": old selection rejected"] = any(
                event.get("data", {}).get("validationError", event.get("data", {}).get("validation_error"))
                == "invalid_candidate_selection" for event in events
            )
            checks[name + ": no tools on rejected selection"] = not calls
            value = prior
        elif not terminal:
            checks[name + ": valid waiting conclusion"] = (
                response.last_status_state == "TASK_STATE_INPUT_REQUIRED"
                and (value.get("status") in {"awaiting_selection", "awaiting_confirmation"})
            )
        if unchanged:
            checks[name + ": new turn visibly answered"] = bool(visible_pipeline_text(path).strip())
            checks[name + ": current successful completion"] = any(call["name"] == "complete_step" for call in success)
            checks[name + ": conclusion unchanged"] = bool(prior) and stable(value) == stable(prior)
            checks[name + ": unchanged event"] = (
                bool(completed) and completed[-1].get("data", {}).get("conclusion_unchanged") is True
            )
        turn_record = {
            "name": name,
            "start_ns": start_ns,
            "end_ns": time.time_ns(),
            "prompt": prompt,
            "status": value.get("status"),
            "state": response.last_status_state,
            "prior_tool_ids": sorted(before),
            "successful_tool_ids": [call["id"] for call in success],
        }
        turns.append(turn_record)
        conclusion = value
        fetch_pipeline_state(
            server_url=f"http://127.0.0.1:{port}",
            context_id=context,
            task_id=task,
            run_dir=args.run_dir,
            name=name,
            redaction_env=env,
        )
        print(json.dumps({"scenario": args.scenario, "turn": name, "status": value.get("status")}), flush=True)
        if not terminal and not invalid_selection and not checks[name + ": valid waiting conclusion"]:
            raise RuntimeError("WaitingStateNotReached")
        return value, events, turn_record

    try:
        server = start_server()
        initial, _, initial_turn = turn(INITIAL)
        if args.scenario == "selection-recovery":
            pid = server.process.pid
            server.terminate()
            server = start_server()
            checks["A2A process actually restarted"] = server.process.pid != pid
        kept, _, followup = turn(QUESTION, unchanged=True)
        checks["candidate batch survives consultation and recovery"] = bool(initial.get("candidate_set_id")) and (
            initial.get("candidate_set_id") == kept.get("candidate_set_id")
            and initial.get("candidates") == kept.get("candidates")
        )
        audits.append(
            ("same Step1 history", followup, "retained", [INITIAL, QUESTION], set(initial_turn["successful_tool_ids"]))
        )
        candidate = kept["candidates"][0]
        materialized, selected_events, selected_turn = turn(encode_selected_candidate(candidate["name"], 0))
        checks["selection advances directly to Step2"] = any(
            event.get("eventType") == "step_started"
            and event.get("step", {}).get("id") == "materialize_selected_candidate"
            for event in selected_events
        )
        audits.append(
            (
                "Step1 to Step2 fresh history",
                selected_turn,
                "fresh",
                [INITIAL, QUESTION],
                set(selected_turn["prior_tool_ids"]),
            )
        )
        cost = materialized.get("selected_candidate_result", {}).get("cost", {})
        checks["real materialization and quote"] = (
            materialized.get("status") == "awaiting_confirmation" and bool(cost)
        )
        observations["materialization Preview"] = {
            "preview_ready_for_create": materialized.get("preview_ready_for_create"),
            "succeeded": cost.get("preview_validation", {}).get("succeeded"),
            "blocking": False,
        }
        if args.scenario == "selection-recovery":
            final, _, _ = turn(encode_deployment_confirmation("cancel"), terminal=True)
            checks["ends without deployment"] = final.get("status") == "cancelled"
        else:
            _, _, step2_followup = turn(STEP2_QUESTION, unchanged=True)
            audits.append(
                (
                    "same Step2 history",
                    step2_followup,
                    "retained",
                    [STEP2_QUESTION],
                    set(selected_turn["successful_tool_ids"]),
                )
            )
            revised, events, rollback_turn = turn(RESELECT)
            checks["Step2 rollback reaches Step1"] = rolled_back_to(events, "solution_planning_and_selection")
            checks["rollback produces a new candidate batch"] = revised.get("status") == "awaiting_selection" and (
                bool(revised.get("candidate_set_id"))
                and revised.get("candidate_set_id") != initial.get("candidate_set_id")
            )
            audits.append(
                (
                    "rollback reentry fresh history",
                    rollback_turn,
                    "rollback",
                    [INITIAL, QUESTION, STEP2_QUESTION],
                    set(rollback_turn["prior_tool_ids"]),
                )
            )
            old_names = {item["name"] for item in revised.get("candidates", [])}
            if candidate["name"] not in old_names:
                _, _, rejected_turn = turn(encode_selected_candidate(candidate["name"], None), invalid_selection=True)
                audits.append(("old selection does not invoke model", rejected_turn, "no-model", [], set()))
            else:
                observations["old selection identity test"] = "not_exercised_same_name_and_index_payload"
            _, _, resumed = turn("解释一下新的双机方案和费用，先不要调整。", unchanged=True)
            step1_ids = {
                str(event.get("data", {}).get("toolUseId"))
                for event in events
                if event.get("eventType") == "tool_started"
                and event.get("step", {}).get("id") == "solution_planning_and_selection"
            }
            audits.append(
                (
                    "new Step1 history after rollback",
                    resumed,
                    "retained",
                    [resumed["prompt"]],
                    step1_ids.intersection(rollback_turn["successful_tool_ids"]),
                )
            )
            final, _, _ = turn("取消这次购买，不需要继续了。", terminal=True)
            checks["ends without deployment"] = final.get("status") == "rejected"
    except Exception as exc:
        checks["scenario completed"] = False
        observations["error_type"] = type(exc).__name__
        observations["last_turn"] = len(turns)
    finally:
        for server in servers:
            server.terminate()
        records = capture.stop()
        requests = main_requests(records)
        for label, window, kind, prompts, ids in audits:
            selected = request_window(requests, window)
            if kind == "no-model":
                checks[label] = not selected
            elif kind == "retained":
                evidence = retained_history(selected[0], prompts=prompts, response_ids=ids) if selected else {}
                checks[label] = bool(evidence) and all(evidence.values())
                observations[label] = evidence
            else:
                matches = [request for request in selected if fresh_history(request, prompts=prompts, old_ids=ids)]
                checks[label] = bool(matches) if kind == "rollback" else bool(selected) and selected[0] in matches
        observed = sorted(
            {
                str(record.get("attributes", {}).get("gen_ai.request.model"))
                for record in records
                if record.get("attributes", {}).get("gen_ai.request.model")
            }
        )
        checks["only requested real model ran"] = observed == [args.model]
        checks["all A2A processes stopped"] = all(
            server.process.poll() is not None for server in servers if server.process
        )
        for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
            (config / name).unlink(missing_ok=True)
        checks["credential copies removed"] = not any(
            (config / name).exists() for name in (".credentials.yml", ".cloud-credentials.yml")
        )
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "scenario": args.scenario,
        "model": args.model,
        "checks": checks,
        "observations": observations,
        "turns": turns,
        "cleanup_status": "not-needed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("selection-recovery", "context-boundaries"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
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
    return int(summary["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
