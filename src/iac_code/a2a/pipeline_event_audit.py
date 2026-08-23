"""按权威性归并 pipeline 事件,避免备份通道把同一逻辑终态重复计数。

终态发布(``pipeline_completed`` / ``pipeline_failed`` / ``pipeline_canceled`` /
``pipeline_handoff_ready``)受关键备份门控保护,同一次取消/切换会在 journal 里留下多条
同 ``eventType`` 记录:

* ``visibility=pending_backup`` 的预写记录(崩溃恢复用);
* ``visibility=committed`` 的权威发布;
* ``visibility=unavailable`` 的降级兜底标记(告知客户端交接不可用);
* 重启恢复路径的幂等补发。

这些记录都是设计需要的,但审计/监控如果直接按 ``eventType`` 计数,一次取消就会被统计成
多次。本模块提供按 ``(eventType, pipelineRunId)`` 归并的入口:只保留权威事件,备份通道
记录不参与计数。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from iac_code.a2a.pipeline_stream import (
    COMMITTED_BACKUP_VISIBILITY,
    PENDING_BACKUP_VISIBILITY,
    UNAVAILABLE_BACKUP_VISIBILITY,
)

#: 受备份门控保护、需要按权威性归并的事件类型。
MERGEABLE_TERMINAL_EVENT_TYPES = frozenset(
    {
        "pipeline_completed",
        "pipeline_failed",
        "pipeline_canceled",
        "pipeline_handoff_ready",
    }
)

#: 只作为备份/降级通道存在的 visibility,永不计入权威计数。
NON_AUTHORITATIVE_VISIBILITIES = frozenset({PENDING_BACKUP_VISIBILITY, UNAVAILABLE_BACKUP_VISIBILITY})


def is_authoritative_pipeline_event(event: Mapping[str, Any]) -> bool:
    """判断单条信封是否为权威事件。

    优先读显式 ``authoritative`` 标记;旧信封没有该字段时回退到 ``visibility``,
    使历史 journal 仍能正确归并。
    """
    authoritative = event.get("authoritative")
    if isinstance(authoritative, bool):
        return authoritative
    return _visibility(event) not in NON_AUTHORITATIVE_VISIBILITIES


def authoritative_pipeline_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """归并出权威事件序列。

    受门控的终态类型按 ``(eventType, pipelineRunId)`` 去重,同组只保留 sequence 最大的
    那一条(committed 发布晚于 pending 预写,恢复补发晚于原发布)。其余事件原样保留,
    仅剔除备份/降级通道记录。返回结果按 ``sequence`` 升序。
    """
    latest_terminal: dict[tuple[str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if not is_authoritative_pipeline_event(event):
            continue
        event_type = _string(event.get("eventType"))
        if event_type not in MERGEABLE_TERMINAL_EVENT_TYPES:
            passthrough.append(dict(event))
            continue
        key = (event_type, _string(event.get("pipelineRunId")))
        previous = latest_terminal.get(key)
        if previous is None or _sequence(event) >= _sequence(previous):
            latest_terminal[key] = dict(event)
    merged: list[dict[str, Any]] = [*passthrough, *latest_terminal.values()]
    merged.sort(key=_sequence)
    return merged


def count_authoritative_pipeline_events(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """统计归并后每种 ``eventType`` 的权威事件条数。"""
    counts: dict[str, int] = {}
    for event in authoritative_pipeline_events(events):
        event_type = _string(event.get("eventType"))
        if event_type:
            counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _visibility(event: Mapping[str, Any]) -> str:
    visibility = event.get("visibility")
    if isinstance(visibility, str):
        return visibility
    data = event.get("data")
    data_visibility = data.get("visibility") if isinstance(data, Mapping) else None
    return data_visibility if isinstance(data_visibility, str) else COMMITTED_BACKUP_VISIBILITY


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _sequence(event: Mapping[str, Any]) -> int:
    value = event.get("sequence")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


__all__ = [
    "MERGEABLE_TERMINAL_EVENT_TYPES",
    "NON_AUTHORITATIVE_VISIBILITIES",
    "authoritative_pipeline_events",
    "count_authoritative_pipeline_events",
    "is_authoritative_pipeline_event",
]
