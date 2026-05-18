from __future__ import annotations

import inspect
import json
import os
import random
import socket
import time
import uuid
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from iac_code.a2a.push_secrets import A2APushSecretKeyring

_REDACTED_HEADERS = {"authorization", "x-a2a-notification-token", "x-api-key", "api-key"}
_ENCRYPTED_JOB_FIELD = "iacCodeEncryptedPushJob"


@dataclass(frozen=True)
class A2APushJob:
    task_id: str
    config_id: str
    url: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    job_id: str = ""
    attempt: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""

    def __post_init__(self) -> None:
        if not self.job_id:
            object.__setattr__(self, "job_id", uuid.uuid4().hex)

    def with_attempt(self, *, attempt: int, next_attempt_at: float | None = None, last_error: str = "") -> A2APushJob:
        return replace(
            self,
            attempt=attempt,
            next_attempt_at=self.next_attempt_at if next_attempt_at is None else next_attempt_at,
            last_error=last_error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "taskId": self.task_id,
            "configId": self.config_id,
            "url": self.url,
            "payload": self.payload,
            "attempt": self.attempt,
            "nextAttemptAt": self.next_attempt_at,
            "lastError": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> A2APushJob:
        return cls(
            job_id=str(data["jobId"]),
            task_id=str(data["taskId"]),
            config_id=str(data["configId"]),
            url=str(data["url"]),
            payload=dict(data["payload"]),
            headers={str(key): str(value) for key, value in dict(data.get("headers") or {}).items()},
            attempt=int(data.get("attempt") or 0),
            next_attempt_at=float(data.get("nextAttemptAt") or 0.0),
            last_error=str(data.get("lastError") or ""),
        )


class A2APushQueue(Protocol):
    async def enqueue(self, job: A2APushJob) -> None: ...

    async def claim(self, *, now: float | None = None) -> A2APushJob | None: ...

    async def ack(self, job_id: str) -> None: ...

    async def retry(self, job: A2APushJob) -> None: ...

    async def dead_letter(self, job: A2APushJob) -> None: ...


@dataclass(frozen=True)
class A2APushRetryPolicy:
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    jitter_ratio: float = 0.2
    max_attempts: int = 5

    def delay_for_attempt(self, attempt: int) -> float:
        base = min(self.max_delay_seconds, self.initial_delay_seconds * (2 ** max(0, attempt - 1)))
        if self.jitter_ratio <= 0:
            return base
        jitter = base * self.jitter_ratio
        return max(0.0, base + random.uniform(-jitter, jitter))


def default_redis_push_consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def require_redis_asyncio() -> Any:
    try:
        return import_module("redis.asyncio")
    except ModuleNotFoundError as exc:
        from iac_code.a2a.transports.base import A2ATransportDependencyError

        raise A2ATransportDependencyError(
            "Redis-backed A2A push delivery requires optional dependencies. Install iac-code[a2a-redis]."
        ) from exc


class LocalFileA2APushQueue:
    def __init__(
        self,
        root: str | Path,
        *,
        inflight_timeout_seconds: float = 300.0,
        secret_keyring: A2APushSecretKeyring | None = None,
    ) -> None:
        self.root = Path(root)
        self._inflight_timeout_seconds = inflight_timeout_seconds
        self._secret_keyring = secret_keyring
        self.pending_dir = self.root / "pending"
        self.inflight_dir = self.root / "inflight"
        self.dead_dir = self.root / "dead"
        for path in (self.pending_dir, self.inflight_dir, self.dead_dir):
            path.mkdir(parents=True, exist_ok=True)
            self._chmod_private(path, directory=True)

    async def enqueue(self, job: A2APushJob) -> None:
        self._write(self.pending_dir / f"{job.job_id}.json", job)

    async def claim(self, *, now: float | None = None) -> A2APushJob | None:
        current = time.time() if now is None else now
        self._recover_expired_inflight(current)
        for path in sorted(self.pending_dir.glob("*.json")):
            job = self._read(path)
            if job.next_attempt_at > current:
                continue
            target = self.inflight_dir / path.name
            leased = job.with_attempt(
                attempt=job.attempt,
                next_attempt_at=current + self._inflight_timeout_seconds,
                last_error=job.last_error,
            )
            path.replace(target)
            self._write(target, leased)
            return self._read(target)
        return None

    async def ack(self, job_id: str) -> None:
        (self.inflight_dir / f"{job_id}.json").unlink(missing_ok=True)

    async def retry(self, job: A2APushJob) -> None:
        (self.inflight_dir / f"{job.job_id}.json").unlink(missing_ok=True)
        self._write(self.pending_dir / f"{job.job_id}.json", job)

    async def dead_letter(self, job: A2APushJob) -> None:
        (self.inflight_dir / f"{job.job_id}.json").unlink(missing_ok=True)
        self._write(self.dead_dir / f"{job.job_id}.json", job)

    def _write(self, path: Path, job: A2APushJob) -> None:
        path.write_text(_serialize_push_job(job, secret_keyring=self._secret_keyring), encoding="utf-8")
        self._chmod_private(path, directory=False)

    def _read(self, path: Path) -> A2APushJob:
        return _deserialize_push_job(path.read_text(encoding="utf-8"), secret_keyring=self._secret_keyring)

    def _recover_expired_inflight(self, now: float) -> None:
        for path in sorted(self.inflight_dir.glob("*.json")):
            job = self._read(path)
            if job.next_attempt_at > now:
                continue
            target = self.pending_dir / path.name
            path.replace(target)
            self._write(
                target,
                job.with_attempt(attempt=job.attempt, next_attempt_at=now, last_error="Delivery lease expired."),
            )

    def _chmod_private(self, path: Path, *, directory: bool) -> None:
        try:
            os.chmod(path, 0o700 if directory else 0o600)
        except OSError:
            return


class RedisStreamsA2APushQueue:
    def __init__(
        self,
        *,
        redis: Any,
        stream: str = "iac-code:a2a:push",
        retry_key: str = "iac-code:a2a:push:retry",
        dead_stream: str = "iac-code:a2a:push:dead",
        consumer_group: str = "iac-code-push",
        consumer_name: str = "",
        lease_timeout_ms: int = 300_000,
        owns_redis: bool = False,
        secret_keyring: A2APushSecretKeyring | None = None,
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._retry_key = retry_key
        self._dead_stream = dead_stream
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name or default_redis_push_consumer_name()
        self._lease_timeout_ms = lease_timeout_ms
        self._owns_redis = owns_redis
        self._secret_keyring = secret_keyring
        self._group_ready = False
        self._claimed_entries: dict[str, str] = {}

    async def enqueue(self, job: A2APushJob) -> None:
        await self._ensure_group()
        await self._redis.xadd(self._stream, {"job": self._serialize(job)})

    async def claim(self, *, now: float | None = None) -> A2APushJob | None:
        await self._ensure_group()
        current = time.time() if now is None else now
        await self._promote_due_retries(current)
        reclaimed = await self._claim_expired()
        if reclaimed is not None:
            return reclaimed
        rows = await self._redis.xreadgroup(
            self._consumer_group,
            self._consumer_name,
            {self._stream: ">"},
            count=1,
            block=0,
        )
        return self._job_from_rows(rows)

    async def ack(self, job_id: str) -> None:
        entry_id = self._claimed_entries.pop(job_id, None)
        if entry_id is not None:
            await self._redis.xack(self._stream, self._consumer_group, entry_id)

    async def retry(self, job: A2APushJob) -> None:
        await self._redis.zadd(self._retry_key, {self._serialize(job): job.next_attempt_at})
        await self.ack(job.job_id)

    async def dead_letter(self, job: A2APushJob) -> None:
        await self._redis.xadd(self._dead_stream, {"job": self._serialize(job)})
        await self.ack(job.job_id)

    async def aclose(self) -> None:
        if not self._owns_redis:
            return
        close = getattr(self._redis, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._redis.xgroup_create(self._stream, self._consumer_group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def _promote_due_retries(self, now: float) -> None:
        members = await self._redis.zrangebyscore(self._retry_key, "-inf", now, start=0, num=10)
        for member in members:
            encoded = _decode_redis_field(member)
            await self._redis.xadd(self._stream, {"job": encoded})
            await self._redis.zrem(self._retry_key, member)

    async def _claim_expired(self) -> A2APushJob | None:
        xautoclaim = getattr(self._redis, "xautoclaim", None)
        if xautoclaim is None:
            return None
        result = await xautoclaim(
            self._stream,
            self._consumer_group,
            self._consumer_name,
            min_idle_time=self._lease_timeout_ms,
            start_id="0-0",
            count=1,
        )
        entries = result[1] if isinstance(result, (list, tuple)) and len(result) >= 2 else []
        return self._job_from_entries(entries)

    def _job_from_rows(self, rows: Any) -> A2APushJob | None:
        for _stream, entries in rows or []:
            job = self._job_from_entries(entries)
            if job is not None:
                return job
        return None

    def _job_from_entries(self, entries: Any) -> A2APushJob | None:
        for entry_id, fields in entries or []:
            encoded = _field_value(fields, "job")
            if encoded is None:
                continue
            job = _deserialize_push_job(_decode_redis_field(encoded), secret_keyring=self._secret_keyring)
            self._claimed_entries[job.job_id] = _decode_redis_field(entry_id)
            return job
        return None

    def _serialize(self, job: A2APushJob) -> str:
        return _serialize_push_job(job, secret_keyring=self._secret_keyring)


def redact_push_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key: "[redacted]" if key.lower() in _REDACTED_HEADERS else value for key, value in headers.items()}


def _decode_redis_field(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _field_value(fields: Any, name: str) -> Any:
    return fields.get(name, fields.get(name.encode("utf-8"))) if isinstance(fields, dict) else None


def _serialize_push_job(job: A2APushJob, *, secret_keyring: A2APushSecretKeyring | None) -> str:
    payload = json.dumps(job.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if secret_keyring is None:
        return payload
    return json.dumps(
        {_ENCRYPTED_JOB_FIELD: {"version": 1, **secret_keyring.encrypt(payload)}},
        sort_keys=True,
        separators=(",", ":"),
    )


def _deserialize_push_job(value: str, *, secret_keyring: A2APushSecretKeyring | None) -> A2APushJob:
    data = json.loads(value)
    encrypted = data.get(_ENCRYPTED_JOB_FIELD) if isinstance(data, dict) else None
    if encrypted is None:
        return A2APushJob.from_dict(data)
    if secret_keyring is None:
        raise ValueError("Encrypted A2A push job requires a configured secret keyring")
    decrypted = secret_keyring.decrypt(dict(encrypted))
    return A2APushJob.from_dict(json.loads(decrypted))
