import logging

import pytest

from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.push_queue import A2APushJob, A2APushRetryPolicy, LocalFileA2APushQueue
from iac_code.a2a.push_worker import A2APushDeliveryWorker, LoggingA2APushAlertSink

from .fakes import FakeRedisPushStore


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTPClient:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.posts: list[dict[str, object]] = []

    async def post(self, url: str, *, json, headers, timeout, **kwargs):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout, **kwargs})
        return FakeResponse(self.statuses.pop(0))


class FakeOneShotHTTPClient(FakeHTTPClient):
    instances: list["FakeOneShotHTTPClient"] = []

    def __init__(self, *, limits=None) -> None:
        super().__init__([204])
        self.limits = limits
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True


class RecordingConnector:
    def __init__(self, status_code: int = 204) -> None:
        self.status_code = status_code
        self.posts: list[dict[str, object]] = []

    async def post(self, url: str, *, json, headers, timeout):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(self.status_code)


class RaisingConnector:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def post(self, url: str, *, json, headers, timeout):
        raise self.exc


class RecordingAlertSink:
    def __init__(self) -> None:
        self.jobs: list[A2APushJob] = []

    async def dead_lettered(self, job: A2APushJob) -> None:
        self.jobs.append(job)


@pytest.mark.asyncio
async def test_push_worker_uses_injected_connector_for_delivery(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    connector = RecordingConnector()
    worker = A2APushDeliveryWorker(queue=queue, connector=connector, metrics=NoOpA2AMetrics())
    await queue.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"ok": True},
            headers={},
        )
    )

    delivered = await worker.run_once()

    assert delivered is True
    assert connector.posts[0]["url"] == "https://callback.example/a2a"


@pytest.mark.asyncio
async def test_push_worker_delivers_redis_claimed_job() -> None:
    from iac_code.a2a.push_queue import RedisStreamsA2APushQueue

    redis = FakeRedisPushStore()
    queue = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-1",
    )
    connector = RecordingConnector()
    worker = A2APushDeliveryWorker(queue=queue, connector=connector, metrics=NoOpA2AMetrics())
    await queue.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"ok": True},
        )
    )

    delivered = await worker.run_once()

    assert delivered is True
    assert connector.posts
    assert redis.acked == [("push", "workers", "1-0")]


@pytest.mark.asyncio
async def test_default_callback_connector_rejects_private_dns(monkeypatch) -> None:
    from iac_code.a2a.push import InvalidPushNotificationConfigError
    from iac_code.a2a.push_worker import DefaultA2APushCallbackConnector

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("10.0.0.1", port))],
    )
    connector = DefaultA2APushCallbackConnector(http_client=FakeHTTPClient([204]))

    with pytest.raises(InvalidPushNotificationConfigError):
        await connector.post("https://callback.example/a2a", json={"ok": True}, headers={}, timeout=5.0)


@pytest.mark.asyncio
async def test_default_callback_connector_pins_validated_ip_and_preserves_host(monkeypatch) -> None:
    from iac_code.a2a.push_worker import DefaultA2APushCallbackConnector

    FakeOneShotHTTPClient.instances = []
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    monkeypatch.setattr("iac_code.a2a.push_worker.httpx.AsyncClient", FakeOneShotHTTPClient)
    http = FakeHTTPClient([204])
    connector = DefaultA2APushCallbackConnector(http_client=http)

    await connector.post(
        "https://callback.example:8443/a2a",
        json={"ok": True},
        headers={"X-Trace": "trace-1"},
        timeout=5.0,
    )

    assert http.posts == []
    assert FakeOneShotHTTPClient.instances[0].posts[0]["url"] == "https://93.184.216.34:8443/a2a"
    assert FakeOneShotHTTPClient.instances[0].posts[0]["headers"] == {
        "X-Trace": "trace-1",
        "Host": "callback.example:8443",
    }
    assert FakeOneShotHTTPClient.instances[0].posts[0]["extensions"] == {"sni_hostname": "callback.example"}


@pytest.mark.asyncio
async def test_default_callback_connector_uses_isolated_clients_for_pinned_hosts_on_same_ip(monkeypatch) -> None:
    from iac_code.a2a.push_worker import DefaultA2APushCallbackConnector

    FakeOneShotHTTPClient.instances = []
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    monkeypatch.setattr("iac_code.a2a.push_worker.httpx.AsyncClient", FakeOneShotHTTPClient)
    pooled_http = FakeHTTPClient([204, 204])
    connector = DefaultA2APushCallbackConnector(http_client=pooled_http)

    await connector.post("https://first.example/a2a", json={"ok": True}, headers={}, timeout=5.0)
    await connector.post("https://second.example/a2a", json={"ok": True}, headers={}, timeout=5.0)

    assert pooled_http.posts == []
    assert len(FakeOneShotHTTPClient.instances) == 2
    assert [client.posts[0]["url"] for client in FakeOneShotHTTPClient.instances] == [
        "https://93.184.216.34/a2a",
        "https://93.184.216.34/a2a",
    ]
    assert [client.posts[0]["extensions"] for client in FakeOneShotHTTPClient.instances] == [
        {"sni_hostname": "first.example"},
        {"sni_hostname": "second.example"},
    ]
    assert all(client.closed for client in FakeOneShotHTTPClient.instances)


@pytest.mark.asyncio
async def test_default_callback_connector_rejects_empty_dns_result(monkeypatch) -> None:
    from iac_code.a2a.push import InvalidPushNotificationConfigError
    from iac_code.a2a.push_worker import DefaultA2APushCallbackConnector

    monkeypatch.setattr("socket.getaddrinfo", lambda host, port: [])
    connector = DefaultA2APushCallbackConnector(http_client=FakeHTTPClient([204]))

    with pytest.raises(InvalidPushNotificationConfigError):
        await connector.post("https://callback.example/a2a", json={"ok": True}, headers={}, timeout=5.0)


@pytest.mark.asyncio
async def test_default_callback_connector_rejects_validator_without_pinned_addresses(monkeypatch) -> None:
    from iac_code.a2a.push import InvalidPushNotificationConfigError
    from iac_code.a2a.push_worker import DefaultA2APushCallbackConnector

    monkeypatch.setattr("iac_code.a2a.push_worker._validate_resolved_callback_host", lambda url: None)
    connector = DefaultA2APushCallbackConnector(http_client=FakeHTTPClient([204]))

    with pytest.raises(InvalidPushNotificationConfigError, match="verified callback addresses"):
        await connector.post("https://callback.example/a2a", json={"ok": True}, headers={}, timeout=5.0)


@pytest.mark.asyncio
async def test_default_callback_connector_brackets_ipv6_literal_host_header(monkeypatch) -> None:
    from iac_code.a2a.push_worker import DefaultA2APushCallbackConnector

    FakeOneShotHTTPClient.instances = []
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port: [(10, 1, 6, "", ("2001:4860:4860::8888", port, 0, 0))],
    )
    monkeypatch.setattr("iac_code.a2a.push_worker.httpx.AsyncClient", FakeOneShotHTTPClient)
    http = FakeHTTPClient([204])
    connector = DefaultA2APushCallbackConnector(http_client=http)

    await connector.post(
        "https://[2001:4860:4860::8888]:8443/a2a",
        json={"ok": True},
        headers={},
        timeout=5.0,
    )

    assert http.posts == []
    assert FakeOneShotHTTPClient.instances[0].posts[0]["headers"]["Host"] == "[2001:4860:4860::8888]:8443"


@pytest.mark.asyncio
async def test_push_worker_delivers_and_acks_success(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    connector = RecordingConnector()
    worker = A2APushDeliveryWorker(queue=queue, connector=connector, metrics=NoOpA2AMetrics())
    await queue.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"ok": True},
            headers={"Authorization": "Bearer secret"},
        )
    )

    delivered = await worker.run_once()

    assert delivered is True
    assert connector.posts[0]["url"] == "https://callback.example/a2a"
    assert not (tmp_path / "inflight" / "job-1.json").exists()


@pytest.mark.asyncio
async def test_push_worker_does_not_retry_or_dead_letter_when_ack_fails_after_callback_success() -> None:
    class AckFailingQueue:
        def __init__(self) -> None:
            self.job = A2APushJob(
                job_id="job-1",
                task_id="task-1",
                config_id="cfg-1",
                url="https://callback.example/a2a",
                payload={"ok": True},
                headers={},
            )
            self.acked: list[str] = []
            self.retried: list[A2APushJob] = []
            self.dead: list[A2APushJob] = []

        async def claim(self, *, now=None):
            return self.job

        async def ack(self, job_id: str) -> None:
            self.acked.append(job_id)
            raise RuntimeError("ack failed")

        async def retry(self, job: A2APushJob) -> None:
            self.retried.append(job)

        async def dead_letter(self, job: A2APushJob) -> None:
            self.dead.append(job)

    queue = AckFailingQueue()
    connector = RecordingConnector()
    worker = A2APushDeliveryWorker(queue=queue, connector=connector, metrics=NoOpA2AMetrics())

    delivered = await worker.run_once()

    assert delivered is False
    assert connector.posts
    assert queue.acked == ["job-1"]
    assert queue.retried == []
    assert queue.dead == []


@pytest.mark.asyncio
async def test_push_worker_retries_transient_failure_with_backoff(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    connector = RecordingConnector(status_code=503)
    worker = A2APushDeliveryWorker(
        queue=queue,
        connector=connector,
        metrics=NoOpA2AMetrics(),
        retry_policy=A2APushRetryPolicy(initial_delay_seconds=2.0, max_delay_seconds=10.0, jitter_ratio=0.0),
        clock=lambda: 100.0,
    )
    await queue.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"ok": True},
            headers={},
        )
    )

    delivered = await worker.run_once()

    assert delivered is False
    retried = await queue.claim(now=101.0)
    assert retried is None
    retried = await queue.claim(now=102.0)
    assert retried is not None
    assert retried.attempt == 1


@pytest.mark.asyncio
async def test_push_worker_retry_stores_public_error_summary(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    connector = RaisingConnector(
        TimeoutError("Authorization: Bearer sk-live Cookie=session-secret at /Users/alice/.iac-code/settings.yml")
    )
    worker = A2APushDeliveryWorker(
        queue=queue,
        connector=connector,
        metrics=NoOpA2AMetrics(),
        retry_policy=A2APushRetryPolicy(initial_delay_seconds=0.0, jitter_ratio=0.0, max_attempts=2),
        clock=lambda: 100.0,
    )
    await queue.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a?token=callback-secret",
            payload={"ok": True},
            headers={},
        )
    )

    delivered = await worker.run_once()

    assert delivered is False
    retried = await queue.claim(now=100.0)
    assert retried is not None
    assert retried.last_error.startswith("TimeoutError:")
    assert "sk-live" not in retried.last_error
    assert "session-secret" not in retried.last_error
    assert "/Users/alice" not in retried.last_error
    assert "[REDACTED]" in retried.last_error


@pytest.mark.asyncio
async def test_push_worker_dead_letters_permanent_failure(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    connector = RecordingConnector(status_code=400)
    worker = A2APushDeliveryWorker(
        queue=queue,
        connector=connector,
        metrics=NoOpA2AMetrics(),
        alert_sink=LoggingA2APushAlertSink(),
    )
    await queue.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"ok": True},
            headers={},
        )
    )

    delivered = await worker.run_once()

    assert delivered is False
    assert (tmp_path / "dead" / "job-1.json").exists()


@pytest.mark.asyncio
async def test_push_worker_dead_letter_stores_public_error_summary(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    alert_sink = RecordingAlertSink()
    connector = RaisingConnector(
        RuntimeError("Authorization: Bearer sk-live Cookie=session-secret at /Users/alice/.iac-code/settings.yml")
    )
    worker = A2APushDeliveryWorker(
        queue=queue,
        connector=connector,
        metrics=NoOpA2AMetrics(),
        alert_sink=alert_sink,
    )
    await queue.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://user:pass@callback.example/a2a?token=callback-secret#frag",
            payload={"ok": True},
            headers={},
        )
    )

    delivered = await worker.run_once()

    assert delivered is False
    assert alert_sink.jobs
    dead = alert_sink.jobs[0]
    assert dead.last_error.startswith("RuntimeError:")
    assert "sk-live" not in dead.last_error
    assert "session-secret" not in dead.last_error
    assert "/Users/alice" not in dead.last_error
    assert "[REDACTED]" in dead.last_error


@pytest.mark.asyncio
async def test_logging_push_alert_sink_sanitizes_url_and_error(caplog) -> None:
    sink = LoggingA2APushAlertSink()
    job = A2APushJob(
        job_id="job-1",
        task_id="task-1",
        config_id="cfg-1",
        url="https://user:pass@callback.example:8443/a2a?token=callback-secret#frag",
        payload={"ok": True},
        headers={"Authorization": "Bearer raw-header"},
        last_error="Authorization: Bearer sk-live Cookie=session-secret at /Users/alice/.iac-code/settings.yml",
    )

    with caplog.at_level(logging.ERROR, logger="iac_code.a2a.push_worker"):
        await sink.dead_lettered(job)

    record = caplog.records[-1]
    assert record.url == "https://callback.example:8443/a2a"
    assert "callback-secret" not in record.url
    assert "user:pass" not in record.url
    assert "sk-live" not in record.last_error
    assert "session-secret" not in record.last_error
    assert "/Users/alice" not in record.last_error
    assert record.headers == {"Authorization": "[redacted]"}


@pytest.mark.asyncio
async def test_resolved_callback_host_rejects_private_dns(monkeypatch) -> None:
    from iac_code.a2a.push import InvalidPushNotificationConfigError
    from iac_code.a2a.push_worker import _validate_resolved_callback_host

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("10.0.0.1", port))],
    )

    with pytest.raises(InvalidPushNotificationConfigError):
        await _validate_resolved_callback_host("https://callback.example/a2a")
