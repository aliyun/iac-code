from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any, AsyncIterator

import httpx
from a2a.types import Role

from iac_code.a2a.signing import AgentCardSignature, agent_card_signature_jwks_url, verify_agent_card_dict
from iac_code.a2a.transport import A2AAuthConfig, A2ATransportBinding, UnsupportedA2ATransportError, headers_for_auth
from iac_code.a2a.transports.base import A2ATransportClient, TransportClientOptions, binding_from_url
from iac_code.a2a.transports.http import HttpA2AClient


class A2ACardVerificationError(ValueError):
    """Raised when a discovered Agent Card fails configured verification."""


TransportClientFactory = Callable[[TransportClientOptions], A2ATransportClient]


@dataclass(frozen=True)
class A2AClientResponse:
    payload: dict[str, Any]

    @property
    def text(self) -> str:
        result = self.payload.get("result")
        if not isinstance(result, dict):
            return ""
        text = result.get("text")
        if isinstance(text, str) and text:
            return text
        status = result.get("status")
        if isinstance(status, dict):
            extracted = _extract_parts_text(status.get("message"))
            if extracted:
                return extracted
        task = result.get("task")
        if isinstance(task, dict):
            task_status = task.get("status")
            if isinstance(task_status, dict):
                extracted = _extract_parts_text(task_status.get("message"))
                if extracted:
                    return extracted
            history = task.get("history")
            if isinstance(history, list):
                for entry in reversed(history):
                    extracted = _extract_agent_entry_text(entry)
                    if extracted:
                        return extracted
        return ""


_AGENT_ROLE_NAME = Role.Name(Role.ROLE_AGENT)


def _extract_agent_entry_text(entry: Any) -> str:
    if not isinstance(entry, dict) or entry.get("role") != _AGENT_ROLE_NAME:
        return ""
    return _extract_parts_text(entry)


def _extract_parts_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    pieces: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        value = part.get("text")
        if isinstance(value, str):
            pieces.append(value)
    return "".join(pieces)


class A2AClient:
    def __init__(
        self,
        *,
        http_client: Any | None = None,
        auth: A2AAuthConfig | None = None,
        verification_secret: str | None = None,
        verification_secrets: Mapping[str, str] | None = None,
        verification_jwks: Mapping[str, Any] | None = None,
        verification_jwks_url: str | None = None,
        require_card_signature: bool = False,
        transport_client_factory: TransportClientFactory | None = None,
        timeout_seconds: float | None = None,
        jwks_cache_ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._owns_http_client = http_client is None
        self._http_client = http_client or (
            httpx.AsyncClient(timeout=timeout_seconds) if timeout_seconds is not None else httpx.AsyncClient()
        )
        self._auth = auth
        self._verification_secret = verification_secret
        self._verification_secrets = verification_secrets
        self._verification_jwks = verification_jwks
        self._verification_jwks_url = verification_jwks_url
        self._remote_jwks_cache: dict[str, tuple[Mapping[str, Any], float]] = {}
        self._jwks_cache_ttl_seconds = max(0.0, jwks_cache_ttl_seconds)
        self._clock = clock
        self._require_card_signature = require_card_signature
        self._transport_client_factory = transport_client_factory

    async def discover(self, base_url: str) -> dict[str, Any]:
        url = base_url.rstrip("/") + "/.well-known/agent-card.json"
        response = await self._http_client.get(url, headers=headers_for_auth(self._auth))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("A2A Agent Card response must be a JSON object")
        if self._should_verify_agent_card(data):
            result = await self._verify_agent_card(data)
            if not result.valid:
                raise A2ACardVerificationError(f"A2A Agent Card verification failed: {result.message}")
        return data

    async def send_message(
        self,
        url: str,
        prompt: str,
        *,
        cwd: str,
        context_id: str | None = None,
    ) -> A2AClientResponse:
        payload = self._message_payload(method="SendMessage", prompt=prompt, cwd=cwd, context_id=context_id)
        transport = self._make_transport_client(url)
        response = await transport.send(payload)
        return A2AClientResponse(payload=response)

    async def stream_message(
        self,
        url: str,
        prompt: str,
        *,
        cwd: str,
        context_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = self._message_payload(method="SendStreamingMessage", prompt=prompt, cwd=cwd, context_id=context_id)
        transport = self._make_transport_client(url)
        async for event in transport.stream(payload):
            yield event

    async def get_task(self, url: str, task_id: str, *, history_length: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return await self._send_jsonrpc(url, method="GetTask", params=params)

    async def list_tasks(
        self,
        url: str,
        *,
        context_id: str | None = None,
        status: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
        include_artifacts: bool | None = None,
    ) -> dict[str, Any]:
        params = _without_none(
            {
                "contextId": context_id,
                "status": status,
                "pageSize": page_size,
                "pageToken": page_token,
                "includeArtifacts": include_artifacts,
            }
        )
        return await self._send_jsonrpc(url, method="ListTasks", params=params)

    async def cancel_task(self, url: str, task_id: str) -> dict[str, Any]:
        return await self._send_jsonrpc(url, method="CancelTask", params={"id": task_id})

    async def subscribe_task(self, url: str, task_id: str) -> AsyncIterator[dict[str, Any]]:
        payload = self._jsonrpc_payload(method="SubscribeToTask", params={"id": task_id})
        transport = self._make_transport_client(url)
        async for event in transport.stream(payload):
            yield event

    async def create_push_notification_config(
        self,
        endpoint_url: str,
        *,
        task_id: str,
        config_id: str,
        url: str,
        token: str | None = None,
        authentication: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = _without_none(
            {
                "taskId": task_id,
                "id": config_id,
                "url": url,
                "token": token,
                "authentication": dict(authentication) if authentication is not None else None,
            }
        )
        return await self._send_jsonrpc(endpoint_url, method="CreateTaskPushNotificationConfig", params=params)

    async def get_push_notification_config(self, url: str, *, task_id: str, config_id: str) -> dict[str, Any]:
        return await self._send_jsonrpc(
            url,
            method="GetTaskPushNotificationConfig",
            params={"taskId": task_id, "id": config_id},
        )

    async def list_push_notification_configs(
        self,
        url: str,
        *,
        task_id: str,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        return await self._send_jsonrpc(
            url,
            method="ListTaskPushNotificationConfigs",
            params=_without_none({"taskId": task_id, "pageSize": page_size, "pageToken": page_token}),
        )

    async def delete_push_notification_config(self, url: str, *, task_id: str, config_id: str) -> dict[str, Any]:
        return await self._send_jsonrpc(
            url,
            method="DeleteTaskPushNotificationConfig",
            params={"taskId": task_id, "id": config_id},
        )

    async def get_extended_agent_card(self, url: str) -> dict[str, Any]:
        return await self._send_jsonrpc(url, method="GetExtendedAgentCard", params={})

    async def aclose(self) -> None:
        if not self._owns_http_client:
            return
        close = getattr(self._http_client, "aclose", None)
        if close is not None:
            await close()

    def _should_verify_agent_card(self, card: Mapping[str, Any]) -> bool:
        return bool(
            self._require_card_signature
            or self._verification_secret
            or self._verification_secrets
            or self._verification_jwks
            or self._verification_jwks_url
        )

    async def _verify_agent_card(self, card: dict[str, Any]) -> AgentCardSignature:
        remote_jwks_url = self._verification_jwks_url or agent_card_signature_jwks_url(card)
        remote_jwks = await self._remote_jwks(remote_jwks_url, force_refresh=False) if remote_jwks_url else None
        jwks = _merge_jwks(remote_jwks, self._verification_jwks)
        result = verify_agent_card_dict(
            card,
            secret=self._verification_secret,
            secrets=self._verification_secrets,
            jwks=jwks,
            require_signature=self._require_card_signature,
        )
        if result.valid or remote_jwks_url is None or result.reason not in {"signature-mismatch", "unknown-key"}:
            return result

        refreshed_jwks = await self._remote_jwks(remote_jwks_url, force_refresh=True)
        return verify_agent_card_dict(
            card,
            secret=self._verification_secret,
            secrets=self._verification_secrets,
            jwks=_merge_jwks(refreshed_jwks, self._verification_jwks),
            require_signature=self._require_card_signature,
        )

    async def _remote_jwks(self, url: str, *, force_refresh: bool) -> Mapping[str, Any]:
        cached = self._remote_jwks_cache.get(url)
        if not force_refresh and cached is not None:
            data, expires_at = cached
            if self._clock() < expires_at:
                return data

        response = await self._http_client.get(url)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("A2A JWKS response must be a JSON object")
        self._remote_jwks_cache[url] = (data, self._clock() + self._jwks_cache_ttl_seconds)
        return data

    def _jsonrpc_headers(self) -> dict[str, str]:
        return {"A2A-Version": "1.0", **headers_for_auth(self._auth)}

    @staticmethod
    def select_endpoint_url(card: Mapping[str, Any], *, fallback_url: str) -> str:
        interfaces = card.get("supportedInterfaces")
        if isinstance(interfaces, Sequence) and not isinstance(interfaces, (str, bytes)):
            for item in interfaces:
                if not isinstance(item, Mapping):
                    continue
                url = item.get("url")
                if isinstance(url, str) and url:
                    return url

        url = card.get("url")
        if isinstance(url, str) and url:
            return url
        return fallback_url

    def _make_transport_client(self, url: str) -> A2ATransportClient:
        binding = binding_from_url(url)
        if binding.transport == "http":
            return _BoundHttpA2AClient(HttpA2AClient(http_client=self._http_client, auth=self._auth), url)
        if self._transport_client_factory is None:
            raise UnsupportedA2ATransportError(
                f"A2A transport {binding.transport!r} requires a transport client factory."
            )
        return self._transport_client_factory(self._transport_options(binding))

    def _transport_options(self, binding: A2ATransportBinding) -> TransportClientOptions:
        auth = self._auth or A2AAuthConfig()
        return TransportClientOptions(
            binding=binding,
            token=auth.bearer_token,
            basic_username=auth.basic_username,
            basic_password=auth.basic_password,
            api_key=auth.api_key,
            api_key_header=auth.api_key_header,
        )

    def _message_payload(self, *, method: str, prompt: str, cwd: str, context_id: str | None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": [{"text": prompt}],
            "metadata": {"iac_code": {"cwd": cwd}},
        }
        if context_id:
            message["contextId"] = context_id
        return self._jsonrpc_payload(
            method=method,
            params={
                "message": message,
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        )

    async def _send_jsonrpc(self, url: str, *, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = self._jsonrpc_payload(method=method, params=params)
        transport = self._make_transport_client(url)
        return await transport.send(payload)

    @staticmethod
    def _jsonrpc_payload(*, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }


def _without_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _merge_jwks(*jwks_values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    keys: list[Any] = []
    for jwks in jwks_values:
        if not jwks:
            continue
        jwks_keys = jwks.get("keys")
        if isinstance(jwks_keys, list):
            keys.extend(jwks_keys)
    return {"keys": keys} if keys else None


class _BoundHttpA2AClient:
    def __init__(self, client: HttpA2AClient, url: str) -> None:
        self._client = client
        self._url = url

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._client.send(self._url, payload)

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async for event in self._client.stream(self._url, payload):
            yield event

    async def aclose(self) -> None:
        return None
