#!/usr/bin/env python3
"""Deterministic real-process fixture for A2A execution-control E2E tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

TOKEN = "execution-control-e2e-token"
TOOL_NAME = "fixture_long_operation"
TOOL_USE_ID = "fixture-long-tool-1"
STACK_ID = "stack-execution-control-e2e-0001"
STACK_INSTANCES_OPERATION_ID = "stack-instances-operation-e2e-0001"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--persistence-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("normal", "pipeline"), required=True)
    parser.add_argument(
        "--scenario",
        choices=(
            "warm-resume-pausing",
            "warm-resume-paused",
            "disconnect-timeout-after-operation-id",
            "disconnect-timeout-inflight-sync-call",
            "disconnect-timeout-backup-blocked",
            "natural-completion-while-pausing",
            "slow-termination-storage",
            "terminate-during-bootstrap",
            "terminate-during-bootstrap-disconnected",
            "disconnect-timeout-during-turn-backup",
            "legacy-cancel-idle",
            "slow-rollover-storage",
            "stack-instances-timeout-after-operation-id",
            "stack-instances-terminate-inflight",
            "recovery-during-normal-rollover",
        ),
        required=True,
    )
    return parser.parse_args()


class _LifecycleLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def write(self, event: str, **data: Any) -> None:
        record = {"event": event, "time": time.time(), **data}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _marker(control_dir: Path, name: str) -> Path:
    return control_dir / name


async def _wait_for_marker(control_dir: Path, name: str, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while not _marker(control_dir, name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for marker {}".format(name))
        await asyncio.sleep(0.02)


def _wait_for_marker_sync(control_dir: Path, name: str, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while not _marker(control_dir, name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for marker {}".format(name))
        time.sleep(0.02)


def _has_tool_result(messages: list[Any], tool_use_id: str) -> bool:
    return any(
        getattr(block, "type", None) == "tool_result" and getattr(block, "tool_use_id", None) == tool_use_id
        for message in messages
        for block in (message.content if isinstance(getattr(message, "content", None), list) else [])
    )


def _create_fixture_runtime(options: Any, *, scenario: str, run_dir: Path) -> Any:
    from iac_code.a2a.backup import run_sync_fenced_with_cancel_completion
    from iac_code.a2a.execution_control import record_execution_external_operation
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.services.agent_factory import AgentRuntime
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
    from iac_code.types.permissions import PermissionResult

    control_dir = run_dir / "control"
    provider_log = _LifecycleLog(run_dir / "provider-lifecycle.jsonl")
    tool_log = _LifecycleLog(run_dir / "tool-lifecycle.jsonl")
    provider = _FixtureProvider(scenario=scenario, control_dir=control_dir, lifecycle=provider_log)

    class FixtureLongOperation(Tool):
        @property
        def name(self) -> str:
            return TOOL_NAME

        @property
        def description(self) -> str:
            return "Wait at a deterministic remote-operation boundary."

        @property
        def input_schema(self) -> dict[str, Any]:
            return {"type": "object", "additionalProperties": False}

        @property
        def timeout(self) -> float | None:
            return None

        def is_read_only(self, input: dict[str, Any] | None = None) -> bool:  # noqa: A002 - Tool protocol
            del input
            return True

        async def check_permissions(self, input: dict[str, Any], context: Any = None) -> PermissionResult:
            if scenario == "slow-termination-storage":
                tool_log.write("permission.requested")
                return PermissionResult(behavior="ask", message="Allow the fixture operation?")
            return PermissionResult(behavior="allow")

        async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
            del tool_input
            tool_log.write("tool.started", toolUseId=context.tool_use_id, scenario=scenario)
            if scenario == "disconnect-timeout-inflight-sync-call":
                return await self._run_sync_call(context)
            if scenario == "disconnect-timeout-after-operation-id":
                return await self._run_ros_stack(context)
            if scenario.startswith("stack-instances-"):
                return await self._run_ros_stack_instances(context)

            try:
                await _wait_for_marker(control_dir, "release-tool")
            except asyncio.CancelledError:
                tool_log.write("tool.cancelled")
                raise
            tool_log.write("tool.completed", result="DURABLE_FIXTURE_RESULT")
            return ToolResult.success("DURABLE_FIXTURE_RESULT")

        async def _run_ros_stack(self, context: ToolContext) -> ToolResult:
            from iac_code.tools.cloud.aliyun.ros_stack import RosStack

            def create_stack(request: Any) -> Any:
                del request
                tool_log.write("sdk.started")
                tool_log.write("sdk.returned", stackId=STACK_ID)
                return SimpleNamespace(body=SimpleNamespace(stack_id=STACK_ID))

            class OfflineRosStack(RosStack):
                def _get_client(self, region: str) -> Any:
                    return SimpleNamespace(create_stack=create_stack)

                async def wait_for_stack_operation(self, *args: Any, **kwargs: Any) -> ToolResult:
                    tool_log.write("polling.started", stackId=STACK_ID)
                    try:
                        return await super().wait_for_stack_operation(*args, **kwargs)
                    except asyncio.CancelledError:
                        tool_log.write("polling.cancelled", stackId=STACK_ID)
                        raise

            stack = OfflineRosStack(allow_pipeline_deployment_actions=True)
            stack.poll_interval = 3600
            params = {
                "StackName": "execution-control-fixture",
                "TemplateBody": '{"ROSTemplateFormatVersion":"2015-09-01","Resources":{}}',
            }
            if context.pipeline_mode:
                template_path = Path(context.cwd) / "fixture-template.json"
                template_path.write_text(params.pop("TemplateBody"), encoding="utf-8")
                params["TemplateURL"] = str(template_path)
            # Keep cloud preflight offline; submission, result recording and
            # polling cancellation all run through the production RosStack.
            with patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None):
                return await stack.execute(
                    tool_input={
                        "action": "CreateStack",
                        "region_id": "cn-hangzhou",
                        "params": params,
                    },
                    context=context,
                )

        async def _run_ros_stack_instances(self, context: ToolContext) -> ToolResult:
            from iac_code.tools.cloud.aliyun.ros_stack_instances import RosStackInstances

            def create_stack_instances(request: Any) -> Any:
                del request
                tool_log.write("sdk.started", action="CreateStackInstances")
                if scenario == "stack-instances-terminate-inflight":
                    _wait_for_marker_sync(control_dir, "release-sdk")
                tool_log.write("sdk.returned", operationId=STACK_INSTANCES_OPERATION_ID)
                return SimpleNamespace(body=SimpleNamespace(operation_id=STACK_INSTANCES_OPERATION_ID))

            class OfflineRosStackInstances(RosStackInstances):
                poll_interval = 3600

                def _get_client(self, region: str) -> Any:
                    return SimpleNamespace(create_stack_instances=create_stack_instances)

                async def _initiate(self, *args: Any, **kwargs: Any) -> str:
                    # Observe the production submission/recording boundary;
                    # never inject externalOperations from the fixture.
                    operation_id = await super()._initiate(*args, **kwargs)
                    tool_log.write("polling.started", operationId=operation_id)
                    return operation_id

            try:
                return await OfflineRosStackInstances().execute(
                    tool_input={
                        "action": "CreateStackInstances",
                        "region_id": "cn-hangzhou",
                        "params": {"StackGroupName": "execution-control-fixture"},
                    },
                    context=context,
                )
            except asyncio.CancelledError:
                tool_log.write("tool.cancelled")
                raise

        async def _run_sync_call(self, context: ToolContext) -> ToolResult:
            def blocking_create_stack() -> str:
                tool_log.write("sdk.started")
                _wait_for_marker_sync(control_dir, "release-sdk")
                tool_log.write("sdk.returned", stackId=STACK_ID)
                return STACK_ID

            async def save_late_result(result: str | None, error: BaseException | None) -> None:
                if error is None and result:
                    await record_execution_external_operation(
                        product="ROS",
                        action="CreateStack",
                        outcome="accepted",
                        resource_type="ALIYUN::ROS::Stack",
                        resource_id=result,
                        region_id="cn-hangzhou",
                        tool_use_id=context.tool_use_id,
                    )
                    tool_log.write("operation.recorded", operationId=result, late=True)

            result = await run_sync_fenced_with_cancel_completion(blocking_create_stack, save_late_result)
            await record_execution_external_operation(
                product="ROS",
                action="CreateStack",
                outcome="accepted",
                resource_type="ALIYUN::ROS::Stack",
                resource_id=result,
                region_id="cn-hangzhou",
                tool_use_id=context.tool_use_id,
            )
            tool_log.write("operation.recorded", operationId=result, late=False)
            return ToolResult.success(result)

    registry = ToolRegistry()
    registry.register(FixtureLongOperation())
    storage = SessionStorage()
    storage.ensure_v2_session_dir_for_new_session(str(options.cwd), str(options.session_id))
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="Deterministic execution-control fixture.",
        tool_registry=registry,
        max_turns=4,
        session_storage=storage,
        session_id=options.session_id,
        resume_messages=options.resume_messages,
        cwd=options.cwd,
    )
    return AgentRuntime(
        agent_loop=loop,
        session_id=loop.session_id,
        tool_registry=registry,
        provider_manager=provider,
        command_registry=None,
        task_manager=None,
        memory_manager=None,
        legacy_memory_manager=None,
    )


class _FixtureProvider:
    def __init__(self, *, scenario: str, control_dir: Path, lifecycle: _LifecycleLog) -> None:
        self._scenario = scenario
        self._control_dir = control_dir
        self._lifecycle = lifecycle
        self._calls = 0

    def get_model_name(self) -> str:
        return "execution-control-fixture"

    def get_provider_display(self) -> str:
        return "Execution control fixture"

    async def stream(
        self,
        messages: list[Any],
        system: str,
        tools: list[Any] | None = None,
        max_tokens: int = 8192,
        **kwargs: Any,
    ):
        del system, max_tokens, kwargs
        from iac_code.types.stream_events import (
            MessageEndEvent,
            MessageStartEvent,
            TextDeltaEvent,
            ToolUseEndEvent,
            ToolUseStartEvent,
            Usage,
        )

        self._calls += 1
        tool_names = [str(getattr(tool, "name", "")) for tool in tools or []]
        message_summary = []
        for message in messages:
            content = getattr(message, "content", None)
            blocks = content if isinstance(content, list) else []
            message_summary.append(
                {
                    "role": str(getattr(message, "role", "")),
                    "contentTypes": [str(getattr(block, "type", type(block).__name__)) for block in blocks],
                    "toolUseIds": [
                        str(tool_use_id)
                        for block in blocks
                        if (tool_use_id := getattr(block, "tool_use_id", None)) is not None
                    ],
                }
            )
        self._lifecycle.write(
            "provider.called",
            call=self._calls,
            tools=tool_names,
            messageCount=len(messages),
            messageSummary=message_summary,
        )
        if self._scenario == "slow-termination-storage" and not (self._control_dir / "other-context").exists():
            yield MessageStartEvent(message_id="fixture-permission-call")
            yield ToolUseStartEvent(tool_use_id=TOOL_USE_ID, name=TOOL_NAME)
            yield ToolUseEndEvent(tool_use_id=TOOL_USE_ID, name=TOOL_NAME, input={})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            return
        if self._scenario in {
            "slow-termination-storage",
            "terminate-during-bootstrap",
            "terminate-during-bootstrap-disconnected",
            "slow-rollover-storage",
            "disconnect-timeout-during-turn-backup",
            "recovery-during-normal-rollover",
        }:
            if self._scenario in {"slow-rollover-storage", "recovery-during-normal-rollover"} and self._calls == 1:
                from iac_code.a2a.execution_control import register_execution_task

                async def background() -> None:
                    self._lifecycle.write("background.started")
                    try:
                        await _wait_for_marker(self._control_dir, "release-background")
                    finally:
                        self._lifecycle.write("background.finished")

                register_execution_task(asyncio.create_task(background()), kind="background_agent")
            yield MessageStartEvent(message_id="fixture-simple-final")
            yield TextDeltaEvent(
                text="RECOVERY_ROLLOVER_TURN_{}".format(self._calls)
                if self._scenario == "recovery-during-normal-rollover"
                else "ISOLATION_FIXTURE_FINAL"
            )
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())
            self._lifecycle.write("provider.completed", call=self._calls)
            return
        if self._scenario == "legacy-cancel-idle":
            try:
                yield MessageStartEvent(message_id="fixture-legacy-cancel")
                yield TextDeltaEvent(text="LEGACY_CANCEL_FIRST_TOKEN")
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())
            finally:
                self._lifecycle.write("provider.closed")
            return
        if self._scenario == "natural-completion-while-pausing":
            yield MessageStartEvent(message_id="fixture-natural-final")
            yield TextDeltaEvent(text="NATURAL_FIXTURE_FINAL")
            self._lifecycle.write("provider.awaiting_message_end", call=self._calls)
            await _wait_for_marker(self._control_dir, "release-provider")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())
            self._lifecycle.write("provider.completed", call=self._calls)
            return

        if not _has_tool_result(messages, TOOL_USE_ID):
            yield MessageStartEvent(message_id="fixture-tool-call")
            yield ToolUseStartEvent(tool_use_id=TOOL_USE_ID, name=TOOL_NAME)
            yield ToolUseEndEvent(tool_use_id=TOOL_USE_ID, name=TOOL_NAME, input={})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            return

        if "complete_step" in tool_names and not _has_tool_result(messages, "fixture-complete-step-1"):
            yield MessageStartEvent(message_id="fixture-complete-step")
            yield ToolUseStartEvent(tool_use_id="fixture-complete-step-1", name="complete_step")
            yield ToolUseEndEvent(
                tool_use_id="fixture-complete-step-1",
                name="complete_step",
                input={"conclusion": {"status": "completed", "result": "DURABLE_FIXTURE_RESULT"}},
            )
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            return

        yield MessageStartEvent(message_id="fixture-final")
        yield TextDeltaEvent(text="EXECUTION_CONTROL_FIXTURE_FINAL")
        yield MessageEndEvent(stop_reason="end_turn", usage=Usage())


def _write_pipeline(pipeline_dir: Path) -> None:
    (pipeline_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (pipeline_dir / "pipeline.yaml").write_text(
        "name: execution_control_fixture\n"
        "context_dependencies:\n"
        "  result: []\n"
        "max_rollbacks: 1\n"
        "steps:\n"
        "  - id: fixture_step\n"
        "    conclusion_field: result\n"
        "    forward: null\n"
        "    description: Execute one deterministic long operation.\n"
        "    prompt: prompts/fixture.md\n"
        "    max_agent_turns: 4\n"
        "    tools:\n"
        "      include: [fixture_long_operation]\n"
        "      exclude: []\n",
        encoding="utf-8",
    )
    (pipeline_dir / "prompts" / "fixture.md").write_text(
        "Call fixture_long_operation once, then call complete_step with its result.",
        encoding="utf-8",
    )


class _GatedPublisher:
    """Use the production worker, but begin publication only after a marker."""

    def __init__(self, staging_root: Path, backup_root: Path, marker: Path) -> None:
        self._staging_root = staging_root
        self._backup_root = backup_root
        self._marker = marker
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from iac_code.services.session_backup_staging import SessionBackupStagingWorker

        worker = SessionBackupStagingWorker(self._staging_root, self._backup_root)

        def run() -> None:
            while not self._stop.wait(0.05):
                if self._marker.exists():
                    worker.run_once()

        self._thread = threading.Thread(target=run, name="execution-control-gated-backup", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def main() -> int:
    args = _parse_args()
    paths = [
        args.run_dir,
        args.config_dir,
        args.persistence_dir,
        args.artifact_dir,
        args.workspace,
        args.staging_dir,
        args.backup_dir,
        args.run_dir / "control",
    ]
    for path in paths:
        path.expanduser().resolve().mkdir(parents=True, exist_ok=True)

    run_dir = args.run_dir.expanduser().resolve()
    pipeline_dir = run_dir / "fixture-pipeline"
    _write_pipeline(pipeline_dir)
    os.environ["IAC_CODE_CONFIG_DIR"] = str(args.config_dir.expanduser().resolve())
    os.environ["IAC_CODE_MODE"] = args.mode
    os.environ["IACCODE_A2A_ALLOWED_CWDS"] = str(args.workspace.expanduser().resolve())
    os.environ["IAC_CODE_CONFIG_BACKUP_TMP_DIR"] = str(args.staging_dir.expanduser().resolve())
    os.environ["IAC_CODE_CONFIG_BACKUP_DIR"] = str(args.backup_dir.expanduser().resolve())

    import uvicorn

    from iac_code.a2a import executor as executor_module
    from iac_code.a2a import pipeline_executor as pipeline_executor_module
    from iac_code.a2a.app import create_app
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    lifecycle = _LifecycleLog(run_dir / "fixture-lifecycle.jsonl")
    control_dir = run_dir / "control"

    def runtime_factory(options: Any) -> Any:
        if args.scenario.startswith("terminate-during-bootstrap"):
            lifecycle.write("bootstrap.started", sessionId=options.session_id)
            _wait_for_marker_sync(control_dir, "release-bootstrap")
        runtime = _create_fixture_runtime(options, scenario=args.scenario, run_dir=run_dir)
        if args.scenario.startswith("terminate-during-bootstrap"):
            original_close = runtime.aclose

            async def gated_close() -> None:
                lifecycle.write("bootstrap.cleanup_started")
                await _wait_for_marker(control_dir, "release-bootstrap-cleanup")
                await original_close()
                lifecycle.write("bootstrap.closed")

            runtime.aclose = gated_close
            lifecycle.write("bootstrap.returned")
        return runtime

    executor_module.create_agent_runtime = runtime_factory
    pipeline_executor_module.create_agent_runtime = runtime_factory

    def create_fixture_pipeline(*unused_args: Any, **kwargs: Any) -> PipelineRunner:
        del unused_args
        return PipelineRunner(pipeline_dir=pipeline_dir, **kwargs)

    pipeline_executor_module.create_pipeline = create_fixture_pipeline

    if (
        args.scenario == "disconnect-timeout-backup-blocked"
        or args.scenario.startswith("terminate-during-bootstrap")
        or args.scenario.startswith("stack-instances-")
    ):
        from iac_code.a2a.transports import dispatcher as dispatcher_module
        from iac_code.services.session_backup_staging import (
            A2ASessionBackupRuntime,
            StagedSessionBackupService,
        )

        service = StagedSessionBackupService(args.staging_dir.expanduser().resolve())
        original_wait = service.wait_until_shared_committed

        def short_wait(*wait_args: Any, **wait_kwargs: Any) -> Any:
            wait_kwargs["timeout_seconds"] = min(float(wait_kwargs.get("timeout_seconds", 30.0)), 0.35)
            return original_wait(*wait_args, **wait_kwargs)

        if args.scenario == "disconnect-timeout-backup-blocked":
            service.wait_until_shared_committed = short_wait  # type: ignore[method-assign]
        publisher = _GatedPublisher(
            args.staging_dir.expanduser().resolve(),
            args.backup_dir.expanduser().resolve(),
            run_dir / "control" / "allow-shared",
        )
        dispatcher_module.create_a2a_session_backup_runtime = lambda: A2ASessionBackupRuntime(
            service=service,
            staging_process=publisher,  # type: ignore[arg-type]
        )

    if args.scenario == "disconnect-timeout-during-turn-backup":
        from iac_code.services.session_backup import BackupReason
        from iac_code.services.session_backup_staging import StagedSessionBackupService

        original_backup = StagedSessionBackupService.backup_session

        def gated_turn_backup(self: Any, *backup_args: Any, **backup_kwargs: Any) -> Any:
            if backup_kwargs.get("reason") == BackupReason.NORMAL_TURN_END:
                lifecycle.write("turn-backup.started")
                _wait_for_marker_sync(control_dir, "release-turn-backup")
            return original_backup(self, *backup_args, **backup_kwargs)

        StagedSessionBackupService.backup_session = gated_turn_backup

    if args.scenario == "slow-termination-storage":
        from iac_code.a2a.persistence import A2APersistenceStore

        original_context_write = A2APersistenceStore.save_context

        def gated_context_write(self: Any, snapshot: Any) -> None:
            target = control_dir / "arm-termination-storage"
            gated = target.exists() and snapshot.context_id == json.loads(target.read_text())["contextId"]
            if gated:
                on_event_loop = threading.current_thread() is threading.main_thread()
                lifecycle.write("termination.commit_started", onEventLoop=on_event_loop)
                _wait_for_marker_sync(control_dir, "release-storage")
                assert not on_event_loop, "termination context write blocked the server event loop"
            original_context_write(self, snapshot)
            if gated:
                lifecycle.write("termination.commit_finished")

        A2APersistenceStore.save_context = gated_context_write
    if args.scenario == "recovery-during-normal-rollover":
        from iac_code.a2a.execution_control import ExecutionController
        from iac_code.services.session_storage import SessionStorage

        original_load = SessionStorage.load
        load_lock = threading.Lock()
        recovery_read_claimed = False

        def gated_recovery_load(self: Any, *load_args: Any, **load_kwargs: Any) -> Any:
            nonlocal recovery_read_claimed
            with load_lock:
                gated = (control_dir / "arm-recovery").exists() and not recovery_read_claimed
                if gated:
                    recovery_read_claimed = True
            messages = original_load(self, *load_args, **load_kwargs)
            if gated:
                lifecycle.write("recovery.read_started", messageCount=len(messages))
                _wait_for_marker_sync(control_dir, "release-recovery")
                lifecycle.write("recovery.read_finished")
            return messages

        SessionStorage.load = gated_recovery_load
        original_rollover = ExecutionController.rollover_normal_execution

        async def observed_rollover(self: Any, **kwargs: Any) -> None:
            old_execution_id = self.execution_id
            await original_rollover(self, **kwargs)
            lifecycle.write(
                "recovery.rollover_completed",
                oldExecutionId=old_execution_id,
                executionId=self.execution_id,
                taskId=self.task_id,
            )

        ExecutionController.rollover_normal_execution = observed_rollover

    if args.scenario == "slow-rollover-storage":
        from iac_code.a2a import execution_control as control_module

        original_write = control_module.atomic_write_json

        def gated_rollover(path: Path, value: Any) -> None:
            if (control_dir / "arm-rollover").exists() and value.get("phase") == "running":
                lifecycle.write("rollover.commit_started", contextId=value.get("contextId"))
                _wait_for_marker_sync(control_dir, "release-storage")
            original_write(path, value)
            if (control_dir / "arm-rollover").exists() and value.get("phase") == "running":
                lifecycle.write("rollover.commit_finished")

        control_module.atomic_write_json = gated_rollover
    if args.scenario == "legacy-cancel-idle":
        from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

        # Park publication after the first delta so the stream driver waits for
        # its next pull. This is the cancellation boundary that leaked activity.
        os.environ["IAC_CODE_A2A_EXTREME_PERFORMANCE"] = "0"
        original_publish = PipelineA2AEventPublisher.publish

        async def gated_publish(self: Any, event: Any, **kwargs: Any) -> Any:
            result = await original_publish(self, event, **kwargs)
            if pipeline_executor_module._text_delta_output(event) == "LEGACY_CANCEL_FIRST_TOKEN":
                lifecycle.write("publication.blocked")
                await _wait_for_marker(control_dir, "release-publication")
            return result

        PipelineA2AEventPublisher.publish = gated_publish

    def idle_shutdown() -> None:
        lifecycle.write("idle.shutdown")
        server.should_exit = True

    app = create_app(
        host=args.host,
        port=args.port,
        token=TOKEN,
        model="execution-control-fixture",
        persistence_dir=args.persistence_dir.expanduser().resolve(),
        artifact_dir=args.artifact_dir.expanduser().resolve(),
        auto_approve_permissions=False,
        idle_shutdown_seconds=1.0 if args.scenario == "legacy-cancel-idle" else 0,
        idle_shutdown_callback=idle_shutdown,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
