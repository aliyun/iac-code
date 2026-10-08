"""Deliver one proven unapplied input through the existing task protocol."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

from iac_code.a2a.client import A2AClient
from iac_code.a2a.transport import A2AAuthConfig
from iac_code.agui.events import a2a_context_id, a2a_task_id

if TYPE_CHECKING:
    from iac_code.a2a.handoff import HandoffInputRequest


class HandoffInputDelivery:
    def __init__(self, url: str, auth: A2AAuthConfig):
        self.url = url
        self.auth = auth

    async def apply(self, request: HandoffInputRequest) -> None:
        receipt = request.receipt
        if not receipt.guidance_id:
            raise ValueError("Guidance identity is required")
        pending = request.pending_input
        parts = []
        if isinstance(pending.get("query"), str) and pending["query"]:
            parts.append({"text": pending["query"]})
        images = (pending.get("image_attachments") or {}).get("items", [])
        materialized = pending.get("image_parts") or images
        if len(images) != len(materialized):
            raise ValueError("Image content has not been checkpointed")
        for descriptor, image in zip(images, materialized, strict=True):
            encoded = image.get("bytes_base64", "")
            payload = base64.b64decode(encoded, validate=True)
            if len(payload) != descriptor["size_bytes"] or hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
                raise ValueError("Image checkpoint digest changed")
            parts.append(
                {"data": {"filename": descriptor["filename"], "bytes": encoded}, "mediaType": descriptor["media_type"]}
            )
        if not parts:
            raise ValueError("The accepted task has no recoverable input")
        metadata = dict((request.caller_metadata or {}).get("iac_code", {}))
        metadata.update(
            guidance_id=receipt.guidance_id,
            input_digest=request.input_digest,
            user_id=receipt.user_id,
            execution_fence={
                "version": "session-handoff-v1",
                "owner": request.target.model_dump(mode="json", by_alias=True),
            },
            run_mode=receipt.mode,
        )
        client = A2AClient(auth=self.auth)
        stream = client.stream_message_parts(
            self.url,
            parts,
            cwd=receipt.cwd,
            context_id=receipt.context_id,
            task_id=receipt.task_id,
            message_id="handoff-" + receipt.guidance_id,
            iac_code_metadata=metadata,
            model=metadata.get("iac_code_model"),
            iac_code_api_key=metadata.get("iac_code_api_key"),
        )
        try:
            async for event in stream:
                if a2a_task_id(event) not in {None, receipt.task_id} or a2a_context_id(event) not in {
                    None,
                    receipt.context_id,
                }:
                    raise ValueError("Input delivery changed the original execution")
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
            await client.aclose()
