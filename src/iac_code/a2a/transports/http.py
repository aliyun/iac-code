from __future__ import annotations

import json
from typing import Any, AsyncIterator, cast

import httpx

from iac_code.a2a.transport import A2AAuthConfig, headers_for_auth


class HttpA2AClient:
    def __init__(self, *, http_client: Any | None = None, auth: A2AAuthConfig | None = None) -> None:
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient()
        self._auth = auth

    async def send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._http_client.post(url, json=payload, headers=self._headers())
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("A2A HTTP response must be a JSON object")
        return data

    async def stream(self, url: str, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async with self._http_client.stream("POST", url, json=payload, headers=self._headers()) as response:
            response.raise_for_status()
            lines = cast(AsyncIterator[str | bytes], response.aiter_lines())
            async for raw_line in lines:
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                if line.startswith("data:"):
                    yield json.loads(line.removeprefix("data:").strip())

    async def aclose(self) -> None:
        if not self._owns_http_client:
            return
        close = getattr(self._http_client, "aclose", None)
        if close is not None:
            await close()

    def _headers(self) -> dict[str, str]:
        return {"A2A-Version": "1.0", **headers_for_auth(self._auth)}
