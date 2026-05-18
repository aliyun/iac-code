import json

import pytest

from iac_code.a2a.push import A2APushSecretKeyring
from iac_code.a2a.push_queue import (
    A2APushJob,
    A2APushRetryPolicy,
    LocalFileA2APushQueue,
    redact_push_headers,
)

from .fakes import FakeRedisPushStore


@pytest.mark.asyncio
async def test_local_file_push_queue_enqueues_claims_acks_and_persists(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    job = A2APushJob(
        job_id="job-1",
        task_id="task-1",
        config_id="cfg-1",
        url="https://callback.example/a2a",
        payload={"statusUpdate": {"taskId": "task-1"}},
        headers={"Authorization": "Bearer secret"},
    )

    await queue.enqueue(job)
    claimed = await queue.claim()

    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert (tmp_path / "inflight" / "job-1.json").exists()

    await queue.ack("job-1")

    assert not (tmp_path / "inflight" / "job-1.json").exists()


@pytest.mark.asyncio
async def test_local_file_push_queue_retries_and_dead_letters(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    job = A2APushJob(
        job_id="job-1",
        task_id="task-1",
        config_id="cfg-1",
        url="https://callback.example/a2a",
        payload={"ok": True},
        headers={},
    )

    await queue.enqueue(job)
    claimed = await queue.claim()
    assert claimed is not None

    await queue.retry(claimed.with_attempt(attempt=1, next_attempt_at=123.0, last_error="timeout"))
    retried = json.loads((tmp_path / "pending" / "job-1.json").read_text(encoding="utf-8"))

    assert retried["attempt"] == 1
    assert retried["nextAttemptAt"] == 123.0

    claimed_again = await queue.claim(now=122.0)
    assert claimed_again is None

    claimed_again = await queue.claim(now=124.0)
    assert claimed_again is not None
    await queue.dead_letter(claimed_again.with_attempt(attempt=3, last_error="HTTP 400"))

    assert (tmp_path / "dead" / "job-1.json").exists()


@pytest.mark.asyncio
async def test_local_file_push_queue_recovers_expired_inflight_jobs(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path, inflight_timeout_seconds=10.0)
    job = A2APushJob(
        job_id="job-1",
        task_id="task-1",
        config_id="cfg-1",
        url="https://callback.example/a2a",
        payload={"ok": True},
        headers={},
    )

    await queue.enqueue(job)
    claimed = await queue.claim(now=100.0)
    assert claimed is not None
    assert claimed.next_attempt_at == 110.0

    restarted = LocalFileA2APushQueue(tmp_path, inflight_timeout_seconds=10.0)
    assert await restarted.claim(now=109.0) is None

    recovered = await restarted.claim(now=111.0)

    assert recovered is not None
    assert recovered.job_id == "job-1"
    assert (tmp_path / "inflight" / "job-1.json").exists()


@pytest.mark.asyncio
async def test_local_file_push_queue_does_not_persist_sensitive_headers(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path)
    job = A2APushJob(
        job_id="job-1",
        task_id="task-1",
        config_id="cfg-1",
        url="https://callback.example/a2a",
        payload={"ok": True},
        headers={"Authorization": "Bearer secret", "X-A2A-Notification-Token": "token"},
    )

    await queue.enqueue(job)

    raw = (tmp_path / "pending" / "job-1.json").read_text(encoding="utf-8")
    assert "secret" not in raw
    assert "token" not in raw


@pytest.mark.asyncio
async def test_local_file_push_queue_encrypts_jobs_when_keyring_is_configured(tmp_path) -> None:
    queue = LocalFileA2APushQueue(tmp_path / "queue", secret_keyring=A2APushSecretKeyring(tmp_path / "keys.json"))
    job = A2APushJob(
        job_id="job-1",
        task_id="task-1",
        config_id="cfg-1",
        url="https://callback.example/a2a",
        payload={"message": "private task payload"},
    )

    await queue.enqueue(job)

    raw = (tmp_path / "queue" / "pending" / "job-1.json").read_text(encoding="utf-8")
    assert "private task payload" not in raw
    assert "callback.example" not in raw
    assert "iacCodeEncryptedPushJob" in raw
    claimed = await queue.claim()
    assert claimed is not None
    assert claimed.payload == {"message": "private task payload"}
    assert claimed.url == "https://callback.example/a2a"


def test_retry_policy_uses_exponential_backoff_with_cap() -> None:
    policy = A2APushRetryPolicy(initial_delay_seconds=1.0, max_delay_seconds=10.0, jitter_ratio=0.0)

    assert policy.delay_for_attempt(1) == 1.0
    assert policy.delay_for_attempt(2) == 2.0
    assert policy.delay_for_attempt(5) == 10.0


def test_redact_push_headers_removes_credentials() -> None:
    assert redact_push_headers(
        {
            "Authorization": "Bearer secret",
            "X-A2A-Notification-Token": "token",
            "X-Trace": "trace-1",
        }
    ) == {
        "Authorization": "[redacted]",
        "X-A2A-Notification-Token": "[redacted]",
        "X-Trace": "trace-1",
    }


@pytest.mark.asyncio
async def test_redis_push_queue_enqueues_claims_and_acks() -> None:
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

    claimed = await queue.claim(now=100.0)

    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert "secret" not in str(redis.streams)
    await queue.ack("job-1")
    assert redis.acked == [("push", "workers", "1-0")]


@pytest.mark.asyncio
async def test_redis_push_queue_encrypts_jobs_when_keyring_is_configured(tmp_path) -> None:
    from iac_code.a2a.push_queue import RedisStreamsA2APushQueue

    redis = FakeRedisPushStore()
    queue = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-1",
        secret_keyring=A2APushSecretKeyring(tmp_path / "keys.json"),
    )
    await queue.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"message": "private task payload"},
        )
    )

    assert "private task payload" not in str(redis.streams)
    assert "callback.example" not in str(redis.streams)
    claimed = await queue.claim(now=100.0)
    assert claimed is not None
    assert claimed.payload == {"message": "private task payload"}
    assert claimed.url == "https://callback.example/a2a"


@pytest.mark.asyncio
async def test_redis_push_queue_claims_new_jobs_only_once_per_group() -> None:
    from iac_code.a2a.push_queue import RedisStreamsA2APushQueue

    redis = FakeRedisPushStore()
    worker_1 = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-1",
        lease_timeout_ms=1000,
    )
    worker_2 = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-2",
        lease_timeout_ms=1000,
    )
    await worker_1.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"ok": True},
        )
    )

    claimed = await worker_1.claim(now=100.0)
    duplicate = await worker_2.claim(now=100.0)

    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert duplicate is None


@pytest.mark.asyncio
async def test_redis_push_queue_reclaims_pending_jobs_only_after_idle_timeout() -> None:
    from iac_code.a2a.push_queue import RedisStreamsA2APushQueue

    redis = FakeRedisPushStore()
    worker_1 = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-1",
        lease_timeout_ms=1000,
    )
    worker_2 = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-2",
        lease_timeout_ms=1000,
    )
    await worker_1.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"ok": True},
        )
    )

    redis.now_ms = 0
    claimed = await worker_1.claim(now=100.0)
    redis.now_ms = 999
    before_timeout = await worker_2.claim(now=101.0)
    redis.now_ms = 1000
    after_timeout = await worker_2.claim(now=102.0)

    assert claimed is not None
    assert before_timeout is None
    assert after_timeout is not None
    assert after_timeout.job_id == "job-1"


@pytest.mark.asyncio
async def test_redis_push_queue_accepts_list_shaped_xautoclaim_response() -> None:
    from iac_code.a2a.push_queue import RedisStreamsA2APushQueue

    redis = FakeRedisPushStore(xautoclaim_response_shape="list")
    worker_1 = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-1",
        lease_timeout_ms=1000,
    )
    worker_2 = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-2",
        lease_timeout_ms=1000,
    )
    await worker_1.enqueue(
        A2APushJob(
            job_id="job-1",
            task_id="task-1",
            config_id="cfg-1",
            url="https://callback.example/a2a",
            payload={"ok": True},
        )
    )
    assert await worker_1.claim(now=100.0) is not None

    redis.now_ms = 1000
    reclaimed = await worker_2.claim(now=101.0)

    assert reclaimed is not None
    assert reclaimed.job_id == "job-1"


@pytest.mark.asyncio
async def test_redis_push_queue_retries_via_sorted_set_and_promotes_due_jobs() -> None:
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
    job = A2APushJob(
        job_id="job-1",
        task_id="task-1",
        config_id="cfg-1",
        url="https://callback.example/a2a",
        payload={"ok": True},
    )
    await queue.enqueue(job)
    claimed = await queue.claim(now=100.0)
    assert claimed is not None

    await queue.retry(claimed.with_attempt(attempt=1, next_attempt_at=125.0, last_error="timeout"))
    assert redis.zsets["push:retry"]
    assert await queue.claim(now=124.0) is None

    promoted = await queue.claim(now=125.0)

    assert promoted is not None
    assert promoted.attempt == 1
    assert promoted.last_error == "timeout"


@pytest.mark.asyncio
async def test_redis_push_queue_dead_letters_claimed_job() -> None:
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
    job = A2APushJob(
        job_id="job-1",
        task_id="task-1",
        config_id="cfg-1",
        url="https://callback.example/a2a",
        payload={"ok": True},
    )
    await queue.enqueue(job)
    claimed = await queue.claim(now=100.0)
    assert claimed is not None

    await queue.dead_letter(claimed.with_attempt(attempt=3, last_error="HTTP 400"))

    assert redis.streams["push:dead"]
    assert redis.acked == [("push", "workers", "1-0")]


@pytest.mark.asyncio
async def test_redis_push_queue_closes_owned_redis_client() -> None:
    from iac_code.a2a.push_queue import RedisStreamsA2APushQueue

    redis = FakeRedisPushStore()
    queue = RedisStreamsA2APushQueue(
        redis=redis,
        stream="push",
        retry_key="push:retry",
        dead_stream="push:dead",
        consumer_group="workers",
        consumer_name="worker-1",
        owns_redis=True,
    )

    await queue.aclose()

    assert redis.closed is True
