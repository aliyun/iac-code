"""AG-UI handoff keeps the adapter checkpoint and original A2A identities."""

from __future__ import annotations

import base64
import hashlib
import hmac

from ag_ui.core import ImageInputContent, InputContentDataSource, RunAgentInput, TextInputContent, UserMessage
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from iac_code.agui.adapter import AguiA2AAdapter
from iac_code.agui.inputs import canonical_digest, parse_run_input


class AguiHandoffRoutes:
    def __init__(self, adapter: AguiA2AAdapter, token: str | None) -> None:
        self.adapter = adapter
        self.token = token

    def routes(self) -> list[Route]:
        return [Route("/iac-code/handoff/{operation:str}", self.handle, methods=["GET", "POST"])]

    async def handle(self, request: Request) -> JSONResponse:
        if self.token and not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + self.token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        operation = request.path_params["operation"]
        if operation not in {"capabilities", "prepare", "restore", "authorize", "discover", "apply-input"}:
            return JSONResponse({"error": "Unknown handoff operation"}, status_code=404)
        try:
            payload = None if operation == "capabilities" else await request.json()
            if payload is not None:
                identity = (
                    payload
                    if operation == "prepare"
                    else payload.get("request", {})
                    if operation == "discover"
                    else payload.get("receipt", {})
                )
                if identity.get("protocol") != "agui":
                    return JSONResponse({"error": "AG-UI handoff identity is required"}, status_code=400)
            if operation == "apply-input":
                if not isinstance(payload, dict):
                    raise ValueError("Invalid input payload")
                payload["skipDelivery"] = True
                result = await self.adapter.client.handoff_request(self.adapter.a2a_url, operation, payload)
                if result.get("status") == "NOT_READY" and result.get("reason") == "agui_input_delivery_required":
                    run_input = self._pending_run_input(payload)
                    ticket = await self.adapter.admit(
                        run_input, canonical_digest(run_input.model_dump(mode="json", by_alias=True))
                    )
                    stream = self.adapter.stream(ticket)
                    try:
                        async for _ in stream:
                            pass
                    finally:
                        close = getattr(stream, "aclose", None)
                        if close is not None:
                            await close()
                    result = await self.adapter.client.handoff_request(self.adapter.a2a_url, operation, payload)
            else:
                result = await self.adapter.client.handoff_request(self.adapter.a2a_url, operation, payload)
            return JSONResponse(result)
        except ValueError:
            return JSONResponse({"error": "Invalid handoff request"}, status_code=400)
        except Exception:
            return JSONResponse({"status": "UNKNOWN", "reason": "agui_handoff_upstream_unconfirmed"})

    def _pending_run_input(self, payload: dict) -> RunAgentInput:
        receipt, pending = payload["receipt"], payload["pendingInput"]
        identity = receipt["aguiIdentity"]
        guidance_id = receipt["guidanceId"]
        metadata = (payload.get("callerMetadata") or {}).get("iac_code", {})
        forwarded = {
            "schemaVersion": 1,
            "rosInvocationId": identity["rosInvocationId"],
            "cwd": receipt["cwd"],
            "userId": receipt["userId"],
            "runMode": receipt["mode"],
            "guidanceId": guidance_id,
            "inputDigest": payload["inputDigest"],
            "executionFence": {"version": "session-handoff-v1", "owner": payload["target"]},
            "model": metadata.get("iac_code_model"),
            "llmApiKey": metadata.get("iac_code_api_key"),
            "llmHeaders": metadata.get("llm_headers"),
            "thinking": metadata.get("thinking"),
            "pipelineName": metadata.get("pipeline_name"),
            "preferredLanguage": metadata.get("preferredLanguage"),
        }
        cloud = {
            "accessKeyId": metadata.get("alibaba_cloud_access_key_id"),
            "accessKeySecret": metadata.get("alibaba_cloud_access_key_secret"),
            "securityToken": metadata.get("alibaba_cloud_security_token"),
            "regionId": metadata.get("alibaba_cloud_region_id"),
        }
        if any(value is not None for value in cloud.values()):
            forwarded["alibabaCloud"] = {key: value for key, value in cloud.items() if value is not None}
        envelope = (pending.get("agui_raw_input") or {}).get("run_input")
        if isinstance(envelope, dict):
            run_input = parse_run_input(envelope)
        else:
            query = pending.get("query")
            content: list = [TextInputContent(text=query)] if query else []
            images = (pending.get("image_attachments") or {}).get("items", [])
            materialized = pending.get("image_parts") or images
            if len(images) != len(materialized):
                raise ValueError("Image content has no recoverable checkpoint")
            for descriptor, image in zip(images, materialized, strict=True):
                encoded = image.get("bytes_base64", "")
                image_bytes = base64.b64decode(encoded, validate=True)
                if (
                    len(image_bytes) != descriptor["size_bytes"]
                    or hashlib.sha256(image_bytes).hexdigest() != descriptor["sha256"]
                ):
                    raise ValueError("Image checkpoint digest changed")
                content.append(
                    ImageInputContent(
                        source=InputContentDataSource(value=encoded, mime_type=descriptor["media_type"]),
                        metadata={"filename": descriptor["filename"]},
                    )
                )
            run_input = RunAgentInput(
                thread_id=identity["threadId"],
                run_id=guidance_id,
                messages=[UserMessage(id=guidance_id, content=content)] if content else [],
                state={},
                tools=[],
                context=[],
                forwarded_props={},
            )
        envelope = run_input.model_dump(mode="json", by_alias=True)
        resume = pending.get("interrupt_responses") or envelope.get("resume")
        forwarded["activeGuidance"] = not bool(resume)
        envelope.update(
            threadId=identity["threadId"],
            runId=guidance_id,
            resume=resume,
            forwardedProps={"iacCode": {key: value for key, value in forwarded.items() if value is not None}},
        )
        return parse_run_input(envelope)
