from __future__ import annotations

import asyncio
import inspect
import logging
import socket
import time
from collections.abc import Awaitable
from ipaddress import ip_address
from typing import Any, Callable, Protocol, TypeAlias, cast
from urllib.parse import urlparse, urlunparse

import httpx

from iac_code.a2a.metrics import A2AMetrics, NoOpA2AMetrics
from iac_code.a2a.push import InvalidPushNotificationConfigError, validate_push_callback_url
from iac_code.a2a.push_queue import A2APushJob, A2APushQueue, A2APushRetryPolicy, redact_push_headers

logger = logging.getLogger(__name__)


class A2APushAlertSink(Protocol):
    async def dead_lettered(self, job: A2APushJob) -> None: ...


class A2APushCallbackConnector(Protocol):
    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> Any: ...


A2APushHeaderResolver: TypeAlias = Callable[[str, str], "dict[str, str] | Awaitable[dict[str, str]]"]


class LoggingA2APushAlertSink:
    async def dead_lettered(self, job: A2APushJob) -> None:
        logger.error(
            "A2A push notification dead-lettered",
            extra={
                "task_id": job.task_id,
                "config_id": job.config_id,
                "url": job.url,
                "attempt": job.attempt,
                "last_error": job.last_error,
                "headers": redact_push_headers(job.headers),
            },
        )


class DefaultA2APushCallbackConnector:
    def __init__(self, *, http_client: Any) -> None:
        self._http_client = http_client

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
        validate_push_callback_url(url)
        validation = _validate_resolved_callback_host(url)
        addresses = await validation if inspect.isawaitable(validation) else validation
        if addresses is None:
            raise InvalidPushNotificationConfigError("A2A push callback delivery requires verified callback addresses")
        if not addresses:
            raise InvalidPushNotificationConfigError("A2A push callback URL host did not resolve to any addresses")
        pinned_url, pinned_headers, extensions = _pinned_callback_request(url, addresses[0], headers)
        async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0)) as client:
            return await client.post(
                pinned_url,
                json=json,
                headers=pinned_headers,
                timeout=timeout,
                extensions=extensions,
            )


class A2APushDeliveryWorker:
    def __init__(
        self,
        *,
        queue: A2APushQueue,
        http_client: Any | None = None,
        connector: A2APushCallbackConnector | None = None,
        metrics: A2AMetrics | None = None,
        retry_policy: A2APushRetryPolicy | None = None,
        alert_sink: A2APushAlertSink | None = None,
        header_resolver: A2APushHeaderResolver | None = None,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._queue = queue
        self._owns_http_client = http_client is None and connector is None
        self._http_client = http_client or (httpx.AsyncClient() if connector is None else None)
        self._connector = connector or DefaultA2APushCallbackConnector(http_client=self._http_client)
        self._metrics = metrics or NoOpA2AMetrics()
        self._retry_policy = retry_policy or A2APushRetryPolicy()
        self._alert_sink = alert_sink or LoggingA2APushAlertSink()
        self._header_resolver = header_resolver
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    async def run_once(self) -> bool:
        job = await self._queue.claim(now=self._clock())
        if job is None:
            return False

        started = self._clock()
        try:
            headers = await self._resolve_headers(job)
            response = await self._connector.post(
                job.url,
                json=job.payload,
                headers=headers,
                timeout=self._timeout_seconds,
            )
            if 200 <= response.status_code < 300:
                try:
                    await self._queue.ack(job.job_id)
                except Exception:
                    logger.exception(
                        "A2A push notification delivered but queue ack failed; lease recovery will retry ownership",
                        extra={"task_id": job.task_id, "config_id": job.config_id, "job_id": job.job_id},
                    )
                    return False
                self._metrics.record_push_delivered(duration_ms=(self._clock() - started) * 1000)
                return True
            raise RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:
            await self._handle_failure(job, exc)
            return False

    async def serve_forever(self, *, idle_sleep_seconds: float = 0.25) -> None:
        while True:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(idle_sleep_seconds)

    async def aclose(self) -> None:
        if not self._owns_http_client:
            return
        close = getattr(self._http_client, "aclose", None)
        if close is not None:
            await close()

    async def _handle_failure(self, job: A2APushJob, exc: Exception) -> None:
        next_attempt = job.attempt + 1
        transient = _is_transient_delivery_error(exc)
        if transient:
            self._metrics.record_push_transient_failure()
        else:
            self._metrics.record_push_permanent_failure()

        if transient and next_attempt < self._retry_policy.max_attempts:
            delay = self._retry_policy.delay_for_attempt(next_attempt)
            await self._queue.retry(
                job.with_attempt(attempt=next_attempt, next_attempt_at=self._clock() + delay, last_error=str(exc))
            )
            self._metrics.record_push_retry_scheduled()
            return

        dead = job.with_attempt(attempt=next_attempt, last_error=str(exc))
        await self._queue.dead_letter(dead)
        self._metrics.record_push_dead_lettered()
        await self._alert_sink.dead_lettered(dead)

    async def _resolve_headers(self, job: A2APushJob) -> dict[str, str]:
        if self._header_resolver is None:
            return dict(job.headers)
        resolved = self._header_resolver(job.task_id, job.config_id)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        resolved = cast(dict[str, str], resolved)
        return dict(resolved)


def _is_transient_delivery_error(exc: Exception) -> bool:
    text = str(exc)
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, OSError)):
        return True
    return any(f"HTTP {code}" in text for code in (408, 409, 425, 429, 500, 502, 503, 504))


async def _validate_resolved_callback_host(url: str) -> list[str]:
    parsed = urlparse(url)
    if parsed.hostname is None:
        raise InvalidPushNotificationConfigError("A2A push callback URL must include a host")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443)
    verified: list[str] = []
    for _, _, _, _, sockaddr in addresses:
        host = str(sockaddr[0])
        address = ip_address(host)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise InvalidPushNotificationConfigError("A2A push callback URL must not resolve to private or local hosts")
        verified.append(host)
    if not verified:
        raise InvalidPushNotificationConfigError("A2A push callback URL host did not resolve to any addresses")
    return verified


def _pinned_callback_request(
    url: str,
    address: str,
    headers: dict[str, str],
) -> tuple[str, dict[str, str], dict[str, str]]:
    parsed = urlparse(url)
    if parsed.hostname is None:
        raise InvalidPushNotificationConfigError("A2A push callback URL must include a host")

    host_header = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port is not None:
        host_header = f"{host_header}:{parsed.port}"

    pinned_host = f"[{address}]" if ":" in address else address
    netloc = pinned_host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

    pinned_headers = dict(headers)
    pinned_headers["Host"] = host_header
    pinned_url = urlunparse(parsed._replace(netloc=netloc))
    return pinned_url, pinned_headers, {"sni_hostname": parsed.hostname}
