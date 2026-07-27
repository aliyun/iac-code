#!/usr/bin/env python3
"""Deterministic Web REPL smoke server.

This server hosts the real Web app with fake runtime adapters. It is intended
for browser E2E smoke checks and never calls real LLMs, local shells, cloud
APIs, or A2A executors.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

FAKE_SECRET_STRINGS = ("sk-test-secret", "ALIYUN_SECRET", "SECRET_ACCESS_KEY")


class FakePipelineStateService:
    """In-memory pipeline recovery service used by ``/api/pipeline/state``."""

    def __init__(self) -> None:
        self._by_context: dict[str, dict[str, Any]] = {}
        self._by_task: dict[str, dict[str, Any]] = {}

    def save(self, snapshot: dict[str, Any]) -> None:
        context_id = str(snapshot.get("contextId") or "")
        task_id = str(snapshot.get("taskId") or "")
        payload = {"snapshot": snapshot, "events": list(snapshot.get("events") or [])}
        if context_id:
            self._by_context[context_id] = payload
        if task_id:
            self._by_task[task_id] = payload

    async def get_state(
        self,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        after_sequence: int | None = None,
    ) -> dict[str, Any]:
        del after_sequence
        if context_id and context_id in self._by_context:
            return self._by_context[context_id]
        if task_id and task_id in self._by_task:
            return self._by_task[task_id]
        raise ValueError("pipeline state not found")


def _stable_pipeline_ids(session: Any) -> tuple[str, str]:
    suffix = str(session.session_id).replace("_", "-")[:12] or uuid.uuid4().hex[:12]
    return f"ctx-smoke-{suffix}", f"task-smoke-{suffix}"


def _candidate_details() -> list[dict[str, Any]]:
    return [
        {
            "candidateName": "Smoke balanced VPC",
            "candidateIndex": 0,
            "summary": "Two-zone VPC with NAT gateway and ECS jump host.",
            "totalMonthlyCost": 128.5,
            "costItems": [
                {"resourceType": "ALIYUN::ECS::Instance", "name": "ecs.g7.large"},
                {"resourceType": "ALIYUN::VPC::NatGateway", "name": "nat.small"},
            ],
        },
        {
            "candidateName": "Smoke minimal VPC",
            "candidateIndex": 1,
            "summary": "Single-zone VPC with lower baseline cost.",
            "totalMonthlyCost": 48.0,
            "costItems": [
                {"resourceType": "ALIYUN::VPC::VSwitch", "name": "vsw-smoke"},
            ],
        },
    ]


def _diagrams() -> list[dict[str, Any]]:
    return [
        {
            "diagramId": "diagram-smoke-balanced",
            "candidateName": "Smoke balanced VPC",
            "candidateIndex": 0,
            "mermaidSource": "graph TD\n  Internet --> NAT\n  NAT --> VPC\n  VPC --> ECS",
        },
        {
            "diagramId": "diagram-smoke-minimal",
            "candidateName": "Smoke minimal VPC",
            "candidateIndex": 1,
            "mermaidSource": "graph TD\n  VPC --> VSwitch",
        },
    ]


def _base_snapshot(session: Any) -> dict[str, Any]:
    return {
        "contextId": session.context_id,
        "taskId": session.task_id,
        "pipelineName": session.pipeline_name or "selling",
        "status": "input-required",
        "lastSequence": 1,
        "steps": [
            {
                "id": "requirements",
                "title": "Collect requirements",
                "status": "completed",
            },
            {
                "id": "candidate-selection",
                "title": "Select candidate",
                "status": "waiting_input",
                "candidates": [
                    {
                        "candidateName": "Smoke balanced VPC",
                        "candidateIndex": 0,
                        "status": "recommended",
                        "summary": "Balanced cost and availability.",
                        "totalMonthlyCost": 128.5,
                    },
                    {
                        "candidateName": "Smoke minimal VPC",
                        "candidateIndex": 1,
                        "status": "available",
                        "summary": "Lowest cost option.",
                        "totalMonthlyCost": 48.0,
                    },
                ],
            },
            {
                "id": "deploy",
                "title": "Deploy selected stack",
                "status": "pending",
            },
        ],
        "pendingInput": {
            "kind": "candidate_selection",
            "message": "Choose the selling candidate to deploy.",
            "required": True,
        },
        "display": {
            "pipelineName": "selling",
            "messages": [
                {
                    "kind": "pipeline.started",
                    "title": "Selling pipeline started",
                    "status": "input-required",
                }
            ],
            "candidateDetails": _candidate_details(),
            "diagrams": _diagrams(),
            "artifacts": [{"name": "ros-template-smoke.yaml", "kind": "template"}],
            "toolResults": [{"toolUseId": "pipeline-smoke-plan", "summary": "candidate plan generated"}],
        },
        "control": {
            "inputHistory": [{"kind": "user.message", "summary": "Provision a smoke-test VPC"}],
            "warningHistory": [],
            "rollbackHistory": [],
            "candidateRestarts": [],
        },
        "events": [
            {
                "kind": "pipeline.started",
                "title": "Selling pipeline started",
                "status": "input-required",
            }
        ],
    }


def _selected_snapshot(
    session: Any,
    *,
    candidate_name: str,
    candidate_index: int | None,
    parameter_overrides: dict[str, Any],
) -> dict[str, Any]:
    snapshot = _base_snapshot(session)
    snapshot.update(
        {
            "status": "completed",
            "lastSequence": 7,
            "pendingInput": None,
            "steps": [
                {
                    "id": "requirements",
                    "title": "Collect requirements",
                    "status": "completed",
                },
                {
                    "id": "candidate-selection",
                    "title": "Select candidate",
                    "status": "completed",
                    "candidates": [
                        {
                            "candidateName": "Smoke balanced VPC",
                            "candidateIndex": 0,
                            "status": "selected" if candidate_index == 0 else "available",
                            "summary": "Balanced cost and availability.",
                            "totalMonthlyCost": 128.5,
                        },
                        {
                            "candidateName": "Smoke minimal VPC",
                            "candidateIndex": 1,
                            "status": "selected" if candidate_index == 1 else "available",
                            "summary": "Lowest cost option.",
                            "totalMonthlyCost": 48.0,
                        },
                    ],
                },
                {
                    "id": "deploy",
                    "title": "Deploy selected stack",
                    "status": "completed",
                },
                {
                    "id": "cleanup",
                    "title": "Cleanup temporary resources",
                    "status": "completed",
                },
                {
                    "id": "handoff",
                    "title": "Return to normal mode",
                    "status": "completed",
                },
            ],
            "stacks": {
                "current": {
                    "kind": "stack.progress",
                    "eventId": "stack-smoke-complete",
                    "stackId": "stack-smoke-selling",
                    "stackName": "iac-code-smoke-selling",
                    "regionId": "cn-hangzhou",
                    "stackStatus": "CREATE_COMPLETE",
                    "progressPercentage": 100,
                    "deploymentSucceeded": True,
                    "deploymentComplete": True,
                }
            },
            "cleanup": {
                "status": "completed",
                "resourceCount": 1,
                "blocksNormalChat": False,
                "message": "cleanup completed",
                "resources": [
                    {
                        "resourceId": "stack-smoke-selling",
                        "resourceType": "ALIYUN::ROS::Stack",
                        "regionId": "cn-hangzhou",
                        "cleanupStatus": "DELETE_COMPLETE",
                    }
                ],
            },
            "normalHandoff": {
                "targetMode": "normal",
                "targetNormalMode": "normal",
                "outcome": "ready",
                "summary": "handoff normal ready",
            },
        }
    )
    snapshot["control"].update(
        {
            "selectedCandidate": {
                "candidateName": candidate_name,
                "candidateIndex": candidate_index,
                "parameterOverrides": parameter_overrides,
            },
            "handoff": snapshot["normalHandoff"],
        }
    )
    snapshot["display"]["messages"] = [
        {"kind": "candidate.selected", "title": candidate_name, "status": "selected"},
        {"kind": "stack.progress", "title": "iac-code-smoke-selling", "status": "CREATE_COMPLETE"},
        {"kind": "cleanup.completed", "title": "cleanup completed", "status": "completed"},
        {"kind": "pipeline_handoff_ready", "title": "handoff normal ready", "status": "ready"},
    ]
    snapshot["events"] = list(snapshot["display"]["messages"])
    return snapshot


def _rollback_snapshot(session: Any, *, message: str) -> dict[str, Any]:
    snapshot = _base_snapshot(session)
    snapshot.update(
        {
            "status": "rollback-required",
            "lastSequence": 11,
            "pendingInput": {
                "kind": "rollback_review",
                "message": "Review rollback cleanup and restart from template generation.",
                "required": True,
            },
            "steps": [
                {
                    "id": "requirements",
                    "title": "Collect requirements",
                    "status": "completed",
                },
                {
                    "id": "candidate-selection",
                    "title": "Select candidate",
                    "status": "completed",
                    "candidates": [
                        {
                            "candidateName": "Smoke balanced VPC",
                            "candidateIndex": 0,
                            "status": "selected",
                            "summary": "Balanced cost and availability.",
                            "totalMonthlyCost": 128.5,
                        }
                    ],
                },
                {
                    "id": "template-generating",
                    "title": "Regenerate template after rollback",
                    "status": "restarting",
                },
                {
                    "id": "deploy",
                    "title": "Deploy selected stack",
                    "status": "failed",
                },
                {
                    "id": "cleanup",
                    "title": "Cleanup rollback resources",
                    "status": "working",
                },
            ],
            "stacks": {
                "current": {
                    "kind": "stack.progress",
                    "eventId": "stack-smoke-rollback",
                    "stackId": "stack-smoke-rollback",
                    "stackName": "iac-code-smoke-rollback",
                    "regionId": "cn-hangzhou",
                    "stackStatus": "ROLLBACK_IN_PROGRESS",
                    "progressPercentage": 62,
                    "deploymentSucceeded": False,
                    "deploymentComplete": False,
                }
            },
            "cleanup": {
                "status": "in_progress",
                "resourceCount": 2,
                "blocksNormalChat": True,
                "message": "Rollback cleanup is still blocking normal handoff.",
                "resources": [
                    {
                        "resourceId": "stack-smoke-rollback",
                        "resourceType": "ALIYUN::ROS::Stack",
                        "regionId": "cn-hangzhou",
                        "cleanupStatus": "DELETE_IN_PROGRESS",
                    },
                    {
                        "resourceId": "sg-smoke-leftover",
                        "resourceType": "ALIYUN::ECS::SecurityGroup",
                        "regionId": "cn-hangzhou",
                        "cleanupStatus": "DELETE_FAILED",
                    },
                ],
                "errors": [
                    {
                        "resourceId": "sg-smoke-leftover",
                        "message": "DependencyViolation: detach ECS network interface before retry.",
                    }
                ],
            },
            "normalHandoff": {
                "targetMode": "pipeline",
                "outcome": "blocked",
                "summary": "handoff blocked until rollback cleanup completes",
            },
        }
    )
    snapshot["control"].update(
        {
            "inputHistory": [{"kind": "user.message", "summary": message}],
            "warningHistory": [
                {
                    "kind": "pipeline.warning",
                    "summary": "Deploy failed; rollback cleanup is required before normal mode.",
                }
            ],
            "rollbackHistory": [
                {
                    "from": "deploy",
                    "to": "template-generating",
                    "reason": "ROS stack entered ROLLBACK_IN_PROGRESS during smoke audit.",
                }
            ],
            "candidateRestarts": [
                {
                    "candidateName": "Smoke balanced VPC",
                    "fromStep": "deploy",
                    "restartStep": "template-generating",
                    "status": "queued",
                }
            ],
        }
    )
    snapshot["display"]["messages"] = [
        {"kind": "stack.progress", "title": "iac-code-smoke-rollback", "status": "ROLLBACK_IN_PROGRESS"},
        {"kind": "pipeline.rollback.triggered", "title": "Rollback to template generation", "status": "rollback"},
        {"kind": "cleanup.failed", "title": "Security group cleanup failed", "status": "failed"},
        {
            "kind": "pipeline_handoff_ready",
            "title": "handoff blocked until rollback cleanup completes",
            "status": "blocked",
        },
    ]
    snapshot["events"] = list(snapshot["display"]["messages"])
    return snapshot


class FakeWebRuntime:
    """Normal-mode fake runtime with optional blocking UI exercise."""

    def __init__(self, session: Any, manager: Any) -> None:
        self.session = session
        self.manager = manager

    async def start_turn(self, request: Any) -> dict[str, Any]:
        from iac_code.agent.message import Message

        turn_id = request.turn_id or f"turn-{uuid.uuid4().hex}"
        message_id = f"assistant-{turn_id}"
        request_text = str(request.text or "").lower()
        assistant_parts = ["normal assistant response from fake runtime\n"]
        async with self.session.turn_lock:
            self.session.active_turn_task = asyncio.current_task()
            try:
                await self.session.events.publish(
                    "user.message",
                    {
                        "turnId": turn_id,
                        "text": request.text,
                        "imageIds": list(request.image_ids),
                        "fileRefs": list(request.file_refs),
                        "source": request.source,
                    },
                )
                await self.session.events.publish(
                    "assistant.message.start",
                    {
                        "turnId": turn_id,
                        "messageId": message_id,
                        "provider": "fake-web-smoke",
                        "model": "fake-model",
                    },
                )
                await self.session.events.publish(
                    "assistant.text.delta",
                    {
                        "turnId": turn_id,
                        "messageId": message_id,
                        "delta": assistant_parts[-1],
                    },
                )
                if "long content state" in request_text:
                    long_content = "\n".join(
                        [
                            "Long visual audit output:",
                            *[
                                (f"resource-{index:03d}: ALIYUN::ECS::Instance cn-hangzhou status=CREATE_IN_PROGRESS")
                                for index in range(1, 41)
                            ],
                        ]
                    )
                    assistant_parts.append(long_content)
                    await self.session.events.publish(
                        "assistant.text.delta",
                        {
                            "turnId": turn_id,
                            "messageId": message_id,
                            "delta": long_content,
                        },
                    )
                await self._publish_tool_events(turn_id)
                if "visual error state" in request_text:
                    await self.session.events.publish(
                        "error",
                        {
                            "turnId": turn_id,
                            "message": "fake visual error state",
                            "code": "fake_visual_error",
                        },
                    )
                if "blocking" in request_text:
                    await self._exercise_blocking_ui(turn_id, message_id)
                await self.session.events.publish(
                    "assistant.message.end",
                    {
                        "turnId": turn_id,
                        "messageId": message_id,
                        "finishReason": "stop",
                    },
                )
                await self.session.events.publish(
                    "turn.done",
                    {"turnId": turn_id, "interrupted": False, "canceled": False},
                )
                self.manager.storage.append(
                    str(self.session.cwd),
                    self.session.session_id,
                    Message(
                        role="user",
                        content=request.text,
                        metadata={"turnId": turn_id, "messageId": f"user-{turn_id}"},
                    ),
                )
                self.manager.storage.append(
                    str(self.session.cwd),
                    self.session.session_id,
                    Message(
                        role="assistant",
                        content="".join(assistant_parts),
                        metadata={"turnId": turn_id, "messageId": message_id},
                    ),
                )
            finally:
                if self.session.active_turn_task is asyncio.current_task():
                    self.session.active_turn_task = None
        return {"accepted": True, "turnId": turn_id}

    async def _publish_tool_events(self, turn_id: str) -> None:
        tool_use_id = f"fake-tool-{turn_id}"
        await self.session.events.publish(
            "tool.started",
            {
                "turnId": turn_id,
                "toolUseId": tool_use_id,
                "toolName": "fakeRosPlan",
                "status": "running",
            },
        )
        await self.session.events.publish(
            "tool.input.delta",
            {
                "turnId": turn_id,
                "toolUseId": tool_use_id,
                "delta": 'template="vpc" api_key=sk-test-secret',
            },
        )
        await self.session.events.publish(
            "tool.result",
            {
                "turnId": turn_id,
                "toolUseId": tool_use_id,
                "resultKind": "text",
                "summary": "fake tool completed",
                "artifacts": [{"name": "fake-ros-template.yaml"}],
            },
        )
        await self.session.events.publish(
            "tool.finished",
            {
                "turnId": turn_id,
                "toolUseId": tool_use_id,
                "status": "completed",
                "elapsedMs": 7,
                "summary": "fake tool completed",
            },
        )

    async def _exercise_blocking_ui(self, turn_id: str, message_id: str) -> None:
        permission_future = asyncio.get_running_loop().create_future()
        self.manager.add_permission_request(
            self.session,
            {
                "title": "Allow fake action",
                "toolName": "bash",
                "toolUseId": "shell-escape",
                "toolInput": {"command": "echo permission-smoke"},
                "message": "Allow fake action for browser smoke?",
                "suggestions": [{"toolName": "bash", "ruleContent": "echo permission-smoke"}],
                "allowAlways": True,
            },
            future=permission_future,
        )
        await asyncio.wait_for(permission_future, timeout=30)
        await self.session.events.publish(
            "assistant.text.delta",
            {
                "turnId": turn_id,
                "messageId": message_id,
                "delta": "permission answered\n",
            },
        )

        question_future = asyncio.get_running_loop().create_future()
        self.manager.add_question_request(
            self.session,
            {
                "question": "Choose deployment region",
                "options": [
                    {"id": "cn-hangzhou", "label": "Use cn-hangzhou"},
                    {"id": "cn-shanghai", "label": "Use cn-shanghai"},
                ],
                "allowFreeText": False,
            },
            future=question_future,
        )
        answer = await asyncio.wait_for(question_future, timeout=30)
        selected = answer.get("selected_id", "unknown") if isinstance(answer, dict) else "unknown"
        await self.session.events.publish(
            "assistant.text.delta",
            {
                "turnId": turn_id,
                "messageId": message_id,
                "delta": f"question answered: {selected}\n",
            },
        )


class FakeShellRunner:
    """Local shell runner that publishes shell cards without executing commands."""

    async def run(self, session: Any, command: str) -> dict[str, Any]:
        shell_use_id = f"local-shell-{uuid.uuid4().hex}"
        await session.events.publish(
            "local.shell.start",
            {
                "shellUseId": shell_use_id,
                "toolUseId": shell_use_id,
                "command": command,
                "local": True,
                "entersAgentContext": False,
            },
        )
        if "visual-fail-long-output" in command:
            payload = {
                "shellUseId": shell_use_id,
                "toolUseId": shell_use_id,
                "command": command,
                "exitCode": 17,
                "stdout": "\n".join(f"visual shell stdout line {index:03d}" for index in range(1, 26)),
                "stderr": "\n".join(f"visual shell stderr line {index:03d}" for index in range(1, 26)),
                "local": True,
                "entersAgentContext": False,
            }
            await session.events.publish("local.shell.end", payload)
            return payload

        payload = {
            "shellUseId": shell_use_id,
            "toolUseId": shell_use_id,
            "command": command,
            "exitCode": 0,
            "stdout": "fake local shell stdout\nSECRET_ACCESS_KEY=ALIYUN_SECRET",
            "stderr": "",
            "local": True,
            "entersAgentContext": False,
        }
        await session.events.publish("local.shell.end", payload)
        return payload


class FakePipelineActionRunner:
    """Selling-pipeline fake runner for candidate, deploy, cleanup, handoff smoke."""

    def __init__(self, manager: Any, state_service: FakePipelineStateService) -> None:
        self.manager = manager
        self.state_service = state_service

    def _ensure_identity(self, session: Any) -> None:
        if not session.context_id or not session.task_id:
            context_id, task_id = _stable_pipeline_ids(session)
            self.manager.attach_pipeline_identity(
                session,
                context_id=context_id,
                task_id=task_id,
                pipeline_name=session.pipeline_name or "selling",
            )

    async def start(
        self,
        session: Any,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection: Any = None,
        event_sink: Any = None,
        permission_resolver: Any = None,
    ) -> Any:
        del image_ids, file_refs, model_selection, event_sink, permission_resolver
        from iac_code.web.pipeline_actions import PipelineActionResult

        self._ensure_identity(session)
        request_text = str(message or "").lower()
        snapshot = (
            _rollback_snapshot(session, message=message)
            if "rollback" in request_text or "fail" in request_text
            else _base_snapshot(session)
        )
        self.state_service.save(snapshot)
        events = list(snapshot.get("events") or [])
        events.insert(
            0,
            {
                "kind": "pipeline.started",
                "pipelineName": "selling",
                "message": message,
            },
        )
        if snapshot.get("status") != "rollback-required":
            events.extend(
                [
                    {
                        "kind": "candidate.selection.required",
                        "message": "Choose a candidate",
                    },
                    {
                        "webEventType": "candidate.detail",
                        "candidateName": "Smoke balanced VPC",
                        "candidateIndex": 0,
                        "summary": "Two-zone VPC with NAT gateway and ECS jump host.",
                    },
                ]
            )
        events.append(
            {
                "webEventType": "pipeline.snapshot",
                "snapshot": snapshot,
                "contextId": session.context_id,
                "taskId": session.task_id,
            }
        )
        return PipelineActionResult(
            accepted=True,
            status_code=202,
            response={
                "accepted": True,
                "action": "started",
                "contextId": session.context_id,
                "taskId": session.task_id,
            },
            events=events,
        )

    async def select_candidate(
        self,
        session: Any,
        selection: Any,
        *,
        model_selection: Any = None,
        event_sink: Any = None,
        permission_resolver: Any = None,
    ) -> Any:
        del model_selection, event_sink, permission_resolver
        from iac_code.web.pipeline_actions import PipelineActionResult

        self._ensure_identity(session)
        candidate_name = selection.candidate_name or "Smoke balanced VPC"
        candidate_index = selection.candidate_index
        if candidate_index is None:
            candidate_index = 0 if candidate_name == "Smoke balanced VPC" else 1
        snapshot = _selected_snapshot(
            session,
            candidate_name=candidate_name,
            candidate_index=candidate_index,
            parameter_overrides=dict(selection.parameter_overrides),
        )
        self.state_service.save(snapshot)
        events = [
            {
                "kind": "candidate.selected",
                "candidateName": candidate_name,
                "candidateIndex": candidate_index,
                "parameterOverrides": dict(selection.parameter_overrides),
            },
            {
                "kind": "stack.progress",
                "stackId": "stack-smoke-selling",
                "stackName": "iac-code-smoke-selling",
                "regionId": "cn-hangzhou",
                "stackStatus": "CREATE_IN_PROGRESS",
                "progressPercentage": 40,
            },
            {
                "kind": "stack.progress",
                "stackId": "stack-smoke-selling",
                "stackName": "iac-code-smoke-selling",
                "regionId": "cn-hangzhou",
                "stackStatus": "CREATE_COMPLETE",
                "progressPercentage": 100,
                "deploymentSucceeded": True,
                "deploymentComplete": True,
            },
            {
                "kind": "cleanup.completed",
                "status": "completed",
                "resourceCount": 1,
                "resources": snapshot["cleanup"]["resources"],
            },
            {
                "kind": "pipeline_handoff_ready",
                "targetMode": "normal",
                "outcome": "ready",
                "summary": "handoff normal ready",
            },
            {
                "webEventType": "pipeline.snapshot",
                "snapshot": snapshot,
                "contextId": session.context_id,
                "taskId": session.task_id,
            },
        ]
        return PipelineActionResult(
            accepted=True,
            status_code=202,
            response={
                "accepted": True,
                "action": "candidate_selected",
                "contextId": session.context_id,
                "taskId": session.task_id,
            },
            events=events,
        )

    async def interrupt(
        self,
        session: Any,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection: Any = None,
        event_sink: Any = None,
        permission_resolver: Any = None,
    ) -> Any:
        del image_ids, file_refs, model_selection, event_sink, permission_resolver
        from iac_code.web.pipeline_actions import PipelineActionResult

        self._ensure_identity(session)
        return PipelineActionResult(
            accepted=True,
            status_code=202,
            response={"accepted": True, "action": "interrupt"},
            events=[
                {
                    "kind": "pipeline.interrupt.submitted",
                    "pipelineInterrupt": True,
                    "message": message,
                }
            ],
        )


def build_app(*, cwd: Path, config_dir: Path) -> Any:
    os.environ["IAC_CODE_CONFIG_DIR"] = str(config_dir)
    os.environ["IAC_CODE_CWD"] = str(cwd)

    from iac_code.memory.memory_manager import MemoryManager

    MemoryManager(str(config_dir / "memory")).save(
        "visual-deploy-memory",
        "Remember to review deployment cleanup and region defaults during visual audit.",
        "project",
        "deploy visual audit legacy memory",
    )

    from starlette.responses import Response
    from starlette.routing import Route

    import iac_code.services.capabilities.multimodal as multimodal
    from iac_code.web import pipeline as web_pipeline
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    state_service = FakePipelineStateService()
    web_pipeline.create_a2a_pipeline_recovery_service = lambda: state_service
    multimodal.is_model_multimodal = lambda *args, **kwargs: True

    manager = WebSessionManager(cwd=cwd)
    app = create_app(
        session_manager=manager,
        runtime_factory=lambda session: FakeWebRuntime(session, manager),
        shell_runner_factory=lambda: FakeShellRunner(),
        pipeline_action_runner_factory=lambda: FakePipelineActionRunner(manager, state_service),
    )

    async def favicon(_request: Any) -> Response:
        return Response(status_code=204)

    app.routes.insert(0, Route("/favicon.ico", favicon, methods=["GET"]))
    return app


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("IAC_CODE_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IAC_CODE_WEB_PORT", "8767")))
    parser.add_argument("--cwd", default=os.environ.get("IAC_CODE_CWD") or str(REPO_ROOT))
    parser.add_argument("--config-dir", default=os.environ.get("IAC_CODE_CONFIG_DIR") or "")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    cwd = Path(args.cwd).expanduser().resolve()
    config_dir = Path(args.config_dir).expanduser().resolve() if args.config_dir else Path(tempfile.mkdtemp())
    config_dir.mkdir(parents=True, exist_ok=True)

    app = build_app(cwd=cwd, config_dir=config_dir)
    print(f"WEB_SMOKE_SERVER_URL=http://{args.host}:{args.port}", flush=True)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
