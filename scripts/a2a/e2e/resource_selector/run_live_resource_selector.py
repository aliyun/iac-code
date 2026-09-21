#!/usr/bin/env python3
"""Opt-in live LLM + Alibaba Cloud A2A resource-selector E2E scenarios."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

E2E_ROOT = Path(__file__).resolve().parents[1]
if str(E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(E2E_ROOT))

from common import (  # noqa: E402
    ManagedServer,
    StreamSummary,
    _free_port,
    _server_env,
    _split_python_command,
    _write_json,
    _write_server_config,
    run_llm_preflight,
    stream_message,
    wait_for_server,
)

from iac_code.a2a.resource_selector import RESOURCE_SELECTION_QUERY_PREFIX  # noqa: E402
from iac_code.config import DEFAULT_MODEL, load_saved_model  # noqa: E402
from iac_code.resource_selector.profiles import get_profile  # noqa: E402
from iac_code.resource_selector.query import (  # noqa: E402
    ResourceSelectorQueryService,
    _selection_values,
)
from iac_code.services.configuration_readiness import configuration_readiness  # noqa: E402

CONFIG_FILES = (".credentials.yml", ".cloud-credentials.yml", "settings.yml")
SCENARIOS = (
    "selected-next-turn",
    "restart-before-answer",
    "canceled-next-turn",
    "empty-list-canceled-next-turn",
    "sequential-vpc-vswitch",
    "duplicate-and-conflict",
    "pipeline-handoff-normal",
    "pipeline-stage-selection",
)
EXPECTED_SELECTOR_ID = "vpc.vpc"
QUERY_OPERATION_KEY = "vpc.vpc.dataapi.vpc.describevpcs"
VSWITCH_SELECTOR_ID = "vpc.vswitch"
VSWITCH_QUERY_OPERATION_KEY = "vpc.vswitch.list"
PIPELINE_NAMES = {
    "pipeline-handoff-normal": "immediate_handoff",
    "pipeline-stage-selection": "resource_selector_stage",
}
FORBIDDEN_LOG_MARKERS = (
    "active session is not ready",
    "is in terminal state",
    "different Context",
    "Task already exists",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default=SCENARIOS[0])
    parser.add_argument("--region", default="cn-hangzhou")
    parser.add_argument("--source-config-dir", type=Path, default=Path("~/.iac-code"))
    parser.add_argument("--server-cwd", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--server-timeout", type=float, default=60.0)
    parser.add_argument("--preflight-timeout", type=float, default=180.0)
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    parser.add_argument("--skip-llm-preflight", action="store_true")
    args = parser.parse_args()
    if not args.allow_real_cloud:
        parser.error("--allow-real-cloud is required because this runner calls the configured LLM and cloud account")
    return args


def _copy_runtime_config(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    for name in CONFIG_FILES:
        source_path = source / name
        if not source_path.is_file():
            continue
        target = destination / name
        shutil.copy2(source_path, target)
        os.chmod(target, 0o600)


def _refresh_source_cloud_credentials(source: Path) -> None:
    previous = os.environ.get("IAC_CODE_CONFIG_DIR")
    os.environ["IAC_CODE_CONFIG_DIR"] = str(source)
    try:
        from iac_code.services.providers.aliyun import AliyunCredentials

        credential = AliyunCredentials.load_from_iac_code_config()
        if credential is not None and credential.mode == "OAuth":
            AliyunCredentials.refresh_oauth_if_needed(credential)
    finally:
        if previous is None:
            os.environ.pop("IAC_CODE_CONFIG_DIR", None)
        else:
            os.environ["IAC_CODE_CONFIG_DIR"] = previous


def _restrict_runtime_permissions(config_dir: Path) -> None:
    settings_path = config_dir / "settings.yml"
    try:
        raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
    except yaml.YAMLError as exc:
        raise RuntimeError("source settings.yml is invalid") from exc
    settings = dict(raw) if isinstance(raw, dict) else {}
    settings["permissions"] = {
        "mode": "default",
        "allow": ["resolve_cloud_resource_selector", "select_cloud_resource"],
        "deny": [
            "aliyun_api",
            "ros_stack",
            "ros_stack_instances",
            "ros_stack_group",
            "ros_template",
            "ros_template_scratch",
            "ros_diagnostic",
            "ros_resource_type_registration",
            "ros_tag",
        ],
        "ask": [],
        "additional_directories": [],
        "audit": {"include_tool_input": False},
    }
    settings_path.write_text(yaml.safe_dump(settings, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(settings_path, 0o600)


def _secret_values(config_dir: Path) -> set[str]:
    markers = ("key", "token", "secret", "password", "credential")
    values: set[str] = set()

    def visit(value: object, *, sensitive: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                visit(item, sensitive=sensitive or any(marker in str(key).casefold() for marker in markers))
        elif isinstance(value, list):
            for item in value:
                visit(item, sensitive=sensitive)
        elif sensitive and isinstance(value, str) and len(value) >= 6:
            values.add(value)

    for name in CONFIG_FILES:
        path = config_dir / name
        if not path.is_file():
            continue
        try:
            visit(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError):
            continue
    return values


def _scrub_artifacts(run_dir: Path, secrets: Iterable[str]) -> None:
    replacements = tuple(value for value in secrets if value)
    if not replacements:
        return
    for path in run_dir.rglob("*"):
        if not path.is_file() or ".runtime-config" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        redacted = text
        for value in replacements:
            redacted = redacted.replace(value, "<redacted>")
        if redacted != text:
            path.write_text(redacted, encoding="utf-8")


def _walk(value: object) -> Iterable[object]:
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _resource_selection_inputs(events_path: Path) -> list[dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        for value in _walk(event):
            if not isinstance(value, Mapping) or value.get("kind") != "cloud_resource_selection":
                continue
            selector = value.get("selector")
            input_id = value.get("inputId")
            tool_use_id = value.get("toolUseId")
            if not isinstance(selector, Mapping) or not isinstance(input_id, str) or not isinstance(tool_use_id, str):
                continue
            key = (input_id, tool_use_id)
            candidate = dict(value)
            previous = found.get(key)
            candidate_score = sum(
                int(isinstance(candidate.get(field), str) and bool(candidate.get(field)))
                for field in ("requestTaskId", "contextId", "pipelineName", "pipelineRunId")
            )
            previous_score = (
                sum(
                    int(isinstance(previous.get(field), str) and bool(previous.get(field)))
                    for field in ("requestTaskId", "contextId", "pipelineName", "pipelineRunId")
                )
                if previous is not None
                else -1
            )
            if candidate_score > previous_score:
                found[key] = candidate
    return list(found.values())


def _iac_code_values(events_path: Path, key: str) -> list[Any]:
    values: list[Any] = []
    if not events_path.is_file():
        return values
    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        for item in _walk(event):
            if not isinstance(item, Mapping):
                continue
            metadata = item.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            iac_code = metadata.get("iac_code")
            if isinstance(iac_code, Mapping) and key in iac_code:
                values.append(iac_code[key])
    return values


def _selection_response(
    pending: Mapping[str, Any],
    *,
    status: str,
    value: str | None = None,
    label: str | None = None,
    options_empty: bool | None = None,
) -> dict[str, Any]:
    selector = pending.get("selector")
    if not isinstance(selector, Mapping):
        raise AssertionError("resource selection input has no selector contract")
    response: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "status": status,
        "requestTaskId": pending["requestTaskId"],
        "contextId": pending["contextId"],
        "inputId": pending["inputId"],
        "toolUseId": pending["toolUseId"],
    }
    if status == "selected":
        if not isinstance(value, str) or not value:
            raise AssertionError("selected response requires a resource value")
        response.update(selectorId=selector["id"], value=value, label=label or value)
        if isinstance(selector.get("source"), Mapping):
            response["source"] = dict(selector["source"])
    elif isinstance(options_empty, bool):
        response["optionsEmpty"] = options_empty
    return response


def _candidate_label(response: object, value: str) -> str:
    for item in _walk(response):
        if not isinstance(item, Mapping) or value not in item.values():
            continue
        for key in ("VpcName", "ResourceName", "Name", "name", "label"):
            label = item.get(key)
            if isinstance(label, str) and label:
                return label
    return value


async def _query_candidates(
    pending: Mapping[str, Any],
    *,
    operation_key: str,
    dynamic_parameters: Mapping[str, Any],
) -> tuple[ResourceSelectorQueryService, list[str], object]:
    selector = pending.get("selector")
    selector_id = selector.get("id") if isinstance(selector, Mapping) else None
    if not isinstance(selector_id, str):
        raise AssertionError("resource selection input has no selector id")
    metadata = selector.get("associationPropertyMetadata")
    if not isinstance(metadata, Mapping):
        raise AssertionError("selector metadata is missing")
    profile = get_profile(selector_id)
    if profile is None:
        raise AssertionError("{} selector profile is unavailable".format(selector_id))
    operation = next((item for item in profile.operations if item.key == operation_key), None)
    if operation is None:
        raise AssertionError("{} live query operation is unavailable".format(operation_key))
    service = ResourceSelectorQueryService()
    projected = await service.query(
        pending_payload=pending,
        operation_key=operation.key,
        dynamic_parameters=dynamic_parameters,
    )
    values = sorted(_selection_values(profile, operation.key, projected, dynamic_parameters, metadata))
    return service, values, projected


async def _query_real_vpc(pending: Mapping[str, Any]) -> tuple[str, str, int, list[str]]:
    selector = pending.get("selector")
    if not isinstance(selector, Mapping) or selector.get("id") != EXPECTED_SELECTOR_ID:
        raise AssertionError("LLM did not request the expected vpc.vpc selector")
    metadata = selector.get("associationPropertyMetadata")
    service, values, projected = await _query_candidates(
        pending,
        operation_key=QUERY_OPERATION_KEY,
        dynamic_parameters={"PageSize": 20},
    )
    if not values:
        raise AssertionError(
            "the configured account has no selectable VPC in {}; create or expose one before rerunning".format(
                metadata.get("RegionId") if isinstance(metadata, Mapping) else "the requested region"
            )
        )
    value = values[0]
    await service.validate_selection(pending_payload=pending, value=value)
    return value, _candidate_label(projected, value), len(values), values


async def _query_empty_vpc(pending: Mapping[str, Any]) -> int:
    missing_vpc_id = "vpc-{}".format(uuid.uuid4().hex[:20])
    service, values, _projected = await _query_candidates(
        pending,
        operation_key=QUERY_OPERATION_KEY,
        dynamic_parameters={"VpcId": missing_vpc_id, "PageSize": 20},
    )
    if values:
        raise AssertionError("the deterministic missing-id VPC query unexpectedly returned candidates")
    if service.options_empty(pending_payload=pending) is not True:
        raise AssertionError("the successful empty VPC query did not produce optionsEmpty=true")
    return 0


def _derived_pending(
    pending: Mapping[str, Any],
    *,
    selector_id: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(pending)
    result["selector"] = {
        "id": selector_id,
        "associationPropertyMetadata": dict(metadata),
        "source": None,
    }
    return result


async def _vpc_with_vswitch(pending: Mapping[str, Any]) -> tuple[str, str, int]:
    selector = pending.get("selector")
    metadata = selector.get("associationPropertyMetadata") if isinstance(selector, Mapping) else None
    region = metadata.get("RegionId") if isinstance(metadata, Mapping) else None
    service, values, projected = await _query_candidates(
        pending,
        operation_key=QUERY_OPERATION_KEY,
        dynamic_parameters={"PageSize": 20},
    )
    for value in values:
        vswitch_pending = _derived_pending(
            pending,
            selector_id=VSWITCH_SELECTOR_ID,
            metadata={"RegionId": region, "VpcId": value},
        )
        _vswitch_service, vswitches, _response = await _query_candidates(
            vswitch_pending,
            operation_key=VSWITCH_QUERY_OPERATION_KEY,
            dynamic_parameters={"PageNumber": 1, "PageSize": 20},
        )
        if vswitches:
            await service.validate_selection(pending_payload=pending, value=value)
            return value, _candidate_label(projected, value), len(values)
    raise AssertionError("none of the queried VPCs contains a selectable VSwitch")


async def _query_real_vswitch(pending: Mapping[str, Any], *, expected_vpc_id: str) -> tuple[str, str, int]:
    selector = pending.get("selector")
    if not isinstance(selector, Mapping) or selector.get("id") != VSWITCH_SELECTOR_ID:
        raise AssertionError("LLM did not request the expected vpc.vswitch selector")
    metadata = selector.get("associationPropertyMetadata")
    if not isinstance(metadata, Mapping) or metadata.get("VpcId") != expected_vpc_id:
        raise AssertionError("the VSwitch selector did not preserve the selected VPC as VpcId metadata")
    service, values, projected = await _query_candidates(
        pending,
        operation_key=VSWITCH_QUERY_OPERATION_KEY,
        dynamic_parameters={"PageNumber": 1, "PageSize": 20},
    )
    if not values:
        raise AssertionError("the selected VPC has no selectable VSwitch")
    value = values[0]
    await service.validate_selection(pending_payload=pending, value=value)
    return value, _candidate_label(projected, value), len(values)


def _assert_input_required(summary: StreamSummary) -> None:
    if "TASK_STATE_INPUT_REQUIRED" not in summary.status_states:
        raise AssertionError("A2A task did not enter input-required state: {}".format(summary.status_states))


def _assert_turn_ready(summary: StreamSummary, *, name: str) -> None:
    # Normal A2A turns intentionally settle in INPUT_REQUIRED so the context is
    # ready for the next user message.  COMPLETED is also valid for providers
    # or transports that publish an explicit terminal completion.
    ready_states = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED"}
    if not ready_states.intersection(summary.status_states):
        raise AssertionError("{} did not become ready for the next turn: {}".format(name, summary.status_states))


class _Harness:
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        run_dir: Path,
        config_dir: Path,
        pipeline_name: str | None = None,
    ) -> None:
        self.args = args
        self.run_dir = run_dir
        self.config_dir = config_dir
        self.workspace = run_dir / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.port = args.port or _free_port(args.host)
        self.server_url = "http://{}:{}".format(args.host, self.port)
        self.config_path = _write_server_config(
            run_dir,
            host=args.host,
            port=self.port,
            auto_approve_permissions=False,
        )
        self.env = _server_env(
            os.environ.copy(),
            provider=args.provider,
            model=args.model,
            api_base=args.api_base,
        )
        self.env.update(
            {
                "IAC_CODE_CONFIG_DIR": str(config_dir),
                "IAC_CODE_A2A_RESOURCE_SELECTOR_ENABLED": "true",
                "IAC_CODE_A2A_SAFE_MODE": "true",
            }
        )
        self.server: ManagedServer | None = None
        self.server_index = 0
        self.lifecycle: list[dict[str, Any]] = []
        self.pipeline_name = pipeline_name

    def start(self) -> None:
        self.server_index += 1
        server_args: list[str] | None = None
        if self.pipeline_name is not None:
            pipeline_root = Path(__file__).resolve().parent / "live_pipelines"
            server_args = [
                "-m",
                "scripts.a2a.e2e.resource_selector.live_pipeline_server",
                "--host",
                self.args.host,
                "--port",
                str(self.port),
                "--config-dir",
                str(self.config_dir),
                "--persistence-dir",
                str(self.run_dir / "a2a-persistence"),
                "--artifact-dir",
                str(self.run_dir / "a2a-artifacts"),
                "--workspace",
                str(self.workspace),
                "--pipeline-root",
                str(pipeline_root),
                "--pipeline-name",
                self.pipeline_name,
            ]
            if self.args.model:
                server_args.extend(("--model", self.args.model))
        self.server = ManagedServer(
            python_cmd=_split_python_command(self.args.python),
            config_path=self.config_path,
            process_cwd=str(self.args.server_cwd.expanduser().resolve()),
            allowed_cwd=str(self.workspace),
            env=self.env,
            log_prefix=self.run_dir / "server-{}".format(self.server_index),
            run_mode="pipeline" if self.pipeline_name is not None else "normal",
            server_args=server_args,
        )
        self.server.start()
        wait_for_server(self.server_url, timeout=self.args.server_timeout)
        self.lifecycle.append({"event": "started", "index": self.server_index, "at": time.time()})

    def restart_after_crash(self) -> None:
        if self.server is None:
            raise RuntimeError("server is not running")
        self.server.kill9()
        self.lifecycle.append({"event": "killed", "index": self.server_index, "at": time.time()})
        self.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.terminate()
            self.lifecycle.append({"event": "stopped", "index": self.server_index, "at": time.time()})
        _write_json(self.run_dir / "server-lifecycle.json", self.lifecycle)

    def stream(self, *, name: str, prompt: str, context_id: str = "", task_id: str = "") -> StreamSummary:
        return stream_message(
            server_url=self.server_url,
            cwd=str(self.workspace),
            prompt=prompt,
            name=name,
            run_dir=self.run_dir,
            timeout=self.args.turn_timeout,
            context_id=context_id,
            task_id=task_id,
            redaction_env=self.env,
        )


def _server_logs(run_dir: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(run_dir.glob("server-*.log"))
        if path.is_file()
    )


def _result_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _single_pending(
    *,
    run_dir: Path,
    stream_name: str,
    summary: StreamSummary,
    region: str,
    selector_id: str = EXPECTED_SELECTOR_ID,
) -> dict[str, Any]:
    inputs = _resource_selection_inputs(run_dir / "{}.events.jsonl".format(stream_name))
    if len(inputs) != 1:
        raise AssertionError("expected exactly one resource-selection input, got {}".format(len(inputs)))
    pending = inputs[0]
    if pending.get("requestTaskId") != summary.task_id or pending.get("contextId") != summary.context_id:
        raise AssertionError("resource-selection correlation fields do not match the A2A task")
    selector = pending.get("selector")
    metadata = selector.get("associationPropertyMetadata") if isinstance(selector, Mapping) else None
    if not isinstance(selector, Mapping) or selector.get("id") != selector_id:
        raise AssertionError("LLM did not request the expected {} selector".format(selector_id))
    if not isinstance(metadata, Mapping) or metadata.get("RegionId") != region:
        raise AssertionError("LLM did not preserve the requested selector region")
    return pending


def _answer_selection(
    harness: _Harness,
    *,
    name: str,
    pending: Mapping[str, Any],
    response: Mapping[str, Any],
) -> StreamSummary:
    return harness.stream(
        name=name,
        prompt=RESOURCE_SELECTION_QUERY_PREFIX + json.dumps(response, ensure_ascii=False, separators=(",", ":")),
        context_id=str(pending["contextId"]),
        task_id=str(pending["requestTaskId"]),
    )


def _assert_no_new_selector(run_dir: Path, *, stream_name: str, previous_input_id: object) -> None:
    repeated = [
        item
        for item in _resource_selection_inputs(run_dir / "{}.events.jsonl".format(stream_name))
        if item.get("inputId") != previous_input_id
    ]
    if repeated:
        raise AssertionError("model emitted another selector after a final selected/canceled response")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    source_config = args.source_config_dir.expanduser().resolve()
    config_dir = run_dir / ".runtime-config"
    secrets: set[str] = set()
    harness: _Harness | None = None
    previous_config_dir = os.environ.get("IAC_CODE_CONFIG_DIR")
    try:
        _refresh_source_cloud_credentials(source_config)
        _copy_runtime_config(source_config, config_dir)
        _restrict_runtime_permissions(config_dir)
        secrets = _secret_values(config_dir)
        os.environ["IAC_CODE_CONFIG_DIR"] = str(config_dir)
        if args.model:
            os.environ["IAC_CODE_MODEL"] = args.model
        effective_model = args.model or load_saved_model() or DEFAULT_MODEL
        readiness = configuration_readiness(model=effective_model)
        _write_json(run_dir / "readiness.json", readiness)
        if not readiness["llm"]["ready"] or not readiness["cloud"]["ready"]:
            raise RuntimeError("both LLM and Alibaba Cloud configuration must be ready: {}".format(readiness))

        pipeline_name = PIPELINE_NAMES.get(args.scenario)
        harness = _Harness(args, run_dir=run_dir, config_dir=config_dir, pipeline_name=pipeline_name)
        if not args.skip_llm_preflight:
            preflight = run_llm_preflight(
                python_cmd=_split_python_command(args.python),
                cwd=str(args.server_cwd.expanduser().resolve()),
                env=harness.env,
                timeout=args.preflight_timeout,
                run_dir=run_dir,
            )
            if preflight.get("ok") is not True:
                raise RuntimeError("LLM preflight failed: {}".format(preflight.get("summary")))

        harness.start()
        context_id = ""
        pipeline_handoff_verified = False
        if args.scenario == "pipeline-handoff-normal":
            handoff = harness.stream(
                name="pipeline-handoff",
                prompt="请立即完成当前测试步骤并切换到普通会话。",
            )
            _assert_turn_ready(handoff, name="pipeline handoff")
            if not handoff.normal_handoff_ready:
                raise AssertionError("Pipeline did not publish a durable normal-chat handoff")
            pipeline_handoff_verified = True
            context_id = handoff.context_id

        if args.scenario == "sequential-vpc-vswitch":
            initial_prompt = (
                "请先使用云资源选择器让我选择一个 {region} 地域的已有 VPC；选择完成后，再让我从该 VPC "
                "中选择一个已有 VSwitch。每次只选择一个资源，不创建、修改或删除资源，不要使用 aliyun_api "
                "预先列举。解析时只用简短英文关键词 VPC 和 VSwitch。"
            ).format(region=args.region)
        else:
            initial_prompt = (
                "请使用云资源选择器让我选择一个 {region} 地域的已有 VPC。只选择，不创建、修改或删除任何资源；"
                "不要使用 aliyun_api 预先列举资源。请用简短英文关键词 VPC 解析选择器并调用选择工具。"
            ).format(region=args.region)
        initial = harness.stream(name="initial", prompt=initial_prompt, context_id=context_id)
        _assert_input_required(initial)
        pending = _single_pending(
            run_dir=run_dir,
            stream_name="initial",
            summary=initial,
            region=args.region,
        )

        candidate_count = 0
        selected_value = ""
        selected_label = ""
        queried_values: list[str] = []
        if args.scenario == "empty-list-canceled-next-turn":
            candidate_count = asyncio.run(_query_empty_vpc(pending))
        elif args.scenario == "sequential-vpc-vswitch":
            selected_value, selected_label, candidate_count = asyncio.run(_vpc_with_vswitch(pending))
        else:
            selected_value, selected_label, candidate_count, queried_values = asyncio.run(_query_real_vpc(pending))

        if args.scenario == "restart-before-answer":
            harness.restart_after_crash()
        if args.scenario == "canceled-next-turn":
            response = _selection_response(pending, status="canceled", options_empty=False)
        elif args.scenario == "empty-list-canceled-next-turn":
            response = _selection_response(pending, status="canceled", options_empty=True)
        else:
            response = _selection_response(
                pending,
                status="selected",
                value=selected_value,
                label=selected_label,
            )
        answer = _answer_selection(harness, name="answer", pending=pending, response=response)

        secondary_selector_id = ""
        secondary_candidate_count = 0
        duplicate_acknowledged = False
        conflict_rejected = False
        if args.scenario == "sequential-vpc-vswitch":
            _assert_input_required(answer)
            second_inputs = [
                item
                for item in _resource_selection_inputs(run_dir / "answer.events.jsonl")
                if item.get("inputId") != pending.get("inputId")
            ]
            if len(second_inputs) != 1:
                raise AssertionError("expected exactly one VSwitch selection after the VPC answer")
            second_pending = second_inputs[0]
            selector = second_pending.get("selector")
            metadata = selector.get("associationPropertyMetadata") if isinstance(selector, Mapping) else None
            if (
                not isinstance(selector, Mapping)
                or selector.get("id") != VSWITCH_SELECTOR_ID
                or not isinstance(metadata, Mapping)
                or metadata.get("RegionId") != args.region
            ):
                raise AssertionError("the second selector is not the expected regional VSwitch contract")
            vswitch_value, vswitch_label, secondary_candidate_count = asyncio.run(
                _query_real_vswitch(second_pending, expected_vpc_id=selected_value)
            )
            second_response = _selection_response(
                second_pending,
                status="selected",
                value=vswitch_value,
                label=vswitch_label,
            )
            answer = _answer_selection(
                harness,
                name="second-answer",
                pending=second_pending,
                response=second_response,
            )
            _assert_turn_ready(answer, name="second selection answer")
            _assert_no_new_selector(
                run_dir,
                stream_name="second-answer",
                previous_input_id=second_pending.get("inputId"),
            )
            secondary_selector_id = VSWITCH_SELECTOR_ID
        else:
            _assert_turn_ready(answer, name="selection answer")
            _assert_no_new_selector(run_dir, stream_name="answer", previous_input_id=pending.get("inputId"))

        if args.scenario == "duplicate-and-conflict":
            _answer_selection(harness, name="duplicate", pending=pending, response=response)
            duplicate_acks = [
                item
                for item in _iac_code_values(run_dir / "duplicate.events.jsonl", "inputReceived")
                if isinstance(item, Mapping)
            ]
            duplicate_acknowledged = any(
                item.get("duplicate") is True or item.get("replayed") is True for item in duplicate_acks
            )
            if not duplicate_acknowledged:
                raise AssertionError("an identical selection retry was not acknowledged idempotently")
            if _resource_selection_inputs(run_dir / "duplicate.events.jsonl"):
                raise AssertionError("an identical selection retry re-opened a selector")
            conflict = _selection_response(pending, status="canceled", options_empty=False)
            conflict_error = ""
            try:
                _answer_selection(harness, name="conflict", pending=pending, response=conflict)
            except RuntimeError as exc:
                conflict_error = str(exc)
            conflict_evidence = conflict_error
            conflict_path = run_dir / "conflict.events.jsonl"
            if conflict_path.is_file():
                conflict_evidence += conflict_path.read_text(encoding="utf-8", errors="replace")
            conflict_evidence += _server_logs(run_dir)
            conflict_rejected = "resource_selection_resume_invalid" in conflict_evidence
            if not conflict_rejected:
                raise AssertionError("a conflicting retry was not rejected")
            if len(queried_values) < 1:
                raise AssertionError("the duplicate scenario did not use a real VPC candidate")

        if args.scenario == "pipeline-stage-selection":
            if not answer.normal_handoff_ready:
                raise AssertionError("the Pipeline did not resume, complete and hand off after selection")
            pipeline_handoff_verified = True

        continuation_token = "LIVE_SELECTOR_NEXT_TURN_OK_" + uuid.uuid4().hex[:8]
        next_turn = harness.stream(
            name="next-turn",
            prompt="这是同一会话的下一条普通消息。只回复：{}".format(continuation_token),
            context_id=initial.context_id,
        )
        _assert_turn_ready(next_turn, name="next turn")
        if continuation_token not in next_turn.text:
            raise AssertionError("the real LLM did not complete the next turn in the same A2A context")

        logs = _server_logs(run_dir)
        allowed_markers = {"Task already exists"} if args.scenario == "duplicate-and-conflict" else set()
        log_lines = logs.splitlines()
        forbidden = []
        for marker in FORBIDDEN_LOG_MARKERS:
            if marker in allowed_markers:
                continue
            matching = [line for line in log_lines if marker.casefold() in line.casefold()]
            if marker == "different Context":
                matching = [line for line in matching if "was created in a different Context" not in line]
            if matching:
                forbidden.append(marker)
        if forbidden:
            raise AssertionError("server logs contain lifecycle regressions: {}".format(forbidden))
        result = {
            "passed": True,
            "scenario": args.scenario,
            "region": args.region,
            "selectorId": EXPECTED_SELECTOR_ID,
            "candidateCount": candidate_count,
            "selectedValueDigest": _result_digest(selected_value) if selected_value else "",
            "secondarySelectorId": secondary_selector_id,
            "secondaryCandidateCount": secondary_candidate_count,
            "taskId": initial.task_id,
            "contextId": initial.context_id,
            "serverStarts": harness.server_index,
            "nextTurnCompleted": True,
            "duplicateAcknowledged": duplicate_acknowledged,
            "conflictRejected": conflict_rejected,
            "pipelineHandoffVerified": pipeline_handoff_verified,
            "usedRealLlm": True,
            "usedRealCloudQuery": True,
        }
        _write_json(run_dir / "summary.json", result)
        return result
    finally:
        if harness is not None:
            harness.stop()
        _scrub_artifacts(run_dir, secrets)
        shutil.rmtree(config_dir, ignore_errors=True)
        if previous_config_dir is None:
            os.environ.pop("IAC_CODE_CONFIG_DIR", None)
        else:
            os.environ["IAC_CODE_CONFIG_DIR"] = previous_config_dir


def main() -> None:
    args = _parse_args()
    result = _run(args)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
