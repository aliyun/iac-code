from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from iac_code.types.stream_events import TextDeltaEvent


class FakeEventQueue:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class UnknownEvent:
    value: str = "unknown"


@dataclass
class FakeRedisPendingEntry:
    entry_id: str
    fields: dict[str, str]
    consumer: str
    last_delivered_ms: int


def text_delta(text: str) -> TextDeltaEvent:
    return TextDeltaEvent(text=text)


def pending_future() -> asyncio.Future[bool]:
    return asyncio.get_running_loop().create_future()


class FakeAgentLoop:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.prompts: list[str] = []

    async def run_streaming(self, prompt: str):
        self.prompts.append(prompt)
        for event in self.events:
            await asyncio.sleep(0)
            yield event


class FakeRuntime(SimpleNamespace):
    pass


class FakeRedisPushStore:
    def __init__(self, *, xautoclaim_response_shape: str = "tuple") -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: set[tuple[str, str]] = set()
        self.group_positions: dict[tuple[str, str], int] = {}
        self.pending: dict[tuple[str, str], dict[str, FakeRedisPendingEntry]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.acked: list[tuple[str, str, str]] = []
        self.closed = False
        self.now_ms = 0
        self.xautoclaim_response_shape = xautoclaim_response_shape
        self._next_id = 1

    async def xgroup_create(self, name, groupname, id="$", mkstream=False):
        if (name, groupname) in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups.add((name, groupname))
        stream = self.streams.setdefault(name, []) if mkstream else self.streams.get(name, [])
        self.group_positions[(name, groupname)] = len(stream) if id == "$" else 0

    async def xadd(self, name, fields):
        entry_id = f"{self._next_id}-0"
        self._next_id += 1
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        return entry_id

    async def xreadgroup(self, groupname, consumername, streams, count=1, block=0):
        stream = next(iter(streams))
        available = self.streams.setdefault(stream, [])
        group_key = (stream, groupname)
        position = self.group_positions.setdefault(group_key, 0)
        if position >= len(available):
            return []
        entries = available[position : position + count]
        self.group_positions[group_key] = position + len(entries)
        pending = self.pending.setdefault(group_key, {})
        for entry_id, fields in entries:
            pending[entry_id] = FakeRedisPendingEntry(
                entry_id=entry_id,
                fields=dict(fields),
                consumer=consumername,
                last_delivered_ms=self.now_ms,
            )
        return [(stream, entries)]

    async def xautoclaim(self, name, groupname, consumername, min_idle_time, start_id="0-0", count=1):
        pending = self.pending.get((name, groupname), {})
        entries = []
        for entry_id in sorted(pending):
            entry = pending[entry_id]
            if self.now_ms - entry.last_delivered_ms < min_idle_time:
                continue
            entry.consumer = consumername
            entry.last_delivered_ms = self.now_ms
            entries.append((entry.entry_id, entry.fields))
            if len(entries) >= count:
                break
        result = ("0-0", entries, [])
        return list(result) if self.xautoclaim_response_shape == "list" else result

    async def xack(self, name, groupname, *ids):
        self.acked.extend((name, groupname, entry_id) for entry_id in ids)
        pending = self.pending.setdefault((name, groupname), {})
        for entry_id in ids:
            pending.pop(entry_id, None)
        return len(ids)

    async def zadd(self, name, mapping):
        self.zsets.setdefault(name, {}).update(mapping)
        return len(mapping)

    async def zrangebyscore(self, name, min, max, start=None, num=None):
        upper = float(max)
        members = [member for member, score in self.zsets.get(name, {}).items() if score <= upper]
        members.sort(key=lambda member: self.zsets[name][member])
        if start is not None and num is not None:
            return members[start : start + num]
        return members

    async def zrem(self, name, *members):
        values = self.zsets.setdefault(name, {})
        removed = 0
        for member in members:
            if member in values:
                removed += 1
                del values[member]
        return removed

    async def aclose(self):
        self.closed = True


class FakeRequestContext:
    def __init__(
        self,
        *,
        task_id: str = "task-1",
        context_id: str = "ctx-1",
        text: str = "hello",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.task_id = task_id
        self.context_id = context_id
        self.metadata = metadata or {}
        self._text = text
        self.message = SimpleNamespace(metadata=self.metadata)

    def get_user_input(self) -> str:
        return self._text
