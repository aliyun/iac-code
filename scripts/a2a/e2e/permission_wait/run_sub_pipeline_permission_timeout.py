#!/usr/bin/env python3
"""Controlled real-AgentLoop and real-parent-Pipeline Sub permission timeout E2E."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from iac_code.a2a.input_required import PermissionInputRegistry
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.agent_loop import AgentLoop
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.providers.base import ToolDefinition
from iac_code.services.permission_wait import (
    PermissionWaitCheckpointStore,
    PermissionWaitCoordinator,
    PermissionWaitPolicy,
)
from iac_code.services.session_backup import BackupReason, BackupResult, SessionBackupService
from iac_code.services.session_storage import SessionStorage
from iac_code.services.session_usage import SessionUsageStore
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.permissions import PermissionResult
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


class _Queue:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


class _AskWriteTool(Tool):
    def __init__(self) -> None:
        self.execution_count = 0

    @property
    def name(self) -> str:
        return "fixture_write"

    @property
    def description(self) -> str:
        return "Write fixture state."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"value": {"type": "string"}}}

    async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
        return PermissionResult(behavior="ask", message="Allow fixture write?")

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        self.execution_count += 1
        return ToolResult.success("fixture write executed")


class _CandidateProvider:
    def __init__(self, *, asks_permission: bool) -> None:
        self.asks_permission = asks_permission
        self.turn = 0

    def get_model_name(self) -> str:
        return "fixture"

    async def stream(
        self,
        messages: Any,
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ):
        self.turn += 1
        yield MessageStartEvent(message_id="fixture-message-{}".format(self.turn))
        if self.asks_permission and self.turn == 1:
            yield TextDeltaEvent(text="candidate A requests one protected write")
            yield ToolUseStartEvent(tool_use_id="fixture-tool-a", name="fixture_write")
            yield ToolUseEndEvent(
                tool_use_id="fixture-tool-a",
                name="fixture_write",
                input={"value": "candidate-a"},
            )
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            return
        text = "candidate A continued after denial" if self.asks_permission else "candidate B completed naturally"
        yield TextDeltaEvent(text=text)
        yield MessageEndEvent(stop_reason="end_turn", usage=Usage())


class _RecordingBackupService:
    """Use the production backup hook while recording its exact policy reasons."""

    def __init__(self, storage: SessionStorage) -> None:
        self._initializer = SessionBackupService(session_storage=storage)
        self.calls: list[dict[str, Any]] = []
        self.current_publication: dict[str, Any] | None = None

    def initialize_session(self, cwd: str, session_id: str) -> None:
        self._initializer.initialize_session(cwd, session_id)

    def backup_session(
        self,
        cwd: str,
        session_id: str,
        *,
        reason: BackupReason,
        critical: bool,
        **_kwargs: Any,
    ) -> BackupResult:
        publication = self.current_publication or {}
        permission = publication.get("permission")
        self.calls.append(
            {
                "reason": reason,
                "critical": critical,
                "eventType": publication.get("eventType"),
                "scope": publication.get("scope"),
                "toolUseId": permission.get("toolUseId") if isinstance(permission, dict) else None,
            }
        )
        return BackupResult(enabled=True, shared_committed=True)


class _CandidateStepExecutor:
    """Controlled StepExecutor seam; each step itself is a real AgentLoop."""

    def __init__(
        self,
        *,
        cwd: Path,
        run_dir: Path,
        projects_dir: Path,
        storage: SessionStorage,
        permission_visible: asyncio.Event,
        denied_results: list[ToolResultEvent],
        final_text: dict[int, str],
        protected_tool: _AskWriteTool,
    ) -> None:
        self._cwd = cwd
        self._run_dir = run_dir
        self._projects_dir = projects_dir
        self._storage = storage
        self._permission_visible = permission_visible
        self._denied_results = denied_results
        self._final_text = final_text
        self._protected_tool = protected_tool
        self.current_agent_loop: AgentLoop | None = None

    def set_telemetry_scope(self, **_kwargs: Any) -> None:
        return None

    def set_telemetry_correlation(self, **_kwargs: Any) -> None:
        return None

    async def execute(self, step: Any, context: Any, session_id: str, **_kwargs: Any):
        candidate = context.get_conclusion("candidate")
        if not isinstance(candidate, dict):
            raise AssertionError("Sub Pipeline candidate was not injected into its context")
        index = int(candidate["fixture_index"])
        asks_permission = bool(candidate["asks_permission"])
        if not asks_permission:
            await self._permission_visible.wait()

        tool_registry = ToolRegistry()
        candidate_tool = self._protected_tool if asks_permission else _AskWriteTool()
        tool_registry.register(candidate_tool)
        candidate_session_id = "{}-candidate-{}".format(session_id, index)
        candidate_session_dir = self._storage.ensure_v2_session_dir_for_new_session(
            str(self._cwd),
            candidate_session_id,
        )
        loop = AgentLoop(
            provider_manager=_CandidateProvider(asks_permission=asks_permission),
            system_prompt="controlled candidate fixture",
            tool_registry=tool_registry,
            max_turns=3,
            session_storage=self._storage,
            session_usage_store=SessionUsageStore(projects_dir=self._projects_dir),
            session_id=candidate_session_id,
            cwd=str(self._cwd),
            pipeline_mode=True,
            result_storage_dir=self._run_dir / "tool-results" / str(index),
            audit_log_path=candidate_session_dir / "permission-audit.jsonl",
        )
        self.current_agent_loop = loop
        text_parts: list[str] = []
        async for event in loop.run_streaming("evaluate candidate {}".format(candidate["name"])):
            if isinstance(event, TextDeltaEvent):
                text_parts.append(event.text)
            if isinstance(event, ToolResultEvent) and event.tool_use_id == "fixture-tool-a":
                self._denied_results.append(event)
            if isinstance(event, PermissionRequestEvent):
                self._permission_visible.set()
            yield event
        text = "".join(text_parts)
        self._final_text[index] = text
        conclusion = {"candidateIndex": index, "summary": text}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)


class _ParentStepExecutor:
    """Deterministic parent steps around the real parallel PipelineRunner step."""

    def __init__(self) -> None:
        self.selection_inputs: list[dict[str, Any]] = []

    async def execute(self, step: Any, context: Any, session_id: str, user_message: Any = None, **_kwargs: Any):
        if step.step_id == "architecture":
            conclusion = {
                "candidates": [
                    {"name": "Plan A", "fixture_index": 0, "asks_permission": True},
                    {"name": "Plan B", "fixture_index": 1, "asks_permission": False},
                ]
            }
        elif step.step_id == "confirm_and_select":
            evaluated = context.get_conclusion("evaluated")
            if not isinstance(evaluated, list) or len(evaluated) != 2:
                raise AssertionError("Parent selection did not receive both evaluated candidates")
            self.selection_inputs = [dict(item) for item in evaluated if isinstance(item, dict)]
            options = [
                {"id": "candidate-{}".format(index), "name": item["candidate"]["name"]}
                for index, item in enumerate(self.selection_inputs)
            ]
            conclusion = {"user_prompt": "Choose a candidate", "options": options}
            if user_message is not None:
                conclusion.update({"selected_candidate_index": 1, "selected_candidate_name": "Plan B"})
        else:
            raise AssertionError("Unexpected controlled parent step: {}".format(step.step_id))
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)


def _write_pipeline_fixture(path: Path) -> None:
    (path / "prompts").mkdir(parents=True)
    for name in ("architecture", "evaluate", "select"):
        (path / "prompts" / "{}.md".format(name)).write_text(name, encoding="utf-8")
    (path / "pipeline.yaml").write_text(
        """name: permission-timeout-fixture
context_dependencies:
  architecture: []
  evaluated: [architecture]
  selection: [evaluated]
max_rollbacks: 1
sub_pipelines:
  evaluate_candidate:
    max_rollbacks: 1
    iterate_over: architecture.candidates
    context_fields_from_parent: []
    steps:
      - id: evaluate
        conclusion_field: evaluation
        forward: null
        prompt: prompts/evaluate.md
steps:
  - id: architecture
    conclusion_field: architecture
    forward: evaluate_candidates
    prompt: prompts/architecture.md
  - id: evaluate_candidates
    type: parallel_sub_pipeline
    sub_pipeline: evaluate_candidate
    conclusion_field: evaluated
    forward: confirm_and_select
    prompt: prompts/evaluate.md
  - id: confirm_and_select
    conclusion_field: selection
    forward: null
    prompt: prompts/select.md
    auto_advance: false
    ui_mode: candidate_selection
""",
        encoding="utf-8",
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def run_scenario(*, run_dir: Path, timeout_seconds: float) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    cwd = run_dir / "workspace"
    cwd.mkdir()
    (run_dir / "audit").mkdir()
    config_dir = run_dir / "config"
    projects_dir = config_dir / "projects"
    pipeline_dir = run_dir / "pipeline-fixture"
    _write_pipeline_fixture(pipeline_dir)

    previous_config_dir = os.environ.get("IAC_CODE_CONFIG_DIR")
    os.environ["IAC_CODE_CONFIG_DIR"] = str(config_dir)
    original_step_factory = SubPipelineExecutor._make_step_executor
    try:
        storage = SessionStorage(projects_dir=projects_dir)
        backup_service = _RecordingBackupService(storage)
        registry = PermissionInputRegistry()
        policy = PermissionWaitPolicy(sub_pipeline_timeout_seconds=timeout_seconds)
        registry.set_permission_wait_coordinator(PermissionWaitCoordinator(policy))
        task_store = A2ATaskStore(backup_service=backup_service)
        context = await task_store.get_or_create_context(
            context_id="ctx-1",
            cwd=str(cwd),
            runtime_factory=lambda _session_id: object(),
        )
        session_id = context.session_id
        task = await task_store.get_or_create_task(task_id="task-1", context_id="ctx-1")
        task.state = "working"
        context.active_task_id = task.task_id
        task_store.mirror_task(task)
        task_store.mirror_context(context)

        permission_visible = asyncio.Event()
        denied_results: list[ToolResultEvent] = []
        final_text: dict[int, str] = {}
        protected_tool = _AskWriteTool()

        def make_candidate_step_executor(_self: SubPipelineExecutor) -> _CandidateStepExecutor:
            return _CandidateStepExecutor(
                cwd=cwd,
                run_dir=run_dir,
                projects_dir=projects_dir,
                storage=storage,
                permission_visible=permission_visible,
                denied_results=denied_results,
                final_text=final_text,
                protected_tool=protected_tool,
            )

        SubPipelineExecutor._make_step_executor = make_candidate_step_executor

        runner = PipelineRunner(
            pipeline_dir=pipeline_dir,
            provider_manager=object(),
            base_tool_registry=ToolRegistry(),
            session_storage=storage,
            session_id=session_id,
            cwd=str(cwd),
            surface="a2a",
            backup_service=backup_service,
        )
        parent_executor = _ParentStepExecutor()
        runner._step_executor.execute = parent_executor.execute

        executor = IacCodeA2APipelineExecutor(
            task_store=task_store,
            model="fixture",
            metrics=NoOpA2AMetrics(),
            artifact_store=None,
            push_notifier=None,
            permission_resolver=None,
            permission_input_registry=registry,
            auto_approve_permissions=False,
            thinking_exposure_types=None,
            backup_service=backup_service,
        )
        queue = _Queue()
        publisher = executor._publisher(
            event_queue=queue,
            pipeline=runner,
            task_id=task.task_id,
            context_id=context.context_id,
            session_id=session_id,
            cwd=str(cwd),
        )
        executor._install_backup_hook(
            publisher,
            pipeline=runner,
            cwd=str(cwd),
            session_id=session_id,
            task=task,
            ctx=context,
        )
        production_before_enqueue = publisher.before_enqueue

        async def record_publication_before_enqueue(envelope: dict[str, Any]) -> bool:
            backup_service.current_publication = envelope
            try:
                if production_before_enqueue is None:
                    return True
                result = production_before_enqueue(envelope)
                if asyncio.iscoroutine(result):
                    result = await result
                return result is not False
            finally:
                backup_service.current_publication = None

        publisher.before_enqueue = record_publication_before_enqueue

        task_while_candidates_finished = None
        async for event in runner.run("evaluate both candidates and select one"):
            await publisher.publish(event)
            if (
                isinstance(event, PipelineEvent)
                and event.type == PipelineEventType.STEP_COMPLETED
                and event.step_id == "evaluate_candidates"
            ):
                task_while_candidates_finished = await task_store.get_task_record(task.task_id)

        evaluated = runner.context.get_conclusion("evaluated")
        async for event in runner.resume(json.dumps({"selected_candidate_index": 1})):
            await publisher.publish(event)

        events = publisher.journal.read_all_repairing_tail()
        event_types = [str(event.get("eventType")) for event in events]
        b_completed = next(
            index
            for index, event in enumerate(events)
            if event.get("eventType") == "candidate_completed" and event.get("candidate", {}).get("index") == 1
        )
        permission_timeout = next(
            index
            for index, event in enumerate(events)
            if event.get("eventType") == "permission_resolved" and event.get("permission", {}).get("timedOut") is True
        )
        checkpoint_store = PermissionWaitCheckpointStore(str(cwd), session_id, storage=storage)
        permission_backup_calls = [
            call
            for call in backup_service.calls
            if call["eventType"] == "permission_requested" or call["toolUseId"] == "fixture-tool-a"
        ]
        selection = runner.context.get_conclusion("selection")
        checks = {
            "candidate A entered real AgentLoop permission wait": "permission_requested" in event_types,
            "candidate B completed before A hard timeout": b_completed < permission_timeout,
            "hard timeout delivered exactly one denied ToolResult": len(denied_results) == 1
            and denied_results[0].is_error
            and denied_results[0].result == "Permission denied.",
            "denied tool did not execute": protected_tool.execution_count == 0,
            "candidate A AgentLoop continued after denial": "candidate A continued after denial"
            in final_text.get(0, ""),
            "real parent Pipeline consumed both candidate conclusions": isinstance(evaluated, list)
            and len(evaluated) == 2
            and len(parent_executor.selection_inputs) == 2
            and all(not item.get("failed", True) for item in parent_executor.selection_inputs),
            "parent remained working through candidate completion": task_while_candidates_finished is not None
            and task_while_candidates_finished.state == "working",
            "parent naturally reached candidate selection and completed": "input_required" in event_types
            and "input_received" in event_types
            and "pipeline_completed" in event_types
            and isinstance(selection, dict)
            and selection.get("selected_candidate_index") == 1,
            "no grace state or durable checkpoint": checkpoint_store.list_active() == []
            and not list(run_dir.rglob("permission-waits/pwb_*.json")),
            "production backup hook excluded Sub permission checkpoint": permission_backup_calls == []
            and any(call["eventType"] == "input_required" for call in backup_service.calls),
        }
        result = {
            "schemaVersion": 1,
            "timeoutSeconds": timeout_seconds,
            "backupCalls": [
                {
                    **call,
                    "reason": call["reason"].value,
                }
                for call in backup_service.calls
            ],
            "checks": checks,
            "passed": all(checks.values()),
        }
        _write_json(run_dir / "result.json", result)
        return result
    finally:
        SubPipelineExecutor._make_step_executor = original_step_factory
        if previous_config_dir is None:
            os.environ.pop("IAC_CODE_CONFIG_DIR", None)
        else:
            os.environ["IAC_CODE_CONFIG_DIR"] = previous_config_dir


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = asyncio.run(run_scenario(run_dir=args.run_dir, timeout_seconds=args.timeout_seconds))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
