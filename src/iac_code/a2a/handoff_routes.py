"""Authenticated handoff HTTP routes, shared by both execution surfaces."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from iac_code.a2a.handoff import (
    HandoffAuthorizeRequest,
    HandoffDiscoverRequest,
    HandoffInputRequest,
    HandoffInputResult,
    HandoffPrepareRequest,
    HandoffRestoreRequest,
    HandoffResult,
    SessionHandoffService,
)
from iac_code.services.handoff_fence import HandoffFrozenError

logger = logging.getLogger(__name__)


class SessionHandoffRoutes:
    def __init__(self, service: SessionHandoffService, *, resolve_cwd: Any) -> None:
        self.service = service
        self.resolve_cwd = resolve_cwd

    def routes(self) -> list[Route]:
        return [
            Route("/iac-code/handoff/capabilities", self.capabilities, methods=["GET"]),
            Route("/iac-code/handoff/prepare", self.prepare, methods=["POST"]),
            Route("/iac-code/handoff/discover", self.discover, methods=["POST"]),
            Route("/iac-code/handoff/apply-input", self.apply_input, methods=["POST"]),
            Route("/iac-code/handoff/restore", self.restore, methods=["POST"]),
            Route("/iac-code/handoff/authorize", self.authorize, methods=["POST"]),
        ]

    async def apply_input(self, request: Request) -> JSONResponse:
        if self.resolve_cwd is None:
            return JSONResponse({"status": "UNSUPPORTED", "reason": "handoff_executor_unavailable"})
        try:
            parsed = HandoffInputRequest.model_validate(await request.json())
            if self.resolve_cwd({"iac_code": {"cwd": parsed.receipt.cwd}}) != parsed.receipt.cwd:
                raise ValueError("Workspace identity changed")
        except (ValueError, ValidationError):
            return JSONResponse({"error": "Invalid handoff input"}, status_code=400)
        try:
            result = await self.service.apply_pending_input(parsed)
        except (HandoffFrozenError, ValueError):
            result = HandoffInputResult(status="CONFLICT", reason="handoff_input_unverifiable")
        except Exception:
            result = HandoffInputResult(status="UNKNOWN", reason="handoff_input_acceptance_unconfirmed")
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    async def capabilities(self, _: Request) -> JSONResponse:
        return JSONResponse(self.service.capabilities())

    async def prepare(self, request: Request) -> JSONResponse:
        return await self._handle(request, "prepare")

    async def discover(self, request: Request) -> JSONResponse:
        return await self._handle(request, "discover")

    async def restore(self, request: Request) -> JSONResponse:
        return await self._handle(request, "restore")

    async def authorize(self, request: Request) -> JSONResponse:
        return await self._handle(request, "authorize")

    async def _handle(self, request: Request, operation: str) -> JSONResponse:
        if self.resolve_cwd is None:
            return JSONResponse({"status": "UNSUPPORTED", "reason": "handoff_executor_unavailable"})
        try:
            payload = await request.json()
            models = {
                "prepare": HandoffPrepareRequest,
                "discover": HandoffDiscoverRequest,
                "restore": HandoffRestoreRequest,
                "authorize": HandoffAuthorizeRequest,
            }
            parsed = models[operation].model_validate(payload)
            identity = (
                parsed
                if isinstance(parsed, HandoffPrepareRequest)
                else parsed.request
                if isinstance(parsed, HandoffDiscoverRequest)
                else parsed.receipt
            )
            cwd = self.resolve_cwd({"iac_code": {"cwd": identity.cwd}})
            if cwd != identity.cwd:
                raise ValueError("Handoff workspace identity is not canonical")
        except (ValueError, ValidationError):
            return JSONResponse({"error": "Invalid session handoff identity"}, status_code=400)
        try:
            if operation == "discover":
                result = await self.service.discover_completed_session(HandoffDiscoverRequest.model_validate(payload))
            elif operation == "prepare":
                result = await self.service.prepare_handoff(HandoffPrepareRequest.model_validate(payload))
            elif operation == "restore":
                result = await self.service.restore_handoff(HandoffRestoreRequest.model_validate(payload))
            else:
                result = await self.service.authorize_destination(HandoffAuthorizeRequest.model_validate(payload))
        except (HandoffFrozenError, ValueError):
            result = HandoffResult(status="CONFLICT", reason="handoff_state_unverifiable")
        except FileNotFoundError:
            result = HandoffResult(status="NOT_READY", reason="handoff_files_not_visible")
        except Exception as exc:
            logger.warning("Session handoff failed operation=%s error_type=%s", operation, type(exc).__name__)
            result = HandoffResult(status="UNKNOWN", reason="handoff_operation_unconfirmed")
        return JSONResponse(result.model_dump(mode="json", by_alias=True))
