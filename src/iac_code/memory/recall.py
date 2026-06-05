"""LLM-assisted project memory recall."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iac_code.memory.memory_manager import MemoryManager
from iac_code.memory.project_memory import is_auto_memory_enabled
from iac_code.providers.base import Message
from iac_code.types.stream_events import Usage


@dataclass(frozen=True)
class MemoryRecallResult:
    content: str = ""
    selected_files: list[str] = field(default_factory=list)
    status: str = "skipped"
    usage: Usage | None = None


class MemoryRecallPrefetch:
    """Non-blocking handle for a turn-scoped memory recall task."""

    def __init__(self, task: asyncio.Task[MemoryRecallResult], *, on_cancel: Callable[[], None] | None = None) -> None:
        self._task = task
        self._on_cancel = on_cancel
        self._cancel_recorded = False

    def done(self) -> bool:
        return self._task.done()

    def add_done_callback(self, callback: Callable[[asyncio.Task[MemoryRecallResult]], None]) -> None:
        self._task.add_done_callback(callback)

    def result(self) -> MemoryRecallResult:
        return self._task.result()

    async def wait(self) -> MemoryRecallResult:
        return await self._task

    def cancel(self) -> None:
        if self._task.done():
            return
        if not self._cancel_recorded and self._on_cancel is not None:
            self._on_cancel()
            self._cancel_recorded = True
        self._task.cancel()


@dataclass
class MemoryRecallStats:
    total_side_queries: int = 0
    successful_side_queries: int = 0
    failed_side_queries: int = 0
    cancelled_side_queries: int = 0
    total_selected_files: int = 0
    last_duration_ms: int = 0
    last_status: str = "skipped"
    last_selected_files: list[str] = field(default_factory=list)
    last_side_query_duration_ms: int = 0
    last_side_query_status: str = "skipped"
    last_side_query_selected_files: list[str] = field(default_factory=list)
    last_prompt_preview: str = ""
    last_response_preview: str = ""
    last_prompt_chars: int = 0
    last_response_chars: int = 0
    total_usage: MemoryRecallUsageStats = field(default_factory=lambda: MemoryRecallUsageStats())
    last_usage: MemoryRecallUsageStats = field(default_factory=lambda: MemoryRecallUsageStats())

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_side_queries": self.total_side_queries,
            "successful_side_queries": self.successful_side_queries,
            "failed_side_queries": self.failed_side_queries,
            "cancelled_side_queries": self.cancelled_side_queries,
            "total_selected_files": self.total_selected_files,
            "last_duration_ms": self.last_duration_ms,
            "last_status": self.last_status,
            "last_selected_files": list(self.last_selected_files),
            "last_side_query_duration_ms": self.last_side_query_duration_ms,
            "last_side_query_status": self.last_side_query_status,
            "last_side_query_selected_files": list(self.last_side_query_selected_files),
            "last_prompt_preview": self.last_prompt_preview,
            "last_response_preview": self.last_response_preview,
            "last_prompt_chars": self.last_prompt_chars,
            "last_response_chars": self.last_response_chars,
            "total_usage": self.total_usage.snapshot(),
            "last_usage": self.last_usage.snapshot(),
        }

    def record_usage(self, usage: Usage) -> None:
        if _usage_is_zero(usage):
            return
        self.total_usage.add(usage)
        self.last_usage = MemoryRecallUsageStats.from_usage(usage)


@dataclass
class MemoryRecallUsageStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    recorded_events: int = 0

    @classmethod
    def from_usage(cls, usage: Usage) -> MemoryRecallUsageStats:
        stats = cls()
        stats.add(usage)
        return stats

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def has_recorded_usage(self) -> bool:
        return self.recorded_events > 0

    def add(self, usage: Usage) -> bool:
        if _usage_is_zero(usage):
            return False
        self.input_tokens += int(usage.input_tokens or 0)
        self.output_tokens += int(usage.output_tokens or 0)
        self.cache_read_input_tokens += int(usage.cache_read_input_tokens or 0)
        self.cache_creation_input_tokens += int(usage.cache_creation_input_tokens or 0)
        self.recorded_events += 1
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "total_tokens": self.total_tokens,
            "recorded_events": self.recorded_events,
            "has_recorded_usage": self.has_recorded_usage,
        }


class MemoryRecallService:
    def __init__(
        self,
        memory_manager: MemoryManager,
        provider_manager: Any,
        *,
        max_files: int = 5,
        timeout_seconds: float = 3.0,
        max_bytes_per_file: int = 12_000,
        max_lines_per_file: int = 240,
        is_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._memory_manager = memory_manager
        self._provider_manager = provider_manager
        self._max_files = max_files
        self._timeout_seconds = timeout_seconds
        self._max_bytes_per_file = max_bytes_per_file
        self._max_lines_per_file = max_lines_per_file
        self._is_enabled = is_enabled or is_auto_memory_enabled
        self._stats = MemoryRecallStats()
        self._surfaced_files: set[str] = set()
        self._read_files: set[str] = set()

    def start_prefetch(self, user_input: str) -> MemoryRecallPrefetch | None:
        query = user_input.strip()
        if not query:
            self._record("skipped", time.monotonic(), selected_files=[])
            return None
        task = asyncio.create_task(self._recall(query, timeout_seconds=None))
        return MemoryRecallPrefetch(task, on_cancel=lambda: self._record_cancelled())

    async def recall(self, user_input: str) -> MemoryRecallResult:
        return await self._recall(user_input, timeout_seconds=self._timeout_seconds)

    async def _recall(self, user_input: str, *, timeout_seconds: float | None) -> MemoryRecallResult:
        started = time.monotonic()
        if not self._is_enabled():
            self._record("disabled", started, selected_files=[])
            return MemoryRecallResult(status="disabled")
        try:
            manifest = self._build_manifest()
        except Exception:
            self._stats.total_side_queries += 1
            self._stats.failed_side_queries += 1
            self._record("failed", started, selected_files=[], side_query=True)
            return MemoryRecallResult(status="failed")
        if not manifest:
            self._record("skipped", started, selected_files=[])
            return MemoryRecallResult(status="skipped")

        self._stats.total_side_queries += 1
        response_usage: Usage | None = None
        prompt = self._build_user_prompt(user_input, manifest)
        response_text = ""
        self._stats.last_usage = MemoryRecallUsageStats()
        try:
            completion = self._provider_manager.complete(
                messages=[Message.user(prompt)],
                system=self._build_system_prompt(),
                tools=None,
                max_tokens=512,
                cache_policy="no_explicit_cache",
            )
            if timeout_seconds is None:
                response = await completion
            else:
                response = await asyncio.wait_for(completion, timeout=timeout_seconds)
            usage = getattr(response, "usage", None)
            if isinstance(usage, Usage):
                response_usage = usage
                self._stats.record_usage(usage)
            response_text = str(getattr(response, "text", ""))
            selected_files = self._parse_selected_files(response_text, manifest)
            selected_files = self._filter_unsuppressed_files(selected_files)
        except TimeoutError:
            self._stats.failed_side_queries += 1
            self._record("timeout", started, selected_files=[], prompt=prompt, response=response_text, side_query=True)
            return MemoryRecallResult(status="timeout")
        except Exception:
            self._stats.failed_side_queries += 1
            self._record("failed", started, selected_files=[], prompt=prompt, response=response_text, side_query=True)
            return MemoryRecallResult(status="failed", usage=response_usage)

        content = self._read_selected_files(selected_files)
        self._stats.successful_side_queries += 1
        self._stats.total_selected_files += len(selected_files)
        self._record(
            "success",
            started,
            selected_files=selected_files,
            prompt=prompt,
            response=response_text,
            side_query=True,
        )
        return MemoryRecallResult(
            content=content,
            selected_files=selected_files,
            status="success",
            usage=response_usage,
        )

    def get_stats_snapshot(self) -> dict[str, Any]:
        return self._stats.snapshot()

    def reset_stats(self) -> None:
        self._stats = MemoryRecallStats()
        self._surfaced_files.clear()
        self._read_files.clear()

    def mark_files_surfaced(self, filenames: Iterable[str]) -> None:
        self._surfaced_files.update(_normalize_memory_filenames(filenames))

    def replace_surfaced_files(self, filenames: Iterable[str]) -> None:
        self._surfaced_files = _normalize_memory_filenames(filenames)

    def get_suppressed_files(self) -> set[str]:
        return set(self._surfaced_files | self._read_files)

    def mark_files_read(self, filenames: Iterable[str]) -> None:
        self._read_files.update(_normalize_memory_filenames(filenames))

    def _filter_unsuppressed_files(self, filenames: list[str]) -> list[str]:
        suppressed = self._surfaced_files | self._read_files
        return [filename for filename in filenames if filename not in suppressed]

    def _build_manifest(self) -> dict[str, dict[str, Any]]:
        manifest: dict[str, dict[str, Any]] = {}
        suppressed = self._surfaced_files | self._read_files
        for memory in self._list_memory_metadata():
            name = str(memory.get("name", "")).strip()
            if not name:
                continue
            filename = f"{name}.md"
            if filename in suppressed:
                continue
            manifest[filename] = {
                "name": name,
                "filename": filename,
                "description": str(memory.get("description", "")).strip(),
                "type": str(memory.get("type", "")).strip(),
            }
        return manifest

    def _list_memory_metadata(self) -> list[dict[str, Any]]:
        metadata_loader = getattr(self._memory_manager, "list_memory_metadata", None)
        if callable(metadata_loader):
            return metadata_loader()
        return self._memory_manager.list_memories()

    def _build_user_prompt(self, user_input: str, manifest: dict[str, dict[str, Any]]) -> str:
        lines = [
            "User query:",
            user_input,
            "",
            "Available memory topic files:",
        ]
        for item in sorted(manifest.values(), key=lambda entry: str(entry["filename"])):
            lines.extend(
                [
                    f"- filename: {item['filename']}",
                    f"  type: {item['type']}",
                    f"  description: {item['description']}",
                ]
            )
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        return _RECALL_SYSTEM_PROMPT_TEMPLATE.format(max_files=self._max_files)

    def _parse_selected_files(self, text: str, manifest: dict[str, dict[str, Any]]) -> list[str]:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Recall response must be a JSON object.")
        raw_files = data.get("files", data.get("selected_files", []))
        if not isinstance(raw_files, list):
            raise ValueError("Recall response files must be a list.")

        selected: list[str] = []
        for raw in raw_files:
            filename = str(raw).strip()
            if filename not in manifest:
                continue
            if not _is_safe_topic_filename(filename):
                continue
            if filename in selected:
                continue
            selected.append(filename)
            if len(selected) >= self._max_files:
                break
        return selected

    def _read_selected_files(self, selected_files: list[str]) -> str:
        if not selected_files:
            return ""

        parts = ["# Recalled Memory"]
        for filename in selected_files:
            memory = self._memory_manager.load(Path(filename).stem)
            if not memory:
                continue
            content = _clip_content(
                str(memory.get("content", "")),
                max_bytes=self._max_bytes_per_file,
                max_lines=self._max_lines_per_file,
            )
            parts.append(
                "## {filename}\n[{type}] {description}\n\n{content}".format(
                    filename=filename,
                    type=memory.get("type", ""),
                    description=memory.get("description", ""),
                    content=content,
                )
            )
        return "\n\n".join(parts) if len(parts) > 1 else ""

    def _record(
        self,
        status: str,
        started: float,
        *,
        selected_files: list[str],
        prompt: str = "",
        response: str = "",
        side_query: bool = False,
    ) -> None:
        self._stats.last_status = status
        self._stats.last_selected_files = list(selected_files)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        self._stats.last_duration_ms = duration_ms
        self._stats.last_prompt_preview = _preview(prompt)
        self._stats.last_response_preview = _preview(response)
        self._stats.last_prompt_chars = len(prompt)
        self._stats.last_response_chars = len(response)
        if side_query:
            self._stats.last_side_query_status = status
            self._stats.last_side_query_selected_files = list(selected_files)
            self._stats.last_side_query_duration_ms = duration_ms

    def _record_cancelled(self) -> None:
        self._stats.cancelled_side_queries += 1
        attempted_queries = (
            self._stats.successful_side_queries + self._stats.failed_side_queries + self._stats.cancelled_side_queries
        )
        if self._stats.total_side_queries < attempted_queries:
            self._stats.total_side_queries = attempted_queries
        self._stats.last_status = "cancelled"
        self._stats.last_selected_files = []
        self._stats.last_side_query_status = "cancelled"
        self._stats.last_side_query_selected_files = []
        self._stats.last_side_query_duration_ms = 0


def _is_safe_topic_filename(filename: str) -> bool:
    path = Path(filename)
    return path.name == filename and path.suffix == ".md" and path.stem not in {"", ".", ".."}


def _normalize_memory_filenames(filenames: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for raw in filenames:
        value = str(raw).strip()
        if not value:
            continue
        filename = value if value.endswith(".md") else f"{value}.md"
        if _is_safe_topic_filename(filename):
            normalized.add(filename)
    return normalized


def _usage_is_zero(usage: Usage) -> bool:
    return (
        int(usage.input_tokens or 0) == 0
        and int(usage.output_tokens or 0) == 0
        and int(usage.cache_read_input_tokens or 0) == 0
        and int(usage.cache_creation_input_tokens or 0) == 0
    )


def _preview(text: str, *, limit: int = 2048) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _clip_content(content: str, *, max_bytes: int, max_lines: int) -> str:
    lines = content.splitlines()[:max_lines]
    clipped = "\n".join(lines)
    encoded = clipped.encode("utf-8")
    if len(encoded) <= max_bytes:
        return clipped
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


_RECALL_SYSTEM_PROMPT_TEMPLATE = (
    "Select clearly relevant memory topic files for the user's current query. "
    'Return only JSON in this exact shape: {{"files": ["name.md"]}}. '
    "Use only filenames from the manifest. Select at most {max_files} files. "
    "Return an empty list when nothing is clearly relevant."
)
