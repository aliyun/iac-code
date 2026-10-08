"""Read-only subscription to an already accepted AG-UI execution."""

from __future__ import annotations

import contextlib
import copy
import hmac
from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import (
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
)
from ag_ui.encoder import EventEncoder
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from iac_code.agui.adapter import AguiA2AAdapter, ThreadBinding
from iac_code.agui.events import (
    A2AEventMapper,
    a2a_context_id,
    a2a_inputs,
    a2a_state,
    a2a_task_id,
    interrupt_from_a2a,
    timestamp_ms,
)


class AguiExecutionSubscriptions:
    def __init__(self, adapter: AguiA2AAdapter, token: str | None) -> None:
        self.adapter = adapter
        self.token = token

    def routes(self) -> list[Route]:
        return [
            Route("/extensions/iac-code/v1/executions/{execution_id:str}/subscribe", self.subscribe, methods=["POST"])
        ]

    async def subscribe(self, request: Request):
        if self.token and not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + self.token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) - {
                "threadId",
                "runId",
                "rosInvocationId",
                "afterSequence",
            }:
                raise ValueError("Invalid observation envelope")
            if not all(
                isinstance(payload.get(key), str) and payload[key] for key in ("threadId", "runId", "rosInvocationId")
            ):
                raise ValueError("Missing observation identity")
            binding = self.adapter._load_thread(payload["threadId"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid observation identity"}, status_code=400)
        if binding is None or binding.task_id is None:
            return JSONResponse({"error": "Execution checkpoint unavailable"}, status_code=404)
        if (
            binding.execution_id != request.path_params["execution_id"]
            or binding.ros_invocation_id != payload["rosInvocationId"]
            or payload["runId"] not in binding.run_digests
        ):
            return JSONResponse({"error": "Execution observation identity changed"}, status_code=409)
        # The observer never owns active_run_id or the producer's cancellation.
        return StreamingResponse(
            self._stream(copy.deepcopy(binding), payload["runId"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _stream(self, binding: ThreadBinding, run_id: str) -> AsyncIterator[str]:
        encoder = EventEncoder()
        mapper = A2AEventMapper(
            thread_id=binding.thread_id,
            run_id=run_id,
            open_pipeline_steps=set(binding.pipeline_open_steps),
            text_snapshot_digests=set(binding.text_snapshot_digests),
        )
        yield encoder.encode(RunStartedEvent(thread_id=binding.thread_id, run_id=run_id, timestamp=timestamp_ms()))
        yield encoder.encode(
            mapper.session_event(
                execution_id=binding.execution_id,
                context_id=binding.context_id,
                task_id=binding.task_id,
                ros_invocation_id=binding.ros_invocation_id,
                session_id=binding.iac_code_session_id,
            )
        )
        stream = self.adapter._stream_after_sideband(binding)
        try:
            async for event in stream:
                if a2a_task_id(event) not in {None, binding.task_id} or a2a_context_id(event) not in {
                    None,
                    binding.context_id,
                }:
                    raise ValueError("Original task observation identity changed")
                for mapped in mapper.map(event):
                    yield encoder.encode(mapped)
                pending = a2a_inputs(event)
                state = a2a_state(event)
                if pending or state in {
                    "input-required",
                    "completed",
                    "failed",
                    "rejected",
                    "canceled",
                    "auth-required",
                }:
                    for closing in mapper.close_all():
                        yield encoder.encode(closing)
                    if state in {"failed", "rejected", "auth-required"}:
                        yield encoder.encode(
                            RunErrorEvent(
                                message="The observed execution failed.",
                                code="EXECUTION_FAILED",
                                timestamp=timestamp_ms(),
                            )
                        )
                    outcome: Any = (
                        RunFinishedInterruptOutcome(interrupts=[interrupt_from_a2a(value) for value in pending])
                        if pending
                        else RunFinishedSuccessOutcome()
                    )
                    yield encoder.encode(
                        RunFinishedEvent(
                            thread_id=binding.thread_id, run_id=run_id, outcome=outcome, timestamp=timestamp_ms()
                        )
                    )
                    return
        finally:
            with contextlib.suppress(Exception):
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
