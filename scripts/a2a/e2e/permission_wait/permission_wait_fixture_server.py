#!/usr/bin/env python3
"""Deterministic A2A server fixture for permission-wait restart E2E tests."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--persistence-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--execution-log", type=Path, required=True)
    parser.add_argument("--mode", choices=("normal", "pipeline"), default="normal")
    parser.add_argument("--resident-timeout-seconds", type=float)
    parser.add_argument("--sub-pipeline-timeout-seconds", type=float)
    parser.add_argument("--timeout-grace-seconds", type=float, default=30.0)
    parser.add_argument("--candidate-first", action="store_true")
    return parser.parse_args()


def _create_fixture_runtime(options: Any, *, execution_log: Path) -> Any:
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.providers.base import ToolDefinition
    from iac_code.services.agent_factory import AgentRuntime
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
    from iac_code.types.permissions import PermissionResult
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        TextDeltaEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )

    class FixtureWriteTool(Tool):
        @property
        def name(self) -> str:
            return "fixture_write"

        @property
        def description(self) -> str:
            return "Record one deterministic pre-authorized write."

        @property
        def input_schema(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }

        async def check_permissions(
            self,
            input: dict[str, Any],  # noqa: A002 - Tool protocol name
            context: dict[str, Any] | None = None,
        ) -> PermissionResult:
            del input, context
            return PermissionResult(behavior="ask", message="Allow deterministic fixture write?")

        async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
            del context
            execution_log.parent.mkdir(parents=True, exist_ok=True)
            with execution_log.open("a", encoding="utf-8") as handle:
                handle.write(str(tool_input["value"]) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return ToolResult.success("fixture write completed")

    class FixtureProvider:
        def get_model_name(self) -> str:
            return "permission-wait-fixture"

        async def stream(
            self,
            messages: list[Any],
            system: str,
            tools: list[ToolDefinition] | None = None,
            max_tokens: int = 8192,
        ):
            del system, tools, max_tokens
            has_tool_result = any(
                getattr(block, "type", None) == "tool_result"
                for message in messages
                for block in (message.content if isinstance(message.content, list) else [])
            )
            if has_tool_result:
                yield MessageStartEvent(message_id="fixture-final")
                yield TextDeltaEvent(text="fixture recovery completed")
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())
                return

            yield MessageStartEvent(message_id="fixture-permission")
            yield TextDeltaEvent(text="fixture permission required")
            yield ToolUseStartEvent(tool_use_id="fixture-tool-1", name="fixture_write")
            yield ToolUseEndEvent(
                tool_use_id="fixture-tool-1",
                name="fixture_write",
                input={"value": "executed"},
            )
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    provider = FixtureProvider()
    registry = ToolRegistry()
    registry.register(FixtureWriteTool())
    storage = SessionStorage()
    storage.ensure_v2_session_dir_for_new_session(str(options.cwd), str(options.session_id))
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="Deterministic permission-wait fixture.",
        tool_registry=registry,
        max_turns=3,
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


def _create_fixture_pipeline(*, execution_log: Path, candidate_first: bool = False, **kwargs: Any) -> Any:
    import asyncio
    import time
    from types import SimpleNamespace

    from iac_code.agent.message import Message, ToolUseBlock
    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
    from iac_code.services.permission_wait import canonical_digest
    from iac_code.services.session_storage import SessionStorage
    from iac_code.types.permissions import PermissionAuditMetadata, PermissionAuditSettings, PermissionResult
    from iac_code.types.stream_events import (
        PermissionRequestEvent,
        PermissionWaitOutcome,
        PermissionWaitSuspended,
        TextDeltaEvent,
    )

    session_id = str(kwargs["session_id"])
    cwd = str(kwargs["cwd"])
    storage = kwargs.get("session_storage") or SessionStorage()
    root_session_dir = storage.ensure_v2_session_dir_for_new_session(cwd, session_id)
    if root_session_dir is None:
        root_session_dir = storage.session_dir(cwd, session_id)
    transcript_id = "transcript_att_0001"
    transcript_storage = PipelineTranscriptStorage(root_session_dir / "pipeline")

    class FixturePipeline:
        pipeline_name = "selling"
        emit_stack_events = False
        handoff_enabled = False

        def __init__(self) -> None:
            self.session = SimpleNamespace(session_dir=root_session_dir / "pipeline")
            self.session.session_dir.mkdir(parents=True, exist_ok=True)
            self.sidecar_status = None
            self.sidecar_restore_result = None
            self._loaded = SimpleNamespace(
                steps=[SimpleNamespace(step_id="fixture_step", step_type="agent", ui_mode="default")],
                sub_pipelines={},
            )

        async def run(self, prompt: str):
            del prompt
            if candidate_first:
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_STARTED,
                    step_id=None,
                    timestamp=time.time(),
                    data={"total_steps": 2, "step_names": ["confirm_and_select", "fixture_step"]},
                )
                self.sidecar_status = "waiting_input"
                yield PipelineEvent(
                    type=PipelineEventType.USER_INPUT_REQUIRED,
                    step_id="confirm_and_select",
                    timestamp=time.time(),
                    data={
                        "kind": "candidate_selection",
                        "prompt": "Choose the fixture candidate",
                        "options": [{"id": "0", "label": "Fixture candidate", "candidate_index": 0}],
                    },
                )
                return
            async for event in self._permission_stream(include_start=True):
                yield event

        async def _permission_stream(self, *, include_start: bool):
            if include_start:
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_STARTED,
                    step_id=None,
                    timestamp=time.time(),
                    data={"total_steps": 1, "step_names": ["fixture_step"]},
                )
            yield PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="fixture_step",
                timestamp=time.time(),
                data={"step_index": 0, "total_steps": 1},
            )
            assistant = Message(
                role="assistant",
                content=[ToolUseBlock(id="fixture-pipeline-tool-1", name="fixture_write", input={"value": "executed"})],
            )
            transcript_storage.append(cwd, transcript_id, assistant)
            digest = canonical_digest([block.model_dump(mode="json") for block in assistant.content])
            response_future = asyncio.get_running_loop().create_future()
            permission = PermissionRequestEvent(
                tool_name="fixture_write",
                tool_input={"value": "executed"},
                tool_use_id="fixture-pipeline-tool-1",
                response_future=response_future,
                continuation_frame={
                    "assistantMessageRef": "session.jsonl:0",
                    "assistantMessageDigest": digest,
                    "orderedToolUseIds": ["fixture-pipeline-tool-1"],
                    "currentIndex": 0,
                    "decisions": [
                        {
                            "toolUseId": "fixture-pipeline-tool-1",
                            "state": "pending",
                            "source": None,
                            "deniedResult": None,
                        }
                    ],
                },
                audit_context={
                    "session_id": transcript_id,
                    "cwd": cwd,
                    "root_session_id": session_id,
                    "transcript_id": transcript_id,
                },
            )
            yield permission
            outcome = await asyncio.shield(response_future)
            if outcome is PermissionWaitOutcome.SUSPEND:
                raise PermissionWaitSuspended(permission.boundary_id)
            allowed = bool(outcome)
            if allowed:
                self._record_execution()
            async for event in self._finish_stream():
                yield event

        async def resume(self, prompt: str):
            if candidate_first and self.sidecar_status == "waiting_input":
                self.sidecar_status = "running"
                async for event in self._permission_stream(include_start=False):
                    yield event
                return
            async for event in self.run(prompt):
                yield event

        async def resume_permission_boundary(self, checkpoint: dict[str, Any]):
            decision = checkpoint.get("decision")
            if not isinstance(decision, dict):
                raise ValueError("permission_resume_invalid: fixture decision is missing")
            if decision.get("value") == "allow_once":
                self._record_execution()
            elif decision.get("value") != "deny":
                raise ValueError("permission_resume_invalid: fixture decision is invalid")
            yield TextDeltaEvent(text="fixture pipeline recovery completed")
            async for event in self._finish_stream():
                yield event

        async def rebuild_permission_audit_event(self, checkpoint: dict[str, Any], recovered: Any):
            if recovered.audit_context.get("transcript_id") != transcript_id:
                raise ValueError("permission_resume_invalid: fixture transcript changed")
            if checkpoint.get("toolUseId") != recovered.tool_use_id:
                raise ValueError("permission_resume_invalid: fixture tool changed")
            metadata = PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                reason_type="prompt_required",
                reason_detail="fixture prompt",
                is_read_only=False,
                operation={"fixture": "write"},
            )
            return PermissionRequestEvent(
                tool_name=recovered.tool_name,
                tool_input=recovered.tool_input,
                tool_use_id=recovered.tool_use_id,
                permission_result=PermissionResult(behavior="ask", audit=metadata),
                audit_context={
                    **recovered.audit_context,
                    "metadata": metadata,
                    "settings": PermissionAuditSettings(),
                },
            )

        async def _finish_stream(self):
            yield PipelineEvent(
                type=PipelineEventType.STEP_COMPLETED,
                step_id="fixture_step",
                timestamp=time.time(),
                data={"conclusion": {"status": "success"}},
            )
            self.sidecar_status = "completed"
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=time.time(),
                data={"total_steps": 1},
            )

        def _record_execution(self) -> None:
            execution_log.parent.mkdir(parents=True, exist_ok=True)
            with execution_log.open("a", encoding="utf-8") as handle:
                handle.write("executed\n")
                handle.flush()
                os.fsync(handle.fileno())

        def continue_from_sidecar(self, user_input: str | None = None):
            if candidate_first:
                self.sidecar_status = "running"
                return self._permission_stream(include_start=False)
            return self.run(user_input or "")

        def should_switch_to_normal(self, data: dict[str, Any]) -> bool:
            del data
            return False

        async def pause_agent_loops(self) -> None:
            return None

        async def resume_agent_loops(self) -> None:
            return None

        def clear_sidecar(self) -> None:
            self.sidecar_status = None

    return FixturePipeline()


def main() -> int:
    args = _parse_args()
    config_dir = args.config_dir.expanduser().resolve()
    persistence_dir = args.persistence_dir.expanduser().resolve()
    artifact_dir = args.artifact_dir.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve()
    execution_log = args.execution_log.expanduser().resolve()
    for path in (config_dir, persistence_dir, artifact_dir, workspace, execution_log.parent):
        path.mkdir(parents=True, exist_ok=True)

    os.environ["IAC_CODE_CONFIG_DIR"] = str(config_dir)
    os.environ["IAC_CODE_MODE"] = args.mode
    os.environ["IACCODE_A2A_ALLOWED_CWDS"] = str(workspace)

    import uvicorn

    from iac_code.a2a import executor as executor_module
    from iac_code.a2a import pipeline_executor as pipeline_executor_module
    from iac_code.a2a.app import create_app

    executor_module.create_agent_runtime = lambda options: _create_fixture_runtime(
        options,
        execution_log=execution_log,
    )
    pipeline_executor_module.create_agent_runtime = executor_module.create_agent_runtime
    fixture_pipelines: dict[str, Any] = {}

    def create_fixture_pipeline(*unused_args: Any, **kwargs: Any) -> Any:
        session_id = str(kwargs["session_id"])
        pipeline = fixture_pipelines.get(session_id)
        if pipeline is None:
            pipeline = _create_fixture_pipeline(
                execution_log=execution_log,
                candidate_first=args.candidate_first,
                **kwargs,
            )
            fixture_pipelines[session_id] = pipeline
        return pipeline

    pipeline_executor_module.create_pipeline = create_fixture_pipeline
    app = create_app(
        host=args.host,
        port=args.port,
        token=None,
        model="permission-wait-fixture",
        persistence_dir=persistence_dir,
        artifact_dir=artifact_dir,
        auto_approve_permissions=False,
        permission_wait={
            "resident_timeout_seconds": args.resident_timeout_seconds,
            "sub_pipeline_timeout_seconds": args.sub_pipeline_timeout_seconds,
            "timeout_grace_seconds": args.timeout_grace_seconds,
        },
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
