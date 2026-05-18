from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

A2A_ID_MAX_LENGTH = 128
TASK_STATE_CANCELED = "canceled"
TASK_STATE_FAILED = "failed"
TASK_STATE_INPUT_REQUIRED = "input-required"
TASK_STATE_SUBMITTED = "submitted"
TASK_STATE_WORKING = "working"
_A2A_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def validate_protocol_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > A2A_ID_MAX_LENGTH:
        raise ValueError("Invalid A2A id")
    if _A2A_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid A2A id")
    return value


@dataclass
class A2ATaskRecord:
    task_id: str
    context_id: str
    state: str = TASK_STATE_SUBMITTED
    output_text: list[str] = field(default_factory=list)
    active_task: asyncio.Task[Any] | None = None
    expired: bool = False
    created_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_active = time.monotonic()


@dataclass
class A2AContextRecord:
    context_id: str
    session_id: str
    cwd: str
    runtime: Any | None = None
    lock: asyncio.Lock | None = None
    active_task_id: str | None = None
    expired: bool = False
    created_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_active = time.monotonic()
