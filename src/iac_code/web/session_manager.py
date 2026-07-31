"""Session lifecycle helpers for the local Web workbench."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping

from iac_code.agent.message import (
    RECALLED_MEMORY_MARKER,
    RECALLED_MEMORY_METADATA_TYPE,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    compaction_summary_tail_count,
    is_compaction_summary_message,
)
from iac_code.i18n import _
from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
from iac_code.pipeline.display_names import display_step_name
from iac_code.pipeline.engine.display_replay import DISPLAY_TRANSCRIPT_FILENAME
from iac_code.pipeline.engine.step_spec import AllowUserEscapes
from iac_code.providers.base import ContentBlock
from iac_code.services.permissions.storage import apply_session_rule
from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories
from iac_code.services.session_index import SessionEntry, SessionIndex, _trim_title
from iac_code.services.session_metadata import SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage
from iac_code.types.permissions import PermissionMode, PermissionRuleValue, ToolPermissionContext
from iac_code.utils.state_io import atomic_write_text
from iac_code.web.events import WebEventBuffer, normalize_event_payload
from iac_code.web.images import load_cached_image
from iac_code.web.permissions import (
    PERMISSION_ALWAYS_ALLOW,
    PERMISSION_ALWAYS_DENY,
    WebPendingElicitation,
    WebPendingPermission,
    WebPendingQuestion,
    canceled_elicitation_answer,
    elicitation_schema_from_payload,
    normalize_elicitation_payload,
    normalize_permission_payload,
    normalize_question_payload,
    permission_choice_to_allowed,
    question_answer_from_body,
)
from iac_code.web.settings import is_foreign_normal_visible, is_foreign_pipeline_visible

logger = logging.getLogger(__name__)

WebMode = Literal["normal", "pipeline"]
SESSION_ID_PATTERN_TEXT = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
SESSION_ID_PATTERN = re.compile(SESSION_ID_PATTERN_TEXT)
HIDDEN_REPLAY_METADATA_TYPES = {
    RECALLED_MEMORY_METADATA_TYPE,
    CLEANUP_PROMPT_METADATA_TYPE,
    "internal-skill-context",
}
# complete_step is intentionally NOT hidden: it carries each pipeline step's
# conclusion, which the web transcript renders as a dedicated card.
PIPELINE_HIDDEN_REPLAY_TOOL_NAMES: set[str] = set()
_LEGACY_CLEANUP_CHINESE_PREFIX = "检测到 pipeline rollback 后仍需要清理的云资源"
_LEGACY_CLEANUP_ROLLBACK_PHRASES = ("rollback cleanup required",)
_LEGACY_CLEANUP_RESOURCE_PHRASES = (
    "leftover resource",
    "stack-",
    "delete_complete",
    "仍需要清理",
    "待清理资源",
    "回滚残留资源",
)
_INTERNAL_SKILL_CONTEXT_RE = re.compile(r"^\s*<skill-name>[^<]+</skill-name>(?:\s|\Z)")
WEB_SESSION_ID_PREFIX = "ws~"
WEB_SESSION_METADATA_FILENAME = "web-session.json"
WEB_PROJECT_METADATA_FILENAME = "web-project.json"


def _metadata_string_list(metadata: Mapping[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_from_utc(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str):
        raise ValueError(_("sessionId is invalid"))
    if (
        Path(session_id).is_absolute()
        or PureWindowsPath(session_id).is_absolute()
        or "/" in session_id
        or "\\" in session_id
        or ".." in session_id
        or not SESSION_ID_PATTERN.fullmatch(session_id)
    ):
        raise ValueError(_("sessionId is invalid"))
    return session_id


def _encode_web_session_id(cwd: str, session_id: str) -> str:
    payload = json.dumps([cwd, session_id], separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return "{}{}".format(WEB_SESSION_ID_PREFIX, encoded)


def _decode_web_session_id(ref: str) -> tuple[str, str] | None:
    if not ref.startswith(WEB_SESSION_ID_PREFIX):
        return None
    encoded = ref[len(WEB_SESSION_ID_PREFIX) :]
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        decoded = json.loads(payload.decode("utf-8"))
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not isinstance(decoded[0], str)
        or not isinstance(decoded[1], str)
    ):
        return None
    return decoded[0], decoded[1]


def _is_legacy_recalled_memory_text(text: str | None) -> bool:
    return bool(text and RECALLED_MEMORY_MARKER in text)


def _is_legacy_cleanup_prompt_text(text: str | None) -> bool:
    if not text:
        return False
    if _LEGACY_CLEANUP_CHINESE_PREFIX in text and "DELETE_COMPLETE" in text:
        return True
    lowered = text.lower()
    has_rollback_context = any(phrase in lowered for phrase in _LEGACY_CLEANUP_ROLLBACK_PHRASES)
    has_cleanup_resource_context = any(phrase in lowered for phrase in _LEGACY_CLEANUP_RESOURCE_PHRASES)
    return has_rollback_context and has_cleanup_resource_context


def _is_internal_skill_context_text(text: str | None) -> bool:
    return bool(text and _INTERNAL_SKILL_CONTEXT_RE.match(text))


def _is_pipeline_handoff_context_text(text: str | None) -> bool:
    return bool(text and text.startswith("[Pipeline Handoff Context]"))


def _is_hidden_replay_message(message: Message) -> bool:
    if message.metadata.get("type") in HIDDEN_REPLAY_METADATA_TYPES:
        return True
    text = message.get_text()
    return (
        _is_legacy_recalled_memory_text(text)
        or _is_legacy_cleanup_prompt_text(text)
        or _is_internal_skill_context_text(text)
        or _is_pipeline_handoff_context_text(text)
    )


def reorder_compaction_markers(messages: list[Message]) -> list[Message]:
    """把压缩标记下沉到它的保留尾部之后,还原压缩真实发生的时间位置。

    存储/LLM 上下文里标记排在其保留尾部(recent)之前(有效切片须从标记起始才带得上最近上下文),
    但标记是在压缩那一刻创建的、时间上晚于尾部。``compaction_summary_tail_count`` 记录了标记后属于
    该尾部的消息条数;这里按这个条数把标记向后挪,让可见转录显示在压缩真实触发的位置(手动压缩→
    回合末尾;自动压缩→触发点,可能落在某个回合的工具循环中途)。旧会话无此字段(count=0)时保持
    原位,向后兼容。

    落点不额外吸附到回合边界:自动压缩在工具循环中途触发时,触发点本就在回合中间,这正是它真实
    发生的位置。可见转录侧由 renderCollapsedTurn 把此处的分隔线折进同一个「已处理」组内,而不是把
    回合切成两半(见前端 flushPendingTurn / renderCompactionBoundaryMarker)。
    """
    result = list(messages)
    moved: set[int] = set()
    index = 0
    while index < len(result):
        message = result[index]
        if id(message) not in moved and is_compaction_summary_message(message):
            tail = compaction_summary_tail_count(message)
            if tail > 0:
                moved.add(id(message))
                result.pop(index)
                # 向后跨过 tail 条「真实尾部」消息定位落点。已被前面标记重排进这段区间的其它
                # 压缩标记不计入尾部——它们是重定位的摘要行,不是压缩时保留的最近上下文;若按裸
                # 偏移 index+tail 计,后一个标记会把这些前置标记误当尾部,下沉不到位,导致最后的
                # 手动压缩落在本回合最终回答之前(而非其后的真实触发位置)。
                target = index
                counted = 0
                while target < len(result) and counted < tail:
                    if id(result[target]) not in moved:
                        counted += 1
                    target += 1
                result.insert(target, message)
                continue
        index += 1
    return result


def _pending_request_details(
    pending: dict[str, WebPendingPermission] | dict[str, WebPendingQuestion] | dict[str, WebPendingElicitation],
) -> list[dict[str, Any]]:
    return [request.to_dict() for request in pending.values()]


def _permission_audit_rule(payload: Mapping[str, Any]) -> str | None:
    suggestions = payload.get("suggestions")
    rules: list[str] = []
    if isinstance(suggestions, list):
        for suggestion in suggestions:
            if not isinstance(suggestion, Mapping):
                continue
            tool_name = str(suggestion.get("toolName", "")).strip()
            rule_content = str(suggestion.get("ruleContent", "")).strip()
            if tool_name:
                rules.append("{}({})".format(tool_name, rule_content) if rule_content else tool_name)
    if rules:
        return ", ".join(rules)
    tool_name = str(payload.get("toolName", "")).strip()
    return tool_name or None


def _new_future() -> asyncio.Future[Any]:
    try:
        return asyncio.get_running_loop().create_future()
    except RuntimeError:
        return asyncio.new_event_loop().create_future()


def _set_future_result(future: asyncio.Future[Any], result: Any) -> None:
    if future.done():
        return
    owner_loop = future.get_loop()
    if owner_loop.is_closed():
        return

    def set_if_pending() -> None:
        if not future.done():
            future.set_result(result)

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if owner_loop is running_loop or not owner_loop.is_running():
        try:
            set_if_pending()
        except RuntimeError:
            if not owner_loop.is_closed():
                raise
        return
    try:
        owner_loop.call_soon_threadsafe(set_if_pending)
    except RuntimeError:
        if not owner_loop.is_closed():
            raise


def _normalize_allow_user_escapes(raw: AllowUserEscapes | dict[str, Any] | None) -> AllowUserEscapes:
    if isinstance(raw, AllowUserEscapes):
        return raw
    if isinstance(raw, dict):
        return AllowUserEscapes(
            skill=bool(raw.get("skill", False)),
            command=bool(raw.get("command", False)),
            shell=bool(raw.get("shell", False)),
        )
    return AllowUserEscapes()


def _allow_user_escapes_payload(allow_user_escapes: AllowUserEscapes) -> dict[str, bool]:
    return {
        "skill": allow_user_escapes.skill,
        "command": allow_user_escapes.command,
        "shell": allow_user_escapes.shell,
    }


def _web_session_metadata_path(storage: SessionStorage, cwd: str, session_id: str) -> Path:
    return storage.session_dir(cwd, session_id) / WEB_SESSION_METADATA_FILENAME


def _pipeline_display_replay_path(storage: SessionStorage, cwd: str, session_id: str) -> Path:
    return storage.session_dir(cwd, session_id) / "pipeline" / DISPLAY_TRANSCRIPT_FILENAME


def _is_foreign_session(storage: SessionStorage, cwd: str, session_id: str) -> bool:
    """Return whether the session originated outside the Web workbench.

    Legacy sidecars predate the explicit origin field and therefore retain
    their historical Web-owned semantics.  Once a foreign session gets Web
    display metadata (pin/archive), the explicit marker keeps it foreign.
    """
    path = _web_session_metadata_path(storage, cwd, session_id)
    if not path.exists():
        return True
    return _read_web_session_metadata(storage, cwd, session_id).get("origin") == "foreign"


def _read_web_session_metadata(storage: SessionStorage, cwd: str, session_id: str) -> dict[str, Any]:
    path = _web_session_metadata_path(storage, cwd, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _web_project_metadata_path(storage: SessionStorage, cwd: str) -> Path:
    return storage.project_dir(cwd) / WEB_PROJECT_METADATA_FILENAME


def _read_web_project_metadata(storage: SessionStorage, cwd: str) -> dict[str, Any]:
    for project_dir in storage.project_read_dirs(cwd):
        path = project_dir / WEB_PROJECT_METADATA_FILENAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _normalize_project_metadata(raw: Mapping[str, Any]) -> dict[str, Any]:
    label = raw.get("label")
    return {
        "pinned": raw.get("pinned") is True,
        "pinnedAt": raw.get("pinnedAt") if isinstance(raw.get("pinnedAt"), str) else None,
        "archived": raw.get("archived") is True,
        "hidden": raw.get("hidden") is True,
        "collapsed": raw.get("collapsed") is True,
        "label": label.strip() if isinstance(label, str) and label.strip() else None,
    }


def _safe_web_mode(value: Any, fallback: WebMode = "normal") -> WebMode:
    return value if value in {"normal", "pipeline"} else fallback


def _mode_from_metadata_or_sidecar(
    storage: SessionStorage,
    cwd: str,
    session_id: str,
    metadata_mode: Any,
    fallback: WebMode = "normal",
) -> WebMode:
    mode = _safe_web_mode(metadata_mode, fallback)
    if (
        mode != "pipeline"
        and not isinstance(metadata_mode, str)
        and _pipeline_display_replay_path(storage, cwd, session_id).exists()
    ):
        return "pipeline"
    return mode


def _permission_mode_from_metadata(value: Any) -> PermissionMode | None:
    if not isinstance(value, str):
        return None
    try:
        return PermissionMode(value)
    except ValueError:
        return None


def _camel_case(value: str) -> str:
    pieces = value.split("_")
    return pieces[0] + "".join(piece[:1].upper() + piece[1:] for piece in pieces[1:])


def _camelize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_camel_case(str(key)): _camelize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_camelize(item) for item in value]
    return value


def _tool_result_payload(block: ToolResultBlock) -> dict[str, Any]:
    return {
        "toolUseId": block.tool_use_id,
        "content": block.content,
        "isError": block.is_error,
    }


def _tool_results_by_id(messages: list[Message]) -> dict[str, list[ToolResultBlock]]:
    results: dict[str, list[ToolResultBlock]] = {}
    for message in messages:
        if message.role != "user" or isinstance(message.content, str):
            continue
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                results.setdefault(block.tool_use_id, []).append(block)
    return results


def _pipeline_replay_transcript_ids(replay: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(replay, Mapping):
        return []
    transcript_ids: list[str] = []

    def append_transcript_id(value: Any) -> None:
        if isinstance(value, str) and value and value not in transcript_ids:
            transcript_ids.append(value)

    attempts = replay.get("attempts")
    if not isinstance(attempts, list):
        return transcript_ids
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        append_transcript_id(attempt.get("transcriptId"))
        sub_pipelines = attempt.get("subPipelines")
        if not isinstance(sub_pipelines, Mapping):
            continue
        for sub_pipeline in sub_pipelines.values():
            if not isinstance(sub_pipeline, Mapping):
                continue
            steps = sub_pipeline.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if isinstance(step, Mapping):
                    append_transcript_id(step.get("transcriptId"))
    return transcript_ids


def _is_tool_result_only_message(message: Message) -> bool:
    return (
        message.role == "user"
        and isinstance(message.content, list)
        and bool(message.content)
        and all(isinstance(block, ToolResultBlock) for block in message.content)
    )


def _persisted_message_stable_id(message: Message) -> str | None:
    """Web 消息磁盘行的稳定重建键。

    新 Web runtime 会直接记录实时 ``messageId``；旧流水线 prompt 只有 ``turnId``，
    恢复时仍可重建成 ``user-<turnId>``。旧行无这些元数据时沿用 ``stored-N``。
    """
    message_id = message.metadata.get("messageId")
    if isinstance(message_id, str) and message_id:
        return message_id
    turn_id = message.metadata.get("turnId")
    if message.role == "user" and isinstance(turn_id, str) and turn_id:
        return "user-{}".format(turn_id)
    return None


def _message_text_blocks(message: Message) -> list[str]:
    if isinstance(message.content, str):
        return [message.content] if message.content else []
    return [block.text for block in message.content if isinstance(block, TextBlock) and block.text]


def _is_listable_entry(entry: SessionEntry) -> bool:
    return bool(entry.name or entry.auto_title)


def _is_listable_session(session: WebSession) -> bool:
    return bool(session.title and session.title != "(empty)")


def _label_from_project_storage_name(name: str) -> str:
    parts = [part for part in name.split("-") if part]
    if not parts:
        return "Local project"
    for marker in ("worktrees", "PycharmProjects", "Desktop", "repo"):
        if marker in parts:
            index = len(parts) - 1 - parts[::-1].index(marker)
            label_parts = parts[index + 1 :]
            if label_parts:
                return "-".join(label_parts)
    return "-".join(parts[-2:]) if len(parts) > 1 else parts[0]


def _runtime_settings_payload() -> dict[str, Any]:
    try:
        from iac_code.web.settings import active_provider_summary, aliyun_cloud_summary

        active_provider = active_provider_summary()
        cloud = aliyun_cloud_summary()
    except Exception:
        active_provider = {
            "provider": None,
            "model": None,
            "effort": None,
            "apiBase": None,
            "hasApiKey": False,
        }
        cloud = {
            "configured": False,
            "mode": None,
            "region": None,
            "expiration": None,
        }
    return normalize_event_payload(
        {
            "provider": active_provider.get("provider"),
            "model": active_provider.get("model"),
            "effort": active_provider.get("effort"),
            "activeProvider": active_provider,
            "cloud": cloud,
        }
    )


def _provider_thinking_config(provider: str | None, model: str | None) -> bool | None:
    """Read the configured ``thinkingEnabled`` (True/False/None-if-unset) for a
    (provider, model).

    Mirrors providers.manager exactly (model-level config wins over provider
    top-level). This is only the raw config value, NOT the effective thinking
    state — the family default for an unset value is applied by
    ``resolve_thinking_active``. Any failure to read settings degrades to None.
    """
    if not provider:
        return None
    try:
        from iac_code.config import get_provider_config
        from iac_code.providers.manager import _get_bool_provider_config_value

        provider_cfg = get_provider_config(provider)
        return _get_bool_provider_config_value(provider_cfg, model or "", "thinkingEnabled")
    except Exception:
        return None


def _usage_totals_payload(totals: Any) -> dict[str, int]:
    return {
        "inputTokens": int(getattr(totals, "input_tokens", 0) or 0),
        "outputTokens": int(getattr(totals, "output_tokens", 0) or 0),
        "cacheReadInputTokens": int(getattr(totals, "cache_read_input_tokens", 0) or 0),
        "cacheCreationInputTokens": int(getattr(totals, "cache_creation_input_tokens", 0) or 0),
        "totalTokens": int(getattr(totals, "total_tokens", 0) or 0),
        "recordedEvents": int(getattr(totals, "recorded_events", 0) or 0),
    }


def _context_usage_payload(
    messages: list[Message],
    *,
    model: str | None = None,
    system_prompt_tokens: int = 0,
    tool_definition_tokens: int = 0,
) -> dict[str, Any]:
    """据持久化消息重建上下文用量。

    重建用的 ``ContextManager`` 没有系统提示与工具定义,``get_usage`` 会少算这两项固定开销;
    调用方传入本会话缓存的开销(来自 turn 期间的实时用量,见 ``WebSession`` 缓存字段)以补齐,
    令 /status 与 composer 实时圆环口径一致。缺省 0 时保持旧行为(服务器重启后首轮前的降级态)。
    """
    try:
        from iac_code.services.context_manager import ContextManager

        context_manager = ContextManager(system_prompt="", model=model or "")
        context_manager.load_messages(messages)
        usage = context_manager.get_usage()
        overhead = max(0, int(system_prompt_tokens)) + max(0, int(tool_definition_tokens))
        if overhead:
            usage["system_prompt_tokens"] = max(0, int(system_prompt_tokens))
            usage["tool_definition_tokens"] = max(0, int(tool_definition_tokens))
            usage["total_tokens"] = int(usage.get("total_tokens", 0)) + overhead
            window = int(usage.get("context_window", 0) or 0)
            usage["usage_percent"] = (usage["total_tokens"] / window * 100) if window > 0 else 0
        return _camelize(usage)
    except Exception:
        return {}


def _canceled_permission_answer() -> dict[str, Any]:
    return {"choice": "canceled", "canceled": True}


def _canceled_question_answer() -> dict[str, Any]:
    return {
        "selected_id": "canceled",
        "selected_label": "Canceled",
        "free_text": "",
        "canceled": True,
    }


def compute_replay_sequence(
    *,
    latest_sequence: int,
    floor_sequence: int,
    is_pipeline: bool,
    active_turn: bool,
    active_turn_floor_sequence: int | None,
) -> int:
    """决定重载时 SSE 从哪个 sequence 之后开始回放。

    前端重载会先用存储转录(load_visible_transcript)重建历史,再以 replaySequence 为界
    重连 SSE 回放缓冲区事件。存储行以位置 id(`stored-N`)作键,而实时事件以 uuid/
    `user-<turnId>` 作键,两者无法合并;因此凡是回放「已在存储转录里的已完成轮次」都会
    造成 assistant 消息(含最终答复)重复渲染。故:

    - 无缓冲事件:回到 latest,无可回放。
    - 流水线会话:保持 floor 回放 —— 其转录经稳定 id 去重,依赖回放重建,行为不变。
    - 普通会话进行中:仅从本轮下界回放(本轮消息尚未全部持久化);拿不到下界时回退 floor。
    - 普通会话空闲:存储转录即完整历史,回到 latest 不回放,避免完成轮次重复渲染。
    """
    if latest_sequence <= 0:
        return latest_sequence
    if is_pipeline:
        return floor_sequence - 1
    if active_turn:
        if active_turn_floor_sequence is not None:
            return max(active_turn_floor_sequence, floor_sequence - 1)
        return floor_sequence - 1
    return latest_sequence


class WebTurnAdmissionLock(asyncio.Lock):
    """An asyncio lock that retains its current owner before contention binds a loop."""

    def __init__(self) -> None:
        super().__init__()
        self.owner_loop: asyncio.AbstractEventLoop | None = None
        self.owner_task: asyncio.Task[Any] | None = None

    async def acquire(self) -> Literal[True]:
        acquired = await super().acquire()
        if acquired:
            self.owner_loop = asyncio.get_running_loop()
            self.owner_task = asyncio.current_task()
        return acquired

    def release(self) -> None:
        super().release()
        self.owner_loop = None
        self.owner_task = None


@dataclass
class WebSession:
    session_id: str
    cwd: str
    mode: WebMode
    created_at: str
    updated_at: str
    status: str = "idle"
    title: str = "(empty)"
    git_branch: str | None = None
    pipeline_name: str | None = None
    context_id: str | None = None
    task_id: str | None = None
    allow_user_escapes: AllowUserEscapes = field(default_factory=AllowUserEscapes)
    permission_mode: PermissionMode | None = None
    permission_context: ToolPermissionContext | None = field(default=None, repr=False)
    # 会话级 provider/模型覆盖；为空时回退到全局 activeProvider。
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    # 会话级 thinking 开关；None 时回退到 provider 全局配置（thinkingEnabled）。
    thinking_enabled: bool | None = None
    # /status(据持久化消息重建的 ContextManager)缺系统提示与工具定义 token,会比 composer
    # 实时圆环少算约一万多 token。turn 期间从活跃 context_manager 的实时用量缓存这两项开销,
    # 重载/状态路径据此补齐,令两处口径一致(见 _context_usage_payload)。服务器重启后首轮前
    # 为 0(降级但安全:少算固定开销,首轮结束即收敛)。
    context_system_prompt_tokens: int = 0
    context_tool_definition_tokens: int = 0
    origin: Literal["web", "foreign"] = field(default="web", repr=False)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    draft: str = ""
    pinned: bool = False
    pinned_at: str | None = None
    archived: bool = False
    # 运行中的会话正常结束、且结束时无人在看(无活跃 SSE 订阅)时置为 True；
    # 打开会话(建立 SSE 订阅)时清除。持久化到 sidecar，跨设备共享。
    unread: bool = False
    pending_permissions: dict[str, WebPendingPermission] = field(default_factory=dict)
    pending_questions: dict[str, WebPendingQuestion] = field(default_factory=dict)
    pending_elicitations: dict[str, WebPendingElicitation] = field(default_factory=dict)
    queued_inputs: list[str] = field(default_factory=list)
    active_turn_task: asyncio.Future[Any] | None = field(default=None, repr=False)
    # Local shell commands do not enter agent context and may run concurrently with
    # each other, but they still keep the session alive for archive/delete safety.
    active_local_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    # 仅内存的触发标志:全新会话置 True,供后续 LLM 生成会话标题的路径消费；
    # 重开/外来会话为 False(标题已存在或不归 Web 生成)。
    pending_llm_title: bool = False
    # 仅内存标志:流水线首个回合先设「即时占位」标题让会话立刻出现在侧栏,标记为临时;
    # 随后在途 LLM 结果可覆盖临时标题(apply_llm_auto_title),用户重命名则清除该标记并胜出。
    title_provisional: bool = False
    # 运行中 turn 的 agent_loop 与 turn id，供“引导/立即插队”端点即时注入使用；
    # 由 runtime.start_turn 在 turn 期间设置、finally 清空。
    active_agent_loop: Any | None = field(default=None, repr=False)
    active_turn_id: str | None = field(default=None, repr=False)
    # 运行中 turn 的“回放下界”:该轮第一条事件的前一个 sequence,由 runtime.start_turn 在
    # 发布 user.message 后设置、finally 清空。重载进行中会话时仅回放本轮事件(尚未持久化),
    # 已完成轮次由存储转录提供,避免完成轮次被回放而重复渲染。
    active_turn_floor_sequence: int | None = field(default=None, repr=False)
    # A bounded SSE buffer cannot prove whether a turn input was already published once
    # that event rolls over. The app keeps this short-lived ownership set until all
    # cancellation/error restoration paths for the active task have completed.
    consumed_turn_ids: set[str] = field(default_factory=set, repr=False)
    turn_admission_lock: WebTurnAdmissionLock = field(default_factory=WebTurnAdmissionLock, repr=False)
    # Pipeline interrupts target an already-running pipeline task, so they must not
    # contend for the turn lifecycle reservation. This lock only serializes judges.
    pipeline_interrupt_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    events: WebEventBuffer = field(init=False, repr=False)
    debug_enabled: bool = False
    read_only: bool = False

    def __post_init__(self) -> None:
        self.events = WebEventBuffer(self.session_id)

    @property
    def web_session_id(self) -> str:
        return _encode_web_session_id(self.cwd, self.session_id)

    def _thinking_effective(self, runtime: dict[str, Any] | None = None) -> bool:
        """Whether the next turn would actually think: session override wins, else
        the configured provider value, else the provider(+model) family default.

        Resolved through ``resolve_thinking_active`` so the toggle matches the wire
        behavior — notably DashScope/Kimi/Zhipu think by default even when unset.
        """
        rt = runtime if runtime is not None else _runtime_settings_payload()
        provider = self.provider or rt.get("provider")
        model = self.model or rt.get("model")
        effort = self.effort if self.effort is not None else rt.get("effort")
        configured = self.thinking_enabled
        if configured is None:
            configured = _provider_thinking_config(provider, model)
        try:
            from iac_code.providers.thinking import resolve_thinking_active

            return resolve_thinking_active(provider, model, configured, effort)
        except Exception:
            return bool(configured)

    def to_dict(self) -> dict[str, Any]:
        latest_sequence = self.events.latest_sequence
        has_buffered_events = latest_sequence > 0
        active_turn = self.active_turn_task is not None and not self.active_turn_task.done()
        replay_sequence = compute_replay_sequence(
            latest_sequence=latest_sequence,
            floor_sequence=self.events.floor_sequence,
            is_pipeline=self.mode == "pipeline" or self.context_id is not None,
            active_turn=active_turn,
            active_turn_floor_sequence=self.active_turn_floor_sequence,
        )
        runtime = _runtime_settings_payload()
        permission_mode = (
            self.permission_context.mode if self.permission_context is not None else self.permission_mode
        ) or PermissionMode.DEFAULT
        pipeline = {
            "pipelineName": self.pipeline_name,
            "pipelineRunId": self.context_id,
            "contextId": self.context_id,
            "taskId": self.task_id,
            "lastSequence": None,
            "currentStep": None,
            "candidate": None,
            "waitingInput": None,
            "cleanupStatus": None,
            "cleanup": None,
            "handoffStatus": None,
            "handoff": None,
            "warningHistory": [],
            "rollbackHistory": [],
            "candidateRestarts": [],
        }
        return {
            "webSessionId": self.web_session_id,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "mode": self.mode,
            "pipelineName": self.pipeline_name,
            "contextId": self.context_id,
            "taskId": self.task_id,
            "allowUserEscapes": _allow_user_escapes_payload(self.allow_user_escapes),
            "status": self.status,
            "title": self.title,
            "pinned": self.pinned,
            "pinnedAt": self.pinned_at,
            "archived": self.archived,
            "unread": self.unread,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "draft": self.draft,
            "permissionMode": permission_mode.value,
            # thinkingEnabled 是会话级覆盖（None=跟随 provider 默认）；thinkingEffective 是
            # 本回合真正会生效的思考开关（override 优先，否则解析 provider(+model) 默认），
            # 供 composer 切换按钮初始渲染，避免旧会话 override=None 一律错误显示为“关”。
            "thinkingEnabled": self.thinking_enabled,
            "thinkingEffective": self._thinking_effective(runtime),
            "latestSequence": latest_sequence,
            "replaySequence": replay_sequence,
            "hasBufferedEvents": has_buffered_events,
            "currentTurnActive": active_turn or self.turn_lock.locked(),
            "debugEnabled": self.debug_enabled,
            "readOnly": self.read_only,
            "pendingPermissionCount": len(self.pending_permissions),
            "pendingQuestionCount": len(self.pending_questions),
            "pendingElicitationCount": len(self.pending_elicitations),
            "pendingPermissions": _pending_request_details(self.pending_permissions),
            "pendingQuestions": _pending_request_details(self.pending_questions),
            "pendingElicitations": _pending_request_details(self.pending_elicitations),
            # 队列消息的完整内容（不只是数量），使前端在 loadSession/resync 重建状态时能恢复
            # “排队中”列表——否则繁忙轮次里权限确认触发 resync 会把排队清空，二者无法共存。
            "queuedInputs": [{"text": item} for item in self.queued_inputs],
            # provider/模型优先返回会话级覆盖，未设置时回退到全局值。
            "provider": self.provider or runtime["provider"],
            "model": self.model or runtime["model"],
            "effort": self.effort if self.effort is not None else runtime["effort"],
            "activeProvider": runtime["activeProvider"],
            "cloud": runtime["cloud"],
            "turn": {
                "active": active_turn or self.turn_lock.locked(),
                "status": "running" if active_turn or self.turn_lock.locked() else self.status,
                "pendingPermissions": len(self.pending_permissions),
                "pendingQuestions": len(self.pending_questions),
                "pendingElicitations": len(self.pending_elicitations),
                "queuedInputs": len(self.queued_inputs),
            },
            "pipeline": pipeline,
            "context": {
                "cwd": self.cwd,
                "latestSequence": latest_sequence,
                "replaySequence": replay_sequence,
            },
        }


class QueuedInputActionError(Exception):
    """排队项逐条操作(删除/编辑/引导)的校验失败，携带 HTTP 状态码供端点映射。"""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class WebSessionManager:
    """Create and list Web sessions while preserving CLI/REPL session storage."""

    def __init__(self, *, projects_dir: Path | str | None = None, cwd: Path | str | None = None) -> None:
        self.cwd = Path(cwd or os.environ.get("IAC_CODE_CWD", os.getcwd())).expanduser().resolve()
        resolved_projects_dir = Path(projects_dir) if projects_dir is not None else None
        self.storage = SessionStorage(projects_dir=resolved_projects_dir)
        self.index = SessionIndex(projects_dir=resolved_projects_dir)
        self._sessions: dict[tuple[str, str], WebSession] = {}
        self._session_lifecycle_epoch = 0
        self._session_mutation_epochs: dict[tuple[str, str], int] = {}
        # 请求级缓存(仅在 batch_reads() 窗口内生效):外来会话可见性开关本是一次请求内
        # 的常量,却曾被 _foreign_hidden 按每会话、每趟装配重复读取 settings.yml(数千次
        # YAML 解析 + 每次 get_config_dir 的 mkdir/chmod),是首屏 ~4s 卡顿主因。窗口内把
        # 开关与每会话判定各缓存一次;窗口外仍读磁盘最新状态,语义不变。
        self._batch_depth = 0
        self._foreign_flags: tuple[bool, bool] | None = None
        self._foreign_hidden_cache: dict[tuple[str, str], bool] | None = None

    @property
    def session_lifecycle_epoch(self) -> int:
        return self._session_lifecycle_epoch

    def session_reference_mutated_since(self, ref: str, session: WebSession, epoch: int) -> bool:
        web_session_key = _decode_web_session_id(ref)
        if web_session_key is not None:
            return self._session_mutation_epochs.get(web_session_key, 0) > epoch
        if not ref:
            return False
        exact_match = session.session_id == ref
        return any(
            mutation_epoch > epoch and (session_id == ref if exact_match else session_id.startswith(ref))
            for (_, session_id), mutation_epoch in self._session_mutation_epochs.items()
        )

    def _record_session_lifecycle_mutation(self, cwd: str, session_id: str) -> None:
        self._session_lifecycle_epoch += 1
        self._session_mutation_epochs[(cwd, session_id)] = self._session_lifecycle_epoch

    def create_session(
        self,
        *,
        cwd: str | None = None,
        mode: WebMode = "normal",
        pipeline_name: str | None = None,
        context_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        allow_user_escapes: AllowUserEscapes | dict[str, Any] | None = None,
        permission_mode: PermissionMode | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> WebSession:
        if mode not in {"normal", "pipeline"}:
            raise ValueError(_("mode must be normal or pipeline"))
        actual_cwd = str(Path(cwd).expanduser().resolve()) if cwd else str(self.cwd)
        actual_session_id = _validate_session_id(session_id) if session_id is not None else uuid.uuid4().hex
        expected_project_dir = self.storage.session_dir(actual_cwd, "__session_probe__").parent.resolve(strict=False)
        session_dir = self.storage.session_dir(actual_cwd, actual_session_id).resolve(strict=False)
        try:
            session_dir.relative_to(expected_project_dir)
        except ValueError as exc:
            raise ValueError(_("sessionId is invalid")) from exc
        session_key = (actual_cwd, actual_session_id)
        existing = self._sessions.get(session_key)
        if existing is not None:
            return existing

        now = _utc_now()
        storage_existed = self.storage.exists(actual_cwd, actual_session_id)
        origin: Literal["web", "foreign"] = (
            "foreign" if storage_existed and _is_foreign_session(self.storage, actual_cwd, actual_session_id) else "web"
        )
        metadata = self.storage.read_metadata(actual_cwd, actual_session_id)
        if not storage_existed:
            self.storage.save(actual_cwd, actual_session_id, [])
            metadata = self.storage.read_metadata(actual_cwd, actual_session_id)
        if metadata is None:
            write_session_metadata(
                self.storage.session_dir(actual_cwd, actual_session_id),
                SessionMetadata(
                    session_id=actual_session_id,
                    cwd=actual_cwd,
                    created_at=now,
                    updated_at=now,
                ),
            )
            metadata = self.storage.read_metadata(actual_cwd, actual_session_id)
        web_metadata = _read_web_session_metadata(self.storage, actual_cwd, actual_session_id)
        restored_mode = _mode_from_metadata_or_sidecar(
            self.storage,
            actual_cwd,
            actual_session_id,
            web_metadata.get("mode"),
            mode,
        )
        restored_pipeline_name = (
            web_metadata.get("pipelineName") if isinstance(web_metadata.get("pipelineName"), str) else pipeline_name
        )
        restored_context_id = (
            web_metadata.get("contextId") if isinstance(web_metadata.get("contextId"), str) else context_id
        )
        restored_task_id = web_metadata.get("taskId") if isinstance(web_metadata.get("taskId"), str) else task_id
        restored_escapes = web_metadata.get("allowUserEscapes")
        restored_permission_mode = _permission_mode_from_metadata(web_metadata.get("permissionMode"))
        if restored_permission_mode is None and permission_mode is not None:
            restored_permission_mode = (
                permission_mode
                if isinstance(permission_mode, PermissionMode)
                else _permission_mode_from_metadata(permission_mode)
            )
        restored_provider = web_metadata.get("provider") if isinstance(web_metadata.get("provider"), str) else provider
        restored_model = web_metadata.get("model") if isinstance(web_metadata.get("model"), str) else model
        restored_effort = web_metadata.get("effort") if isinstance(web_metadata.get("effort"), str) else effort
        restored_thinking_enabled = (
            web_metadata.get("thinkingEnabled") if isinstance(web_metadata.get("thinkingEnabled"), bool) else None
        )
        restored_pinned = web_metadata.get("pinned") is True
        restored_pinned_at = web_metadata.get("pinnedAt") if isinstance(web_metadata.get("pinnedAt"), str) else None
        restored_archived = web_metadata.get("archived") is True
        session = WebSession(
            session_id=actual_session_id,
            cwd=actual_cwd,
            mode=restored_mode,
            pipeline_name=restored_pipeline_name,
            context_id=restored_context_id,
            task_id=restored_task_id,
            allow_user_escapes=_normalize_allow_user_escapes(restored_escapes or allow_user_escapes),
            permission_mode=restored_permission_mode,
            provider=restored_provider,
            model=restored_model,
            effort=restored_effort,
            thinking_enabled=restored_thinking_enabled,
            origin=origin,
            status="idle",
            title=metadata.name if metadata and metadata.name else "(empty)",
            git_branch=metadata.git_branch if metadata else None,
            created_at=(metadata.created_at if metadata and metadata.created_at else now),
            updated_at=(metadata.updated_at if metadata and metadata.updated_at else now),
            pinned=restored_pinned,
            pinned_at=restored_pinned_at,
            archived=restored_archived,
        )
        session.pending_llm_title = not storage_existed
        self._sessions[session_key] = session
        if not storage_existed:
            self._record_session_lifecycle_mutation(actual_cwd, actual_session_id)
        self.persist_web_metadata(session)
        return session

    def _foreign_visibility_flags(self) -> tuple[bool, bool]:
        """返回 ``(normal_visible, pipeline_visible)``,batch_reads 窗口内只读一次 settings.yml。

        两个开关本是一次请求内的常量;在 batch 窗口内合并缓存,避免装配循环里成千上万次
        重复的 YAML 解析与 ``get_config_dir`` 的 mkdir/chmod。窗口外每次读磁盘最新状态。
        """
        if self._batch_depth > 0:
            if self._foreign_flags is None:
                self._foreign_flags = (is_foreign_normal_visible(), is_foreign_pipeline_visible())
            return self._foreign_flags
        return is_foreign_normal_visible(), is_foreign_pipeline_visible()

    def _foreign_hidden(self, cwd: str, session_id: str) -> bool:
        """列表/搜索装配用:外来会话按「其他」开关隐藏时返回 True。

        纯文件系统判定,须先于 ``_from_entry`` 调用以短路,避免被隐藏的外来会话被缓存进
        ``_sessions`` 后又被第二个循环重新纳入。web 会话(有 ``web-session.json``)恒返回
        ``False``;其余按其是否为流水线(有 ``pipeline/display.jsonl``)分别看对应开关。

        同一会话会在四趟首屏装配里被反复判定,故在 batch_reads 窗口内按 (cwd, session_id)
        缓存结果,把每会话两次路径 ``exists()`` 与开关读取压到每请求一次。
        """
        cache = self._foreign_hidden_cache if self._batch_depth > 0 else None
        if cache is not None:
            cached = cache.get((cwd, session_id))
            if cached is not None:
                return cached
        if not _is_foreign_session(self.storage, cwd, session_id):
            result = False
        elif _pipeline_display_replay_path(self.storage, cwd, session_id).exists():
            result = not self._foreign_visibility_flags()[1]
        else:
            result = not self._foreign_visibility_flags()[0]
        if cache is not None:
            cache[(cwd, session_id)] = result
        return result

    def is_session_read_only(self, session: "WebSession") -> bool:
        """post_message 只读守卫:外来 pipeline 恒只读;外来普通仅在开关② 关闭时只读;
        web 会话恒可写。权威用文件系统判定。"""
        if not _is_foreign_session(self.storage, session.cwd, session.session_id):
            return False
        if _pipeline_display_replay_path(self.storage, session.cwd, session.session_id).exists():
            return True
        return not is_foreign_normal_visible()

    @contextmanager
    def batch_reads(self) -> Iterator[None]:
        """在一次请求内复用同一份全项目扫描结果,避免重复 stat + 解析每个会话文件。

        首页 ``/api/sessions`` 一次请求里连续调用 list_sessions_page、
        list_pinned_sessions、list_session_projects、list_pinned_projects,
        它们各自会触发一次 ``list_all_projects_page(limit=None)`` 全量扫描。用底层
        index 的快照把这几次扫描压到一次;仅作用于本代码块,块外读写仍取磁盘最新状态。
        """
        self._batch_depth += 1
        if self._batch_depth == 1:
            self._foreign_flags = None
            self._foreign_hidden_cache = {}
        try:
            with self.index.snapshot():
                yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self._foreign_flags = None
                self._foreign_hidden_cache = None

    def list_sessions_page(self, *, limit: int | None = None) -> tuple[list[WebSession], int]:
        entries, total = self._list_entries_for_visible_limit(limit)
        sessions: list[WebSession] = []
        for entry in entries:
            # 外来会话按开关隐藏须先于 _from_entry 短路(避免缓存进 _sessions)。
            if self._foreign_hidden(entry.cwd, entry.session_id):
                continue
            # 放宽前置门槛:外来会话常无 name/auto_title,需放行到 _from_entry 取兜底标题。
            if not _is_listable_entry(entry) and not _is_foreign_session(self.storage, entry.cwd, entry.session_id):
                continue
            session = self._from_entry(entry)
            if session.archived:
                continue
            if not _is_listable_entry(entry) and not _is_listable_session(session):
                continue
            sessions.append(session)
        listed_keys = {(session.cwd, session.session_id) for session in sessions}
        for session in sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True):
            session_key = (session.cwd, session.session_id)
            if session_key in listed_keys:
                continue
            if self._foreign_hidden(session.cwd, session.session_id):
                continue
            if session.archived:
                continue
            # 与分组视图(1191 行)/搜索(1291 行)一致:过滤仍为「(empty)」的空会话
            # (如未拿到自动标题的售卖流水线会话),避免其泄漏进侧边栏。
            if not _is_listable_session(session):
                continue
            sessions.append(session)
            listed_keys.add(session_key)
        if sessions:
            if limit is not None:
                return sessions[:limit], max(total, len(sessions))
            return sessions, max(total, len(sessions))
        # 兜底分支:主循环无任何可列会话时的「最后手段」,刻意不套 _is_listable_session,
        # 避免侧边栏彻底空白(依赖此行为的 resume / 跨项目 ID 去重用例)。
        all_sessions = [
            session
            for session in sorted(self._sessions.values(), key=lambda session: session.session_id)
            if not session.archived and not self._foreign_hidden(session.cwd, session.session_id)
        ]
        if limit is not None:
            return all_sessions[:limit], max(total, len(all_sessions))
        return all_sessions, max(total, len(all_sessions))

    def list_sessions(self) -> list[WebSession]:
        sessions, _total = self.list_sessions_page()
        return sessions

    def loaded_sessions(self) -> tuple[WebSession, ...]:
        """Return the in-memory sessions that may own process-local work."""
        return tuple(self._sessions.values())

    def list_project_sessions(self, cwd: str, *, limit: int | None = None) -> tuple[list[WebSession], int]:
        sessions: list[WebSession] = []
        for entry in self.index.list_for_cwd(cwd):
            if self._foreign_hidden(entry.cwd, entry.session_id):
                continue
            if not _is_listable_entry(entry) and not _is_foreign_session(self.storage, entry.cwd, entry.session_id):
                continue
            session = self._from_entry(entry)
            if session.archived or session.pinned:
                continue
            if not _is_listable_entry(entry) and not _is_listable_session(session):
                continue
            sessions.append(session)
        listed_keys = {(session.cwd, session.session_id) for session in sessions}
        for session in sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True):
            session_key = (session.cwd, session.session_id)
            if session.cwd != cwd or session_key in listed_keys:
                continue
            if self._foreign_hidden(session.cwd, session.session_id):
                continue
            if session.archived or session.pinned:
                continue
            # 与分组视图(1191 行)/搜索(1291 行)一致:过滤仍为「(empty)」的空会话
            # (如未拿到自动标题的售卖流水线会话),避免其泄漏进展开项目列表。
            if not _is_listable_session(session):
                continue
            sessions.insert(0, session)
            listed_keys.add(session_key)
        total = len(sessions)
        if limit is not None:
            return sessions[:limit], total
        return sessions, total

    def read_project_metadata(self, cwd: str) -> dict[str, Any]:
        return _normalize_project_metadata(_read_web_project_metadata(self.storage, cwd))

    def update_project_metadata(
        self,
        cwd: str,
        *,
        pinned: bool | None = None,
        archived: bool | None = None,
        hidden: bool | None = None,
        collapsed: bool | None = None,
        label: str | None = None,
        clear_label: bool = False,
    ) -> dict[str, Any]:
        current = _normalize_project_metadata(_read_web_project_metadata(self.storage, cwd))
        if pinned is not None:
            if pinned and not current["pinned"]:
                current["pinnedAt"] = _utc_now()
            current["pinned"] = pinned
            if pinned:
                # A pinned project is shown in the pinned area, so it is neither archived nor removed.
                current["archived"] = False
                current["hidden"] = False
            else:
                current["pinnedAt"] = None
        if archived is not None:
            current["archived"] = archived
            if archived:
                current["pinned"] = False
                current["pinnedAt"] = None
        if hidden is not None:
            current["hidden"] = hidden
            if hidden:
                current["pinned"] = False
                current["pinnedAt"] = None
        if collapsed is not None:
            current["collapsed"] = collapsed
        if clear_label:
            current["label"] = None
        elif label is not None:
            current["label"] = label.strip() or None

        path = _web_project_metadata_path(self.storage, cwd)
        payload = json.dumps({"schemaVersion": 1, **current, "updatedAt": _utc_now()}, ensure_ascii=False)
        atomic_write_text(path, payload + "\n")
        return current

    def _collect_project_groups(self, per_project_limit: int) -> dict[str, dict[str, Any]]:
        entries, _total = self.index.list_all_projects_page(limit=None)
        groups: dict[str, dict[str, Any]] = {}
        listed_keys: set[tuple[str, str]] = set()

        for entry in entries:
            if not entry.cwd:
                continue
            if self._foreign_hidden(entry.cwd, entry.session_id):
                continue
            group = groups.setdefault(
                entry.cwd, {"cwd": entry.cwd, "sessions": [], "total": 0, "archived_total": 0, "_sort_mtime": 0.0}
            )
            group["_sort_mtime"] = max(float(group["_sort_mtime"]), entry.mtime)
            listed_keys.add((entry.cwd, entry.session_id))
            session = self._from_entry(entry)
            # 常规条目走 _is_listable_entry 快速判定;index 判为不可列时(如「(empty)」流水线条目)
            # 再看 _from_entry 从 web sidecar 兜底出的标题是否让会话可列,避免流水线会话被漏掉。
            if not _is_listable_entry(entry) and not _is_listable_session(session):
                continue
            if session.archived:
                # 记录本项目的已归档会话数,供 list_session_projects 决定「归档后隐藏空卡片」。
                group["archived_total"] += 1
                continue
            if session.pinned:
                continue
            group["total"] += 1
            if len(group["sessions"]) < per_project_limit:
                group["sessions"].append(session)

        for session in sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True):
            session_key = (session.cwd, session.session_id)
            if session_key in listed_keys:
                continue
            if self._foreign_hidden(session.cwd, session.session_id):
                continue
            group = groups.setdefault(
                session.cwd, {"cwd": session.cwd, "sessions": [], "total": 0, "archived_total": 0, "_sort_mtime": 0.0}
            )
            group["_sort_mtime"] = max(float(group["_sort_mtime"]), _timestamp_from_utc(session.updated_at))
            listed_keys.add(session_key)
            if not _is_listable_session(session):
                continue
            if session.archived:
                group["archived_total"] += 1
                continue
            if session.pinned:
                continue
            group["total"] += 1
            group["sessions"].insert(0, session)
            group["sessions"] = group["sessions"][:per_project_limit]

        represented_storage_names = {
            project_dir.name for cwd in groups for project_dir in self.storage.project_read_dirs(cwd)
        }
        for project_dir in self.index.list_project_directories():
            if project_dir.name in represented_storage_names:
                continue
            try:
                sort_mtime = project_dir.stat().st_mtime
            except OSError:
                sort_mtime = 0.0
            groups[project_dir.name] = {
                "cwd": project_dir.name,
                "label": _label_from_project_storage_name(project_dir.name),
                "sessions": [],
                "total": 0,
                "archived_total": 0,
                "_sort_mtime": sort_mtime,
            }

        for group in groups.values():
            meta = self.read_project_metadata(group["cwd"])
            group["pinned"] = meta["pinned"]
            group["pinnedAt"] = meta["pinnedAt"]
            group["archived"] = meta["archived"]
            group["hidden"] = meta["hidden"]
            group["collapsed"] = meta["collapsed"]
            if meta["label"]:
                group["label"] = meta["label"]
        return groups

    def search_sessions(
        self,
        query: str,
        *,
        limit: int = 50,
        include_archived: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fuzzy-search sessions across *all* projects for the spotlight palette.

        Enumeration mirrors :meth:`_collect_project_groups` (index entries plus
        the in-memory ``_sessions`` fallback, de-duplicated), but with no
        per-project cap and without dropping pinned sessions — a pinned chat is
        still searchable. Archived sessions are excluded unless
        ``include_archived`` is set. An empty ``query`` matches everything
        (used for the palette's default "recent" state). Each result is the
        session's ``to_dict()`` payload with an extra ``projectLabel`` key.
        Results are sorted by ``updatedAt`` descending and truncated to
        ``limit``; the returned total is the pre-truncation match count.
        """
        normalized = (query or "").strip().lower()
        entries, _total = self.index.list_all_projects_page(limit=None)
        listed_keys: set[tuple[str, str]] = set()
        label_cache: dict[str, str] = {}

        def project_label(cwd: str) -> str:
            if cwd not in label_cache:
                meta = self.read_project_metadata(cwd)
                label_cache[cwd] = meta.get("label") or os.path.basename(cwd.rstrip("/")) or cwd
            return label_cache[cwd]

        def matches(session: WebSession, label: str) -> bool:
            if not normalized:
                return True
            cwd = session.cwd or ""
            haystacks = (session.title or "", label, os.path.basename(cwd.rstrip("/")), cwd)
            return any(normalized in value.lower() for value in haystacks)

        collected: list[WebSession] = []

        for entry in entries:
            if not entry.cwd:
                continue
            if self._foreign_hidden(entry.cwd, entry.session_id):
                continue
            listed_keys.add((entry.cwd, entry.session_id))
            session = self._from_entry(entry)
            if not _is_listable_entry(entry) and not _is_listable_session(session):
                continue
            if session.archived and not include_archived:
                continue
            collected.append(session)

        for session in self._sessions.values():
            session_key = (session.cwd, session.session_id)
            if session_key in listed_keys:
                continue
            if self._foreign_hidden(session.cwd, session.session_id):
                continue
            listed_keys.add(session_key)
            if not _is_listable_session(session):
                continue
            if session.archived and not include_archived:
                continue
            collected.append(session)

        matched = [session for session in collected if matches(session, project_label(session.cwd))]
        matched.sort(key=lambda item: item.updated_at or "", reverse=True)
        total = len(matched)
        limited = matched if limit is None else matched[: max(0, limit)]

        results: list[dict[str, Any]] = []
        for session in limited:
            payload = session.to_dict()
            payload["projectLabel"] = project_label(session.cwd)
            results.append(payload)
        return results, total

    @staticmethod
    def _project_public_payload(group: dict[str, Any]) -> dict[str, Any]:
        return {
            "cwd": group["cwd"],
            "label": group.get("label"),
            "sessions": group["sessions"],
            "total": group["total"],
            "hasMore": int(group["total"]) > len(group["sessions"]),
            "pinned": bool(group.get("pinned")),
            "pinnedAt": group.get("pinnedAt"),
            "archived": bool(group.get("archived")),
            "collapsed": bool(group.get("collapsed")),
        }

    def list_session_projects(
        self,
        *,
        project_limit: int | None = None,
        per_project_limit: int = 5,
        include_empty: bool = False,
    ) -> tuple[list[dict[str, Any]], int, int]:
        groups = self._collect_project_groups(per_project_limit)
        active = [
            group
            for group in groups.values()
            if not group.get("pinned")
            and not group.get("archived")
            and not group.get("hidden")
            # 侧栏(include_empty=False):空项目不显示,仅当有可见会话(total>0)才出现。两个
            # 「显露外来会话」开关只在有会话可查看/接着跑的场景下把外来会话计入 total;一旦计入,
            # 项目就不再是空项目而自然出现。归档后残留的空卡、纯外来(开关关)、真·空目录都隐藏。
            # include_empty=True 供记忆/插件的项目选择器解析用:除归档后残留的空卡(archived_total>0)
            # 外,仍覆盖无会话项目,保持这些面板可管理空项目的既有行为。
            and (int(group.get("total") or 0) > 0 or (include_empty and int(group.get("archived_total") or 0) == 0))
        ]
        projects = sorted(
            active,
            key=lambda group: (
                -float(group.get("_sort_mtime") or 0.0),
                str(group.get("label") or group.get("cwd") or "").lower(),
            ),
        )
        total_projects = len(projects)
        total_sessions = sum(int(group["total"]) for group in projects)
        if project_limit is not None:
            projects = projects[:project_limit]
        return [self._project_public_payload(group) for group in projects], total_projects, total_sessions

    def list_pinned_projects(self, *, per_project_limit: int = 5) -> list[dict[str, Any]]:
        groups = self._collect_project_groups(per_project_limit)
        pinned = [
            group
            for group in groups.values()
            if group.get("pinned") and not group.get("archived") and not group.get("hidden")
        ]
        pinned.sort(key=lambda group: group.get("pinnedAt") or "", reverse=True)
        return [self._project_public_payload(group) for group in pinned]

    def _collect_archived_groups(self) -> dict[str, dict[str, Any]]:
        """Group *archived* sessions by project.

        Mirror of :meth:`_collect_project_groups` but inverting the archive
        filter: every archived session is kept (no per-project cap), so the
        「已归档对话」view can list them all. Sessions inside each group are
        sorted by ``updated_at`` descending; groups carry the same project
        metadata (label/pinned/…) as the active listing.
        """
        entries, _total = self.index.list_all_projects_page(limit=None)
        groups: dict[str, dict[str, Any]] = {}
        listed_keys: set[tuple[str, str]] = set()

        for entry in entries:
            if not entry.cwd:
                continue
            if self._foreign_hidden(entry.cwd, entry.session_id):
                continue
            listed_keys.add((entry.cwd, entry.session_id))
            session = self._from_entry(entry)
            if not _is_listable_entry(entry) and not _is_listable_session(session):
                continue
            if not session.archived:
                continue
            group = groups.setdefault(entry.cwd, {"cwd": entry.cwd, "sessions": [], "total": 0, "_sort_mtime": 0.0})
            group["_sort_mtime"] = max(float(group["_sort_mtime"]), entry.mtime)
            group["sessions"].append(session)

        for session in self._sessions.values():
            session_key = (session.cwd, session.session_id)
            if session_key in listed_keys:
                continue
            if self._foreign_hidden(session.cwd, session.session_id):
                continue
            listed_keys.add(session_key)
            if not _is_listable_session(session) or not session.archived:
                continue
            group = groups.setdefault(session.cwd, {"cwd": session.cwd, "sessions": [], "total": 0, "_sort_mtime": 0.0})
            group["_sort_mtime"] = max(float(group["_sort_mtime"]), _timestamp_from_utc(session.updated_at))
            group["sessions"].append(session)

        for group in groups.values():
            group["sessions"].sort(key=lambda item: item.updated_at, reverse=True)
            group["total"] = len(group["sessions"])
            meta = self.read_project_metadata(group["cwd"])
            group["pinned"] = meta["pinned"]
            group["pinnedAt"] = meta["pinnedAt"]
            group["archived"] = meta["archived"]
            group["hidden"] = meta["hidden"]
            group["collapsed"] = meta["collapsed"]
            if meta["label"]:
                group["label"] = meta["label"]
        return groups

    def list_archived_projects(self) -> list[dict[str, Any]]:
        """List projects that contain archived sessions, newest project first."""
        groups = self._collect_archived_groups()
        projects = sorted(
            groups.values(),
            key=lambda group: (
                -float(group.get("_sort_mtime") or 0.0),
                str(group.get("label") or group.get("cwd") or "").lower(),
            ),
        )
        return [self._project_public_payload(group) for group in projects]

    def list_pinned_sessions(self) -> list[WebSession]:
        sessions, _total = self.list_sessions_page(limit=None)
        pinned = [
            session for session in sessions if session.pinned and not session.archived and _is_listable_session(session)
        ]
        pinned.sort(key=lambda session: session.pinned_at or session.updated_at, reverse=True)
        return pinned

    def _list_entries_for_visible_limit(self, limit: int | None) -> tuple[list[SessionEntry], int]:
        if limit is None:
            return self.index.list_all_projects_page(limit=None)
        if limit <= 0:
            return self.index.list_all_projects_page(limit=0)

        def visible_active_count(candidate_entries: list[SessionEntry]) -> int:
            count = 0
            for entry in candidate_entries:
                if self._foreign_hidden(entry.cwd, entry.session_id):
                    continue
                if not _is_listable_entry(entry) and not _is_foreign_session(self.storage, entry.cwd, entry.session_id):
                    continue
                session = self._from_entry(entry)
                if session.archived:
                    continue
                if not _is_listable_entry(entry) and not _is_listable_session(session):
                    continue
                count += 1
            return count

        scan_limit = limit
        entries, total = self.index.list_all_projects_page(limit=scan_limit)
        while visible_active_count(entries) < limit and len(entries) < total:
            next_limit = min(total, max(scan_limit * 2, limit * 4, scan_limit + 50))
            if next_limit <= scan_limit:
                break
            scan_limit = next_limit
            entries, total = self.index.list_all_projects_page(limit=scan_limit)
        return entries, total

    def get_session(self, ref: str) -> WebSession | None:
        web_session_key = _decode_web_session_id(ref)
        if web_session_key is not None:
            return self._session_from_key(web_session_key)

        matching_key = self._unique_session_key_for_bare_ref(ref)
        if matching_key is None:
            return None
        return self._session_from_key(matching_key)

    def delete_session(self, ref: str) -> bool:
        """Permanently delete a session (storage + in-memory cache).

        Resolves ``ref`` the same way as :meth:`get_session`, then removes
        its on-disk storage and drops it from the in-memory cache so it no
        longer appears in any listing. Returns ``True`` if the session was
        found and removed.
        """
        session = self.get_session(ref)
        if session is None:
            return False
        removed = self.storage.delete_session(session.cwd, session.session_id)
        self._sessions.pop((session.cwd, session.session_id), None)
        self._record_session_lifecycle_mutation(session.cwd, session.session_id)
        return removed

    def _active_session_keys_for_project(self, cwd: str) -> list[tuple[str, str]]:
        """Return keys of every listable, not-yet-archived session under ``cwd``.

        Mirrors the enumeration in :meth:`_collect_project_groups` (index
        entries first, then in-memory sessions not represented by the index)
        but without the per-project cap and without dropping pinned sessions —
        archiving a project should sweep up *all* of its live conversations.
        """
        keys: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        entries, _total = self.index.list_all_projects_page(limit=None)
        for entry in entries:
            if entry.cwd != cwd:
                continue
            key = (entry.cwd, entry.session_id)
            if key in seen:
                continue
            seen.add(key)
            session = self._from_entry(entry)
            if not _is_listable_entry(entry) and not _is_listable_session(session):
                continue
            if session.archived:
                continue
            keys.append(key)
        for session in self._sessions.values():
            if session.cwd != cwd:
                continue
            key = (session.cwd, session.session_id)
            if key in seen:
                continue
            seen.add(key)
            if not _is_listable_session(session) or session.archived:
                continue
            keys.append(key)
        return keys

    async def archive_project_sessions(self, cwd: str) -> int:
        """Archive every active session belonging to ``cwd``.

        Applies the single-session archive (set ``archived=True`` and clear any
        pin, see :func:`patch_session`) to each listable, not-yet-archived
        session of the project so the whole project's conversations move into
        the 「已归档对话」view grouped under this project, each individually
        un-archivable. Project metadata is intentionally left untouched (no
        project-level ``archived`` flag) so un-archiving any session brings it
        straight back to the sidebar. Returns the number of sessions archived.
        """
        archived = 0
        for key in self._active_session_keys_for_project(cwd):
            session = self._session_from_key(key)
            if session is None or session.archived:
                continue
            async with session.turn_admission_lock:
                if self._sessions.get(key) is not session:
                    continue
                active_task = session.active_turn_task
                if (
                    session.turn_lock.locked()
                    or (active_task is not None and not active_task.done())
                    or any(not task.done() for task in session.active_local_tasks)
                    or session.pending_permissions
                    or session.pending_questions
                    or session.pending_elicitations
                ):
                    continue
                session.archived = True
                session.pinned = False
                session.pinned_at = None
                self.persist_web_metadata(session)
                archived += 1
        return archived

    def delete_archived_sessions(self, cwd: str | None = None) -> int:
        """Delete every archived session, optionally limited to one project.

        Returns the number of sessions whose storage was removed.
        """
        groups = self._collect_archived_groups()
        if cwd is not None:
            groups = {key: group for key, group in groups.items() if key == cwd}
        deleted = 0
        for group in groups.values():
            for session in list(group["sessions"]):
                if self.is_session_read_only(session):
                    continue
                if self.storage.delete_session(session.cwd, session.session_id):
                    deleted += 1
                self._sessions.pop((session.cwd, session.session_id), None)
                self._record_session_lifecycle_mutation(session.cwd, session.session_id)
        return deleted

    def load_visible_messages(self, session_id: str, *, cwd: Path | str | None = None) -> list[dict[str, Any]]:
        """Load transcript rows that should be visible in the browser."""
        return self.load_visible_transcript(session_id, cwd=cwd)["messages"]

    def load_visible_transcript(self, session_id: str, *, cwd: Path | str | None = None) -> dict[str, Any]:
        """Load visible browser transcript rows plus replayed tool-call state."""
        actual_cwd = str(cwd) if cwd is not None else str(self.cwd)
        resume_messages = self.load_resume_messages(session_id, cwd=actual_cwd)
        # 压缩标记在存储里排在其保留尾部之前(LLM 有效切片需要),但可见转录应显示在压缩真实发生的
        # 位置——把标记下沉到尾部之后。仅影响展示顺序,不改存储/LLM 上下文。
        resume_messages = reorder_compaction_markers(resume_messages)
        web_metadata = _read_web_session_metadata(self.storage, actual_cwd, session_id)
        # 「有流水线历史可回放」与「当前是否按流水线路由」解耦:交接给普通对话后模式落为
        # normal(Issue 4),但 sidecar 仍留有 contextId 指向 A2A 日志。只要曾是流水线(存有
        # contextId),reload 就走流水线回放路径重建整段转录,不因模式翻转而丢历史。
        is_pipeline_session = _mode_from_metadata_or_sidecar(
            self.storage, actual_cwd, session_id, web_metadata.get("mode")
        ) == "pipeline" or bool(isinstance(web_metadata.get("contextId"), str) and web_metadata.get("contextId"))
        visible: list[dict[str, Any]] = []
        tools: dict[str, dict[str, Any]] = {}
        pipeline_replay_inserted = False
        normal_chat_marker_inserted = False
        # Mid-pipeline answers (persisted as ``source=pipeline`` web messages, e.g. the
        # "0" chosen at a confirm_and_select) are woven into the replay right after the
        # ``input_required`` prompt they answered instead of trailing the whole replay
        # (Issue 2). ``append_pipeline_replay`` drains this queue at each anchor row;
        # anything left over falls through to the legacy append-at-end path below.
        pipeline_answer_queue: list[Message] = []
        consumed_answer_ids: set[int] = set()

        def append_visible_message(
            *,
            role: str,
            content: str,
            thinking_blocks: list[str] | None = None,
            tool_use_ids: list[str] | None = None,
            block_payloads: list[dict[str, Any]] | None = None,
            segment_tools: dict[str, dict[str, Any]] | None = None,
            kind: str = "",
            pipeline_step: dict[str, Any] | None = None,
            elapsed_seconds: float = 0.0,
            message_id: str | None = None,
            image_ids: list[str] | None = None,
            file_refs: list[str] | None = None,
        ) -> None:
            # Pipeline reload rows pass the translator's stable id (``plmk-*`` / ``pl-*``)
            # so a mid-run reload dedups against the replayed live SSE stream; normal rows
            # fall back to a positional ``stored-N`` id.
            message_id = message_id or "stored-{}".format(len(visible) + 1)
            payload: dict[str, Any] = {
                "messageId": message_id,
                "role": role,
                "content": content,
                "text": content,
                "thinking": "\n\n".join(thinking_blocks or []),
                "toolUseIds": tool_use_ids or [],
                "blocks": block_payloads if block_payloads else [{"type": "text", "text": content}],
                "stored": True,
                "status": "completed",
                "sequence": len(visible) + 1,
            }
            if elapsed_seconds:
                payload["elapsedSeconds"] = elapsed_seconds
            if kind:
                payload["kind"] = kind
            if pipeline_step:
                payload["pipelineStep"] = pipeline_step
            if image_ids:
                payload["imageIds"] = list(image_ids)
            if file_refs:
                payload["fileRefs"] = list(file_refs)
            visible.append(payload)
            for tool_id, tool in (segment_tools or {}).items():
                tools[tool_id] = {**tool, "messageId": message_id}

        def append_transcript_messages(
            messages: list[Message],
            *,
            skip_user_text: bool = False,
            hidden_tool_names: set[str] | None = None,
            tool_result_source: list[Message] | None = None,
        ) -> None:
            tool_results = _tool_results_by_id(tool_result_source or messages)
            hidden_tool_names = hidden_tool_names or set()

            for message in messages:
                if _is_hidden_replay_message(message):
                    continue
                if skip_user_text and message.role == "user" and not _is_tool_result_only_message(message):
                    continue
                if _is_tool_result_only_message(message):
                    continue

                if isinstance(message.content, list):
                    message_image_ids = _metadata_string_list(message.metadata, "imageIds")
                    message_file_refs = _metadata_string_list(message.metadata, "fileRefs")
                    stable_message_id = _persisted_message_stable_id(message)
                    stable_segment_index = 0
                    text_blocks: list[str] = []
                    thinking_blocks: list[str] = []
                    tool_use_ids: list[str] = []
                    block_payloads: list[dict[str, Any]] = []
                    segment_tools: dict[str, dict[str, Any]] = {}

                    def flush_segment() -> None:
                        nonlocal text_blocks, thinking_blocks, tool_use_ids, block_payloads, segment_tools
                        nonlocal stable_segment_index
                        if not text_blocks and not thinking_blocks and not tool_use_ids and not block_payloads:
                            return
                        message_id = stable_message_id
                        if message_id and stable_segment_index:
                            message_id = "{}-segment-{}".format(message_id, stable_segment_index + 1)
                        append_visible_message(
                            role=message.role,
                            content="\n".join(text_blocks),
                            thinking_blocks=thinking_blocks,
                            tool_use_ids=tool_use_ids,
                            block_payloads=block_payloads,
                            segment_tools=segment_tools,
                            elapsed_seconds=message.elapsed_seconds,
                            message_id=message_id,
                            image_ids=message_image_ids if stable_segment_index == 0 else None,
                            file_refs=message_file_refs if stable_segment_index == 0 else None,
                        )
                        stable_segment_index += 1
                        text_blocks = []
                        thinking_blocks = []
                        tool_use_ids = []
                        block_payloads = []
                        segment_tools = {}

                    for block in message.content:
                        if isinstance(block, TextBlock):
                            if tool_use_ids:
                                flush_segment()
                            if block.text:
                                text_blocks.append(block.text)
                            block_payloads.append({"type": "text", "text": block.text})
                        elif isinstance(block, ThinkingBlock):
                            if tool_use_ids:
                                flush_segment()
                            if block.thinking:
                                thinking_blocks.append(block.thinking)
                            block_payloads.append({"type": "thinking", "thinking": block.thinking})
                        elif isinstance(block, ToolUseBlock):
                            if block.name in hidden_tool_names:
                                continue
                            result_blocks = tool_results.get(block.id, [])
                            tool_use_ids.append(block.id)
                            block_payloads.append(
                                {
                                    "type": "tool_use",
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                }
                            )
                            segment_tools[block.id] = {
                                "toolUseId": block.id,
                                "toolName": block.name,
                                "input": block.input,
                                "status": "failed"
                                if any(result.is_error for result in result_blocks)
                                else "completed"
                                if result_blocks
                                else "pending",
                                "results": [_tool_result_payload(result) for result in result_blocks],
                                "stored": True,
                            }
                        elif isinstance(block, ImageBlock):
                            block_payloads.append(
                                {
                                    "type": "image",
                                    "mediaType": block.media_type,
                                    "refId": block.ref_id,
                                }
                            )
                    flush_segment()
                else:
                    content = "\n".join(_message_text_blocks(message))
                    if not content and isinstance(message.content, str):
                        content = message.content
                    append_visible_message(
                        role=message.role,
                        content=content,
                        elapsed_seconds=message.elapsed_seconds,
                        message_id=_persisted_message_stable_id(message),
                        image_ids=_metadata_string_list(message.metadata, "imageIds"),
                        file_refs=_metadata_string_list(message.metadata, "fileRefs"),
                    )

        def optional_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def append_pipeline_marker(
            step: Mapping[str, Any],
            *,
            level: str,
            kind: str,
            parent_step_id: str = "",
            candidate_name: str = "",
            group_id: str = "",
            parent_group_id: str = "",
            depth: int = 0,
        ) -> None:
            step_id = str(step.get("stepId") or "")
            title = display_step_name(step_id)
            index = optional_int(step.get("index"))
            total = optional_int(step.get("total"))
            attempt_no = optional_int(step.get("attemptNo")) or 1
            if level == "candidate":
                content = _("◆ Plan: {}").format(candidate_name or "Candidate")
            else:
                prefix = "·" if level == "sub_step" else "●"
                content = "{} {}".format(prefix, title or step_id or "Step")
            # Top-level steps and candidate sub-steps both carry an index/total
            # coordinate; show the same ``(N/M)`` progress suffix for either so a
            # reloaded transcript matches the live one. Candidate rows ("◆ 方案：…")
            # have no total-candidates count, so they stay without a suffix.
            if level in ("step", "sub_step") and index is not None and total is not None:
                content += " ({}/{})".format(index, total)
            if attempt_no > 1:
                content += " #{}".format(attempt_no)
            append_visible_message(
                role="assistant",
                content=content,
                kind=kind,
                pipeline_step={
                    "level": level,
                    "stepId": step_id,
                    "title": title,
                    "index": index,
                    "total": total,
                    "status": str(step.get("status") or ""),
                    "attemptNo": attempt_no,
                    "parentStepId": parent_step_id,
                    "candidateName": candidate_name,
                    "groupId": group_id,
                    "parentGroupId": parent_group_id,
                    "depth": depth,
                },
            )

        def pipeline_step_group_id(step: Mapping[str, Any]) -> str:
            step_id = str(step.get("stepId") or "step")
            suffix = str(step.get("attemptId") or step.get("transcriptId") or step.get("attemptNo") or "1")
            return "step:{}:{}".format(step_id, suffix)

        def pipeline_sub_step_group_id(step: Mapping[str, Any], parent_group_id: str) -> str:
            step_id = str(step.get("stepId") or "sub_step")
            suffix = str(step.get("attemptId") or step.get("transcriptId") or step.get("attemptNo") or "1")
            return "{}:sub_step:{}:{}".format(parent_group_id, step_id, suffix)

        def append_normal_chat_marker() -> None:
            nonlocal normal_chat_marker_inserted
            if normal_chat_marker_inserted:
                return
            normal_chat_marker_inserted = True
            append_visible_message(
                role="assistant",
                content=_("↪ Normal chat"),
                kind="normal_chat_boundary",
                pipeline_step={
                    "level": "normal_chat",
                    "stepId": "",
                    "title": _("Normal chat"),
                    "index": None,
                    "total": None,
                    "status": "",
                    "attemptNo": 1,
                    "parentStepId": "",
                    "candidateName": "",
                    "groupId": "normal-chat",
                    "parentGroupId": "",
                    "depth": 0,
                },
            )

        def sorted_sub_pipelines(attempt: Mapping[str, Any]) -> list[Mapping[str, Any]]:
            sub_pipelines = attempt.get("subPipelines")
            if not isinstance(sub_pipelines, Mapping):
                return []
            items = [item for item in sub_pipelines.values() if isinstance(item, Mapping)]
            return sorted(
                items,
                key=lambda item: (
                    optional_int(item.get("candidateIndex")) is None,
                    optional_int(item.get("candidateIndex")) or 0,
                    str(item.get("candidateName") or item.get("subPipelineId") or ""),
                ),
            )

        def sorted_sub_steps(
            sub_pipeline: Mapping[str, Any],
            replay_metadata: Mapping[str, Mapping[str, Any]],
        ) -> list[Mapping[str, Any]]:
            steps = sub_pipeline.get("steps")
            if not isinstance(steps, list):
                return []
            items = [item for item in steps if isinstance(item, Mapping)]

            def replay_order(item: Mapping[str, Any]) -> int | None:
                for key in (item.get("attemptId"), item.get("transcriptId")):
                    if key and (metadata := replay_metadata.get(str(key))):
                        return optional_int(metadata.get("eventOrder"))
                return None

            return sorted(
                items,
                key=lambda item: (
                    replay_order(item) is None,
                    replay_order(item) or 0,
                    optional_int(item.get("stepIndex")) is None,
                    optional_int(item.get("stepIndex")) or 0,
                    optional_int(item.get("attemptNo")) or 0,
                    str(item.get("stepId") or ""),
                ),
            )

        def append_pipeline_transcript(transcript_storage: Any, transcript_id: Any) -> None:
            if not isinstance(transcript_id, str) or not transcript_id:
                return
            append_transcript_messages(
                transcript_storage.load(actual_cwd, transcript_id),
                skip_user_text=True,
                hidden_tool_names=PIPELINE_HIDDEN_REPLAY_TOOL_NAMES,
            )

        def pipeline_replay_metadata_by_key() -> dict[str, dict[str, Any]]:
            display_path = _pipeline_display_replay_path(self.storage, actual_cwd, session_id)
            try:
                lines = display_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return {}
            metadata_by_key: dict[str, dict[str, Any]] = {}
            for event_order, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, Mapping) or event.get("type") != "sub_step_started":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                candidate_name = str(payload.get("candidate_name") or payload.get("candidateName") or "")
                if not candidate_name:
                    continue
                metadata = {
                    "candidateName": candidate_name,
                    "eventOrder": event_order,
                    "subPipelineId": str(payload.get("sub_pipeline_id") or payload.get("subPipelineId") or ""),
                }
                for key in (
                    payload.get("active_attempt_id"),
                    payload.get("attempt_id"),
                    payload.get("transcript_id"),
                    payload.get("sub_pipeline_id"),
                    payload.get("subPipelineId"),
                ):
                    if key:
                        metadata_by_key[str(key)] = metadata
            return metadata_by_key

        def append_pipeline_replay() -> None:
            nonlocal pipeline_replay_inserted, normal_chat_marker_inserted
            if pipeline_replay_inserted:
                return
            pipeline_replay_inserted = True
            # Web pipeline turns run in a separate A2A pipeline session and never write this
            # session's own pipeline/display.jsonl, so reload rebuilds the main transcript from
            # the A2A event journal via the same translator that powers the live stream.
            context_id = web_metadata.get("contextId") if isinstance(web_metadata.get("contextId"), str) else None
            envelopes = self._load_a2a_pipeline_envelopes(context_id)
            if envelopes:
                from iac_code.web.pipeline_transcript import build_pipeline_transcript_rows

                for row in build_pipeline_transcript_rows(envelopes):
                    kind = str(row.get("kind") or "")
                    # A2A 日志里若已有交接信封,翻译器会产出「↪ 普通对话」分隔行;标记为已插入,
                    # 避免主循环的启发式 append_normal_chat_marker 再补一条,导致分隔出现两次(Issue 7c)。
                    if kind == "normal_chat_boundary":
                        normal_chat_marker_inserted = True
                    append_visible_message(
                        role=str(row.get("role") or "assistant"),
                        content=str(row.get("content") or ""),
                        kind=kind,
                        pipeline_step=row.get("pipelineStep") or None,
                        tool_use_ids=list(row.get("toolUseIds") or []),
                        segment_tools=dict(row.get("tools") or {}),
                        message_id=str(row.get("id") or "") or None,
                        # Carry reconstructed thinking so reloaded pipeline messages show
                        # 思考完成 like live/normal mode (build_pipeline_transcript_rows folds
                        # assistant.thinking.delta into row["thinking"]).
                        thinking_blocks=[str(row["thinking"])] if row.get("thinking") else None,
                    )
                    # Weave the persisted user answer(s) directly after the prompt row
                    # they answered, so a mid-pipeline "0" lands between the confirm
                    # prompt and the next step marker instead of at the very end.
                    for _slot in range(int(row.get("inputAnswerSlots") or 0)):
                        if not pipeline_answer_queue:
                            break
                        answer = pipeline_answer_queue.pop(0)
                        consumed_answer_ids.add(id(answer))
                        append_transcript_messages([answer], tool_result_source=resume_messages)
                return
            replay = self.load_pipeline_display_replay(session_id, cwd=actual_cwd)
            if not isinstance(replay, Mapping):
                return
            attempts = replay.get("attempts")
            if not isinstance(attempts, list):
                return
            try:
                from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage

                transcript_storage = PipelineTranscriptStorage(
                    self.storage.session_dir(actual_cwd, session_id) / "pipeline"
                )
            except Exception:
                return
            replay_metadata = pipeline_replay_metadata_by_key()
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                step_group_id = pipeline_step_group_id(attempt)
                append_pipeline_marker(
                    attempt,
                    level="step",
                    kind="pipeline_step",
                    group_id=step_group_id,
                    depth=0,
                )
                append_pipeline_transcript(transcript_storage, attempt.get("transcriptId"))
                parent_step_id = str(attempt.get("stepId") or "")
                for sub_pipeline in sorted_sub_pipelines(attempt):
                    sub_pipeline_id = str(sub_pipeline.get("subPipelineId") or "")
                    sub_pipeline_metadata = replay_metadata.get(sub_pipeline_id, {})
                    candidate_name = str(
                        sub_pipeline.get("candidateName") or sub_pipeline_metadata.get("candidateName") or ""
                    )
                    candidate_group_id = "candidate:{}".format(sub_pipeline_id or candidate_name or "candidate")
                    sub_steps = sorted_sub_steps(sub_pipeline, replay_metadata)
                    if sub_steps:
                        append_pipeline_marker(
                            {
                                "stepId": sub_pipeline_id or "candidate",
                                "status": sub_pipeline.get("status"),
                                "attemptNo": 1,
                            },
                            level="candidate",
                            kind="pipeline_candidate",
                            parent_step_id=parent_step_id,
                            candidate_name=candidate_name,
                            group_id=candidate_group_id,
                            parent_group_id=step_group_id,
                            depth=1,
                        )
                    for sub_step in sub_steps:
                        sub_step_metadata = (
                            replay_metadata.get(str(sub_step.get("attemptId") or ""))
                            or replay_metadata.get(str(sub_step.get("transcriptId") or ""))
                            or {}
                        )
                        candidate_name = str(
                            sub_step.get("candidateName")
                            or sub_pipeline.get("candidateName")
                            or sub_step_metadata.get("candidateName")
                            or sub_pipeline_metadata.get("candidateName")
                            or ""
                        )
                        append_pipeline_marker(
                            sub_step,
                            level="sub_step",
                            kind="pipeline_sub_step",
                            parent_step_id=parent_step_id,
                            candidate_name=candidate_name,
                            group_id=pipeline_sub_step_group_id(sub_step, candidate_group_id),
                            parent_group_id=candidate_group_id,
                            depth=2,
                        )
                        append_pipeline_transcript(transcript_storage, sub_step.get("transcriptId"))

        def should_start_normal_chat(message: Message) -> bool:
            if not is_pipeline_session or not pipeline_replay_inserted or normal_chat_marker_inserted:
                return False
            if _is_hidden_replay_message(message):
                return False
            if _is_tool_result_only_message(message):
                return False
            # 首选显式标记:persist_pipeline_user_prompt 在流水线交接给普通对话后,给该回合的
            # prompt 打上 ``normalChat``。分隔就插在首条带此标记的消息前。
            if message.metadata.get("normalChat"):
                return True
            # Issue 5 之后,流水线中途的 input_required 文本回复也会以 ``source=pipeline`` 落进
            # JSONL 但不带 ``normalChat``;它们仍在流水线内部,不能触发分隔(否则分隔会错插到
            # 流水线中间)。
            if message.metadata.get("source") == "pipeline":
                return False
            # 旧会话(Issue 5 之前持久化)web JSONL 里没有流水线 prompt,交接后普通消息也不带
            # 标记;沿用“回放后首条非交接文本 user 消息”的启发式,保持向后兼容。
            return not _is_pipeline_handoff_context_text(message.get_text())

        # Pre-collect mid-pipeline answers (every ``source=pipeline`` user reply after the
        # launcher, excluding the normal-chat handoff prompt and handoff-context text) so
        # ``append_pipeline_replay`` can slot them at their prompt anchors. The launcher is
        # the first such message — it opens the replay, it is not an answer to a prompt.
        if is_pipeline_session:
            launcher_seen = False
            for message in resume_messages:
                if _is_hidden_replay_message(message) or _is_tool_result_only_message(message):
                    continue
                if message.role != "user" or message.metadata.get("source") != "pipeline":
                    continue
                if message.metadata.get("normalChat"):
                    continue
                if _is_pipeline_handoff_context_text(message.get_text()):
                    continue
                if not launcher_seen:
                    launcher_seen = True
                    continue
                pipeline_answer_queue.append(message)

        def append_compaction_boundary_marker(message: Message) -> None:
            # 内联压缩标记(role=user)在 LLM 上下文里是就绪摘要,但可见转录须折成分隔条行,
            # 而不是渲染成巨型 user 气泡。去掉遗留文本前缀后作为 assistant 分隔条呈现。
            text = message.get_text()
            prefix = "[Conversation Summary]\n"
            summary = text[len(prefix) :] if text.startswith(prefix) else text
            append_visible_message(
                role="assistant",
                content=summary,
                kind="context_compaction_boundary",
            )

        for message in resume_messages:
            if id(message) in consumed_answer_ids:
                continue
            if is_compaction_summary_message(message):
                append_compaction_boundary_marker(message)
                continue
            if is_pipeline_session and _is_pipeline_handoff_context_text(message.get_text()):
                append_pipeline_replay()
                continue
            if should_start_normal_chat(message):
                append_normal_chat_marker()
            before_count = len(visible)
            append_transcript_messages([message], tool_result_source=resume_messages)
            if (
                is_pipeline_session
                and not pipeline_replay_inserted
                and len(visible) > before_count
                and message.role == "user"
                and not _is_tool_result_only_message(message)
            ):
                append_pipeline_replay()
        if is_pipeline_session and not pipeline_replay_inserted:
            append_pipeline_replay()
        return normalize_event_payload({"messages": visible, "tools": tools})

    def _load_pipeline_sidecar_replay_messages(self, session_id: str, *, cwd: str) -> list[Message]:
        replay = self.load_pipeline_display_replay(session_id, cwd=cwd)
        transcript_ids = _pipeline_replay_transcript_ids(replay)
        if not transcript_ids:
            return []
        try:
            from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage

            transcript_storage = PipelineTranscriptStorage(self.storage.session_dir(cwd, session_id) / "pipeline")
            messages: list[Message] = []
            for transcript_id in transcript_ids:
                messages.extend(transcript_storage.load(cwd, transcript_id))
            return messages
        except Exception:
            return []

    def _load_a2a_pipeline_envelopes(self, context_id: str | None) -> list[dict[str, Any]]:
        """Read the fine-grained A2A pipeline event journal for a web pipeline context.

        Resolves the context to its separate pipeline session (cwd + session_id) and
        returns the journaled envelopes in ``sequence`` order, or an empty list when the
        context / journal is missing or unreadable.
        """
        if not context_id:
            return []
        try:
            from iac_code.a2a.persistence import A2APersistenceStore
            from iac_code.a2a.pipeline_journal import A2APipelineJournal
            from iac_code.a2a.pipeline_paths import existing_a2a_pipeline_dir_for_session
            from iac_code.config import get_config_dir

            snapshot = A2APersistenceStore(get_config_dir() / "a2a").load_context(context_id)
            if snapshot is None:
                return []
            pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=snapshot.cwd, session_id=snapshot.session_id)
            return A2APipelineJournal(pipeline_dir).read_all_repairing_tail()
        except Exception:
            return []

    def load_pipeline_display_replay(self, session_id: str, *, cwd: Path | str | None = None) -> dict[str, Any] | None:
        """Load a REPL pipeline display sidecar as a browser-friendly replay model."""
        actual_cwd = str(cwd) if cwd is not None else str(self.cwd)
        display_path = _pipeline_display_replay_path(self.storage, actual_cwd, session_id)
        if not display_path.exists():
            return None
        try:
            from iac_code.pipeline.engine.display_replay import PipelineDisplayReducer, load_display_events
            from iac_code.pipeline.engine.session import PipelineSession

            events = load_display_events(display_path)
            if not events:
                return None
            sidecar = PipelineSession(display_path.parent)
            restore_result = sidecar.restore_sync({})
            model = PipelineDisplayReducer().reduce(events, restore_result.attempts)
            if not model.attempts:
                return None
            return normalize_event_payload(_camelize(asdict(model)))
        except Exception:
            return None

    def load_resume_messages(self, session_id: str, *, cwd: Path | str | None = None) -> list[Message]:
        """Load stored conversation messages for CLI/REPL-compatible agent resume."""
        actual_cwd = str(cwd) if cwd is not None else str(self.cwd)
        return self.storage.load(actual_cwd, session_id)

    def _resolve_session_arg(self, session: WebSession | str) -> WebSession:
        if isinstance(session, WebSession):
            return session
        resolved = self.get_session(session)
        if resolved is None:
            raise ValueError(_("session not found"))
        return resolved

    def status(self, session: WebSession | str) -> dict[str, Any]:
        """Return a redacted JSON-safe status snapshot for a session."""
        session = self._resolve_session_arg(session)
        payload = session.to_dict()
        visible_messages = self.load_visible_messages(session.session_id, cwd=session.cwd)
        resume_messages = self.load_resume_messages(session.session_id, cwd=session.cwd)
        payload["messageCounts"] = {
            "visible": len(visible_messages),
            "resume": len(resume_messages),
        }
        payload["contextUsage"] = _context_usage_payload(
            resume_messages,
            model=payload.get("model"),
            system_prompt_tokens=session.context_system_prompt_tokens,
            tool_definition_tokens=session.context_tool_definition_tokens,
        )
        try:
            from iac_code.services.session_usage import SessionUsageStore

            usage_store = SessionUsageStore(projects_dir=getattr(self.storage, "_projects_dir", None))
            payload["usage"] = _usage_totals_payload(usage_store.load(session.cwd, session.session_id))
        except Exception:
            payload["usage"] = _usage_totals_payload(None)
        return normalize_event_payload(payload)

    def persist_web_metadata(self, session: WebSession | str) -> None:
        """Persist Web-only session state without changing CLI session metadata schema."""
        session = self._resolve_session_arg(session)
        path = _web_session_metadata_path(self.storage, session.cwd, session.session_id)
        payload = json.dumps(
            normalize_event_payload(
                {
                    "schemaVersion": 1,
                    "mode": session.mode,
                    "pipelineName": session.pipeline_name,
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                    "allowUserEscapes": _allow_user_escapes_payload(session.allow_user_escapes),
                    "permissionMode": (
                        session.permission_context.mode.value
                        if session.permission_context is not None
                        else (session.permission_mode or PermissionMode.DEFAULT).value
                    ),
                    "thinkingEnabled": session.thinking_enabled,
                    "provider": session.provider,
                    "model": session.model,
                    "effort": session.effort,
                    "origin": session.origin,
                    "pinned": session.pinned,
                    "pinnedAt": session.pinned_at,
                    "archived": session.archived,
                    "unread": session.unread,
                    # 流水线会话的 prompt 不写入 web 会话自身的 JSONL(对话进 A2A/pipeline 存储),
                    # 因此 index 扫描得到的 auto_title 恒为空、标题回落成「(empty)」。这里把实时派生的
                    # 标题落到 web sidecar,刷新/重启后由 _from_entry 从 sidecar 兜底读回。
                    "autoTitle": (session.title if session.title and session.title != "(empty)" else None),
                    # 忠实序列化会话真实的 updated_at,而非每次持久化时另取当前时间——否则磁盘与内存
                    # 会有微秒级漂移,且非活动操作(置顶/重命名)会误把「上一次操作」刷成现在。
                    "updatedAt": session.updated_at,
                }
            ),
            ensure_ascii=False,
        )
        atomic_write_text(path, payload + "\n")

    def mark_session_completed(self, session: WebSession | str) -> bool:
        """轮次正常结束时调用:若结束时无人在看(无活跃 SSE 订阅),标记为未读并持久化。

        「有没有在看」以是否存在活跃 SSE 订阅判定——前端只对当前会话开 SSE,故订阅数>0
        即代表用户正看着它结束,不应标未读。归档会话不标未读。返回是否发生变更。
        """
        session = self._resolve_session_arg(session)
        if session.archived or session.events.subscriber_count > 0 or session.unread:
            return False
        session.unread = True
        self.persist_web_metadata(session)
        return True

    def mark_session_viewed(self, session: WebSession | str) -> bool:
        """打开会话(建立 SSE 订阅)时调用:清除未读标记并持久化。返回是否发生变更。"""
        session = self._resolve_session_arg(session)
        if not session.unread:
            return False
        session.unread = False
        self.persist_web_metadata(session)
        return True

    def mark_session_running(self, session: WebSession | str) -> bool:
        """新轮次开始时调用:清除未读标记并持久化。返回是否发生变更。

        未读只表示「上一次运行结束时无人在看」。一旦会话又开始跑新一轮,它就不再处于
        「已结束待查看」状态,必须清除未读——否则 unread 与「进行中」会同时为真:侧栏对
        非当前会话不建 SSE、仅靠数秒一次的列表轮询刷新,期间任何列表快照都会把未读圆点
        画在一个正在运行的会话上(与「运行中→结束才标未读」的取值语义相悖)。与
        mark_session_completed 对称:结束标未读,开始清未读。"""
        session = self._resolve_session_arg(session)
        if not session.unread:
            return False
        session.unread = False
        self.persist_web_metadata(session)
        return True

    def apply_pipeline_auto_title(self, session: WebSession | str, text: str) -> bool:
        """为尚无标题的流水线会话把首条 prompt 设为标题并持久化。

        普通会话的标题由 index 扫描 JSONL 首条 user prompt 隐式得到;流水线会话的 prompt
        不进 web 会话自身 JSONL,故标题恒为「(empty)」、在侧栏被过滤。此方法在首个流水线回合
        用 prompt 文本补上标题(内存 + web sidecar),返回是否发生了变更(供调用方广播事件)。
        """
        session = self._resolve_session_arg(session)
        if session.title and session.title != "(empty)":
            return False
        title = _trim_title(text) if text else ""
        if not title:
            return False
        session.title = title
        # 即时占位标题:允许随后在途 LLM 结果覆盖(见 apply_llm_auto_title)。
        session.title_provisional = True
        self.persist_web_metadata(session)
        return True

    def apply_llm_auto_title(self, session: WebSession | str, title: str) -> bool:
        """把 LLM 生成/回退得到的标题落到内存 + web sidecar,并发 session.updated。

        仅当当前无有效标题(仍为空或「(empty)」)、或当前为流水线设的临时占位标题时生效,
        避免覆盖用户重命名或已冻结的正式标题。落库后清除临时标记(冻结)。返回是否发生变更。
        """
        session = self._resolve_session_arg(session)
        title = (title or "").strip()
        if not title:
            return False
        if session.title and session.title != "(empty)" and not session.title_provisional:
            return False
        session.title = title
        session.title_provisional = False
        self.persist_web_metadata(session)
        session.events.append("session.updated", {"title": session.title})
        return True

    def schedule_llm_title(self, session: WebSession | str, *, text: str, image_ids: list[str]) -> None:
        """新会话首个 turn:后台一次性生成标题(不阻塞本轮)。once-only,失败回退非空标题。"""
        session = self._resolve_session_arg(session)
        if not session.pending_llm_title:
            return
        session.pending_llm_title = False  # 立即消费,永不重触发

        async def _run() -> None:
            try:
                from iac_code.web import session_titler
                from iac_code.web.runtime import model_selection_for_session

                image_blocks: list[ContentBlock] = []
                for image_id in image_ids:
                    try:
                        img = load_cached_image(image_id, cwd=session.cwd, session_id=session.session_id)
                    except Exception:  # noqa: BLE001 - 图片缺失不应中断标题生成
                        continue
                    image_blocks.append(ContentBlock(type="image", media_type=img.media_type, data=img.base64_data))
                title = await session_titler.generate_session_title(
                    text=text,
                    image_blocks=image_blocks,
                    selection=model_selection_for_session(session),
                )
                if not title:
                    stripped = (text or "").strip()
                    title = _trim_title(stripped) if stripped else _("New image chat")
                self.apply_llm_auto_title(session, title)
            except Exception:  # noqa: BLE001 - 标题为 best-effort,任何异常都不得影响会话
                logger.debug("schedule_llm_title failed", exc_info=True)

        task = asyncio.create_task(_run())
        session.active_local_tasks.add(task)
        task.add_done_callback(session.active_local_tasks.discard)

    def persist_pipeline_user_prompt(
        self,
        session: WebSession | str,
        text: str,
        *,
        normal_chat: bool = False,
        turn_id: str | None = None,
        image_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
    ) -> None:
        """把流水线回合的用户 prompt 落入 web 会话自身的 JSONL。

        普通回合由 agent runtime 把 user 消息写进会话 JSONL,恢复时 ``load_resume_messages``
        能读回并渲染成用户气泡;流水线回合的对话进 A2A/pipeline 存储,web JSONL 里没有用户
        消息,导致刷新后主转录区连第一条 prompt 都不见。这里在回合开始时补写一条 user 消息
        (打上 ``source=pipeline`` 便于区分),让恢复路径与普通回合对齐。

        ``normal_chat=True`` 表示流水线已交接给普通对话(``normalHandoff``),这条 prompt 属于
        交接后的普通对话回合。打上 ``normalChat`` 标记,恢复时 ``should_start_normal_chat`` 只在
        看到首条带此标记的消息前插入「↪ 普通对话」分隔,避免流水线中途的 ``input_required``
        文本回复被误判为普通对话起点。
        """
        session = self._resolve_session_arg(session)
        image_ids = list(image_ids or [])
        file_refs = list(file_refs or [])
        if (not text or not text.strip()) and not image_ids and not file_refs:
            return
        metadata: dict[str, Any] = {"source": "pipeline"}
        if normal_chat:
            metadata["normalChat"] = True
        # 记录 turnId,让恢复路径给这条 prompt 行赋与实时 ``user.message`` 相同的
        # ``user-<turnId>`` 稳定键(见 append_transcript_messages)。中途 reload 时磁盘行
        # 与被回放的实时事件因此按同一键去重,不再出现「0」/普通对话文本各显示两次(Issue 7c/d)。
        if turn_id:
            metadata["turnId"] = turn_id
        if image_ids:
            metadata["imageIds"] = image_ids
        if file_refs:
            metadata["fileRefs"] = file_refs
        self.storage.append(
            str(session.cwd),
            session.session_id,
            Message(role="user", content=text, metadata=metadata),
        )

    def persist_pipeline_handoff_context(self, session: WebSession | str, summary: str | None) -> bool:
        """把流水线交接摘要(``[Pipeline Handoff Context]``)落入 web 会话自身的 JSONL。

        交接给普通对话后,普通回合由 ``WebSessionRuntime`` 直接跑 agent,上下文取自
        ``load_resume_messages``(读 web 会话 JSONL);而流水线的实际产物(创建了哪些资源、
        生成了哪些模板)只存在 A2A/pipeline 存储里,web JSONL 从没有这些信息。此前 Web 侧交接
        只翻转模式、从不注入交接上下文,导致进入普通对话后问「你刚才创建了什么」时 LLM 毫无
        流水线记忆、答「什么都没创建」。

        这里复用引擎既有交接逻辑:``summary`` 来自 A2A 快照的 ``normalHandoff.summary``,由
        ``pipeline.build_normal_handoff_summary`` → ``handoff.build_handoff_summary`` 生成,与 CLI
        ``_handoff_pipeline_to_normal`` 注入 ``context_manager`` 的内容同源。把它作为一条 user 消息
        落入 web JSONL,普通回合的 ``load_resume_messages`` 即可读到,LLM 得到流水线上下文。

        幂等:摘要已在会话中则不重复追加(交接时 + 重启兜底两处调用不会重复注入)。可见转录由
        ``load_visible_transcript`` 的 ``_is_pipeline_handoff_context_text`` 过滤,不渲染成用户气泡。
        """
        session = self._resolve_session_arg(session)
        if not isinstance(summary, str) or not summary.strip():
            return False
        cwd = str(session.cwd)
        existing = self.storage.load(cwd, session.session_id)
        for message in existing:
            if message.role == "user" and message.get_text() == summary:
                return False
        self.storage.append(
            cwd,
            session.session_id,
            Message(role="user", content=summary, metadata={"source": "pipeline"}),
        )
        return True

    def switch_session_to_normal_after_handoff(self, session: WebSession | str) -> bool:
        """流水线交接给普通对话后,把会话模式落为 ``normal``。

        ``post_message`` 按 ``session.mode`` 路由:交接后若仍是 ``pipeline``,用户输入会继续
        走流水线路径被引擎忽略,表现为「进入普通对话后继续对话完全没反应」(Issue 4)。这里
        翻转为 ``normal`` 并持久化,后续输入才走普通 agent 运行时。

        ``context_id`` / ``task_id`` 有意保留在 sidecar:``load_visible_transcript`` 的
        ``is_pipeline_session`` 判定已放宽为「模式为 pipeline **或** sidecar 存有 contextId」,
        据此 reload 仍能从 A2A 日志重建整段流水线转录,不因模式翻转而丢历史。
        """
        session = self._resolve_session_arg(session)
        if session.mode != "pipeline":
            return False
        session.mode = "normal"
        session.updated_at = _utc_now()
        self.persist_web_metadata(session)
        return True

    def touch_session_activity(self, session: WebSession | str) -> None:
        """Bump a session's last-activity time and persist it.

        侧边栏显示的相对时间(「刚刚 / N 分 / N 小时」)代表「距上一次操作多久」,读取的是
        ``updated_at``。该字段过去仅在创建 / 绑定 pipeline 时设置,跑一轮对话不会更新,导致
        刚操作过的会话仍显示很久以前。轮次开始时调用此方法即可让时间反映真实活动。
        """
        session = self._resolve_session_arg(session)
        session.updated_at = _utc_now()
        self.persist_web_metadata(session)

    def attach_pipeline_identity(
        self,
        session: WebSession | str,
        *,
        context_id: str,
        task_id: str,
        pipeline_name: str | None = None,
    ) -> None:
        """Attach and persist pipeline recovery identity for a Web session."""
        session = self._resolve_session_arg(session)
        session.mode = "pipeline"
        session.context_id = context_id
        session.task_id = task_id
        if pipeline_name:
            session.pipeline_name = pipeline_name
        session.updated_at = _utc_now()
        self.persist_web_metadata(session)
        session.events.append(
            "session.updated",
            {
                "mode": session.mode,
                "pipelineName": session.pipeline_name,
                "contextId": session.context_id,
                "taskId": session.task_id,
            },
        )

    def ensure_permission_context(self, session: WebSession | str) -> ToolPermissionContext:
        """Return the session-scoped permission context, loading configured rules once."""
        session = self._resolve_session_arg(session)
        if session.permission_context is not None:
            return session.permission_context

        from iac_code.services.permissions.loader import load_permission_context

        permission_context = load_permission_context(session.cwd)
        if session.permission_mode is not None:
            permission_context.mode = session.permission_mode
        trusted_read_dirs = build_session_trusted_read_directories(
            session.session_id,
            session_dir=self.storage.session_dir(session.cwd, session.session_id),
        )
        for directory in trusted_read_dirs:
            if directory not in permission_context.trusted_read_directories:
                permission_context.trusted_read_directories.append(directory)
        session.permission_context = permission_context
        session.permission_mode = permission_context.mode
        return permission_context

    def set_permission_mode(self, session: WebSession | str, mode: str | PermissionMode) -> PermissionMode:
        """Set the session-scoped permission mode and notify browser clients."""
        from iac_code.services.permissions.loader import parse_cli_permission_mode

        session = self._resolve_session_arg(session)
        permission_mode = mode if isinstance(mode, PermissionMode) else parse_cli_permission_mode(str(mode))
        permission_context = self.ensure_permission_context(session)
        permission_context.mode = permission_mode
        session.permission_context = permission_context
        session.permission_mode = permission_mode
        session.events.append(
            "session.updated",
            {
                "permissionMode": permission_mode.value,
            },
        )
        return permission_mode

    def set_thinking_enabled(self, session: WebSession | str, enabled: bool | None) -> bool | None:
        """Set the session-scoped thinking toggle and notify browser clients.

        ``None`` clears the override so the session falls back to the provider's
        persisted ``thinkingEnabled`` config; a bool overrides it for this
        session only (never written to settings.yml).
        """
        session = self._resolve_session_arg(session)
        normalized = bool(enabled) if isinstance(enabled, bool) else None
        session.thinking_enabled = normalized
        # 同时广播 thinkingEffective，让其它标签页在 override=None 时也能显示 provider 默认，
        # 而不是把 currentSession 里旧的 effective 值残留下来。
        session.events.append(
            "session.updated",
            {
                "thinkingEnabled": normalized,
                "thinkingEffective": session._thinking_effective(),
            },
        )
        return normalized

    def set_session_model(
        self,
        session: WebSession | str,
        *,
        provider: str,
        model: str,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Set the session-scoped provider/model/effort and notify browser clients."""
        from iac_code.providers.registry import PROVIDER_REGISTRY

        session = self._resolve_session_arg(session)
        descriptor = PROVIDER_REGISTRY.get(provider)
        if descriptor is None:
            raise ValueError(_("unknown provider"))
        if descriptor.model_ids and model not in descriptor.model_ids:
            raise ValueError(_("unknown model"))
        normalized_effort = effort or None
        if normalized_effort is not None:
            from iac_code.providers.thinking import get_thinking_spec, normalize_effort

            normalized = normalize_effort(normalized_effort)
            allowed = {item.value for item in get_thinking_spec(provider, model).allowed_efforts}
            if normalized is None or normalized not in allowed:
                raise ValueError(_("unknown effort"))
            normalized_effort = normalized
        session.provider = provider
        session.model = model
        session.effort = normalized_effort
        self.persist_web_metadata(session)
        session.events.append(
            "session.updated",
            {
                "provider": provider,
                "model": model,
                "effort": normalized_effort,
            },
        )
        return {"provider": provider, "model": model, "effort": normalized_effort}

    def clear_session_model(self, session: WebSession | str) -> dict[str, Any]:
        """Drop the session-scoped provider/model/effort override.

        The session falls back to the global runtime provider (``activeProvider``
        或第三方 ``llm_source``)。合作方源无法作为会话级 provider 存储(不在
        ``PROVIDER_REGISTRY`` 中),切换到合作方源时需先清掉本会话的覆盖,否则
        ``to_payload`` 里 ``self.provider or runtime['provider']`` 仍走旧的会话级 provider。
        """
        session = self._resolve_session_arg(session)
        session.provider = None
        session.model = None
        session.effort = None
        self.persist_web_metadata(session)
        session.events.append(
            "session.updated",
            {
                "provider": None,
                "model": None,
                "effort": None,
            },
        )
        return {"provider": None, "model": None, "effort": None}

    def clear_visible_state(self, session: WebSession | str) -> None:
        """Clear transient visible state and notify browser clients."""
        session = self._resolve_session_arg(session)
        session.draft = ""
        session.events.append(
            "session.updated",
            {
                "cleared": True,
            },
        )

    def toggle_debug(self, session: WebSession | str, *, enabled: bool | None = None) -> bool:
        """Toggle or set the session debug flag and notify browser clients."""
        session = self._resolve_session_arg(session)
        session.debug_enabled = not session.debug_enabled if enabled is None else enabled
        session.events.append(
            "session.updated",
            {
                "debugEnabled": session.debug_enabled,
            },
        )
        return session.debug_enabled

    def rename_session(self, session: WebSession | str, name: str) -> str:
        """Persist a session rename and update the loaded Web session."""
        session = self._resolve_session_arg(session)
        current_metadata = self.storage.read_metadata(session.cwd, session.session_id)
        git_branch = (
            current_metadata.git_branch if current_metadata and current_metadata.git_branch else session.git_branch
        )
        result = self.storage.rename_session(session.cwd, session.session_id, name, git_branch=git_branch)
        metadata = self.storage.read_metadata(session.cwd, session.session_id)
        session.title = metadata.name if metadata and metadata.name else session.title
        # 用户显式重命名后冻结:在途 LLM 标题结果不得再覆盖。
        session.title_provisional = False
        session.git_branch = metadata.git_branch if metadata else session.git_branch
        session.updated_at = metadata.updated_at if metadata and metadata.updated_at else session.updated_at
        session.events.append(
            "session.updated",
            {
                "title": session.title,
            },
        )
        return result

    def add_permission_request(
        self,
        session: WebSession | str,
        payload: dict[str, Any],
        *,
        future: asyncio.Future[Any] | None = None,
        audit_event: Any | None = None,
    ) -> str:
        """Track a pending permission request and append a replayable browser event."""
        session = self._resolve_session_arg(session)
        request_id = uuid.uuid4().hex
        pending = WebPendingPermission(
            request_id=request_id,
            session_id=session.session_id,
            payload=normalize_permission_payload(payload, request_id=request_id, session_id=session.session_id),
            future=future or _new_future(),
            created_at=_utc_now(),
            audit_event=audit_event,
        )
        session.pending_permissions[request_id] = pending
        session.events.append("permission.request", pending.to_dict())
        return request_id

    def get_pending_permission(
        self,
        request_id: str,
        *,
        session_id: str | None = None,
    ) -> WebPendingPermission | None:
        """Return a pending permission when it exists and belongs to the expected session."""
        for session in self._sessions.values():
            pending = session.pending_permissions.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return None
            return pending
        return None

    def resolve_permission(
        self,
        request_id: str,
        answer: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a pending permission request across loaded sessions."""
        for session in self._sessions.values():
            pending = session.pending_permissions.get(request_id)
            if pending is not None:
                if session_id is not None and pending.session_id != session_id:
                    return {"requestId": request_id, "resolved": False}
                session.pending_permissions.pop(request_id)
                choice = str(answer["choice"])
                resolved_answer = {"choice": choice}
                allowed = permission_choice_to_allowed(choice)
                audit_ok = True
                if pending.audit_event is not None:
                    from iac_code.services.permissions.audit import (
                        emit_permission_boundary_audit,
                        should_fail_closed_permission_audit,
                    )

                    decision: Literal["allow", "deny"] = "allow" if allowed else "deny"
                    scope = "session_rule" if choice in {PERMISSION_ALWAYS_ALLOW, PERMISSION_ALWAYS_DENY} else "once"
                    audit_ok = emit_permission_boundary_audit(
                        pending.audit_event,
                        session_id=session.session_id,
                        decision=decision,
                        scope=scope,
                        source="web_prompt",
                        reason_type="prompt_selection",
                        reason_detail=choice,
                        rule=_permission_audit_rule(pending.payload) if scope == "session_rule" else None,
                    )
                    if allowed and not audit_ok and should_fail_closed_permission_audit(pending.audit_event, "allow"):
                        _set_future_result(pending.future, False)
                        session.events.append(
                            "permission.resolved",
                            {
                                "requestId": request_id,
                                "answer": resolved_answer,
                            },
                        )
                        return {"requestId": request_id, "resolved": True}
                self._apply_permission_choice(session, pending, choice)
                _set_future_result(pending.future, allowed)
                session.events.append(
                    "permission.resolved",
                    {
                        "requestId": request_id,
                        "answer": resolved_answer,
                    },
                )
                return {"requestId": request_id, "resolved": True}
        return {"requestId": request_id, "resolved": False}

    def _apply_permission_choice(self, session: WebSession, pending: WebPendingPermission, choice: str) -> None:
        if choice not in {PERMISSION_ALWAYS_ALLOW, PERMISSION_ALWAYS_DENY}:
            return
        suggestions = pending.payload.get("suggestions")
        behavior = "allow" if choice == PERMISSION_ALWAYS_ALLOW else "deny"
        permission_context = self.ensure_permission_context(session)
        if not isinstance(suggestions, list) or not suggestions:
            tool_name = str(pending.payload.get("toolName", "")).strip()
            if tool_name:
                session.permission_context = apply_session_rule(
                    permission_context,
                    behavior,
                    PermissionRuleValue(tool_name=tool_name, rule_content=""),
                )
            return
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            tool_name = str(suggestion.get("toolName", "")).strip()
            rule_content = str(suggestion.get("ruleContent", "")).strip()
            if not tool_name or not rule_content:
                continue
            permission_context = apply_session_rule(
                permission_context,
                behavior,
                PermissionRuleValue(tool_name=tool_name, rule_content=rule_content),
            )
        session.permission_context = permission_context

    def discard_permission_request(self, request_id: str, *, session_id: str | None = None) -> None:
        """Remove a pending permission without publishing a user-visible answer."""
        for session in self._sessions.values():
            pending = session.pending_permissions.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return
            session.pending_permissions.pop(request_id, None)
            return

    def cancel_permission_request(self, request_id: str, *, session_id: str | None = None) -> None:
        """Resolve a pending permission as canceled so browser state can clear it."""
        for session in self._sessions.values():
            pending = session.pending_permissions.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return
            session.pending_permissions.pop(request_id, None)
            _set_future_result(pending.future, False)
            session.events.append(
                "permission.resolved",
                {
                    "requestId": request_id,
                    "answer": _canceled_permission_answer(),
                },
            )
            return

    def add_question_request(
        self,
        session: WebSession | str,
        payload: dict[str, Any],
        *,
        future: asyncio.Future[Any] | None = None,
    ) -> str:
        """Track a pending question request and append a replayable browser event."""
        session = self._resolve_session_arg(session)
        request_id = uuid.uuid4().hex
        pending = WebPendingQuestion(
            request_id=request_id,
            session_id=session.session_id,
            payload=normalize_question_payload(payload, request_id=request_id, session_id=session.session_id),
            future=future or _new_future(),
            created_at=_utc_now(),
        )
        session.pending_questions[request_id] = pending
        session.events.append("question.request", pending.to_dict())
        return request_id

    def get_pending_question(
        self,
        request_id: str,
        *,
        session_id: str | None = None,
    ) -> WebPendingQuestion | None:
        """Return a pending question when it exists and belongs to the expected session."""
        for session in self._sessions.values():
            pending = session.pending_questions.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return None
            return pending
        return None

    def resolve_question(
        self,
        request_id: str,
        answer: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a pending question request across loaded sessions."""
        for session in self._sessions.values():
            pending = session.pending_questions.get(request_id)
            if pending is not None:
                if session_id is not None and pending.session_id != session_id:
                    return {"requestId": request_id, "resolved": False}
                session.pending_questions.pop(request_id)
                resolved_answer = question_answer_from_body(answer)
                _set_future_result(pending.future, resolved_answer)
                session.events.append(
                    "question.resolved",
                    {
                        "requestId": request_id,
                        "answer": resolved_answer,
                    },
                )
                return {"requestId": request_id, "resolved": True}
        return {"requestId": request_id, "resolved": False}

    def discard_question_request(self, request_id: str, *, session_id: str | None = None) -> None:
        """Remove a pending question without publishing a user-visible answer."""
        for session in self._sessions.values():
            pending = session.pending_questions.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return
            session.pending_questions.pop(request_id, None)
            return

    def cancel_question_request(self, request_id: str, *, session_id: str | None = None) -> None:
        """Resolve a pending question as canceled so browser state can clear it."""
        for session in self._sessions.values():
            pending = session.pending_questions.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return
            session.pending_questions.pop(request_id, None)
            answer = _canceled_question_answer()
            _set_future_result(pending.future, None)
            session.events.append(
                "question.resolved",
                {
                    "requestId": request_id,
                    "answer": answer,
                },
            )
            return

    async def request_mcp_elicitation(
        self,
        session: WebSession | str,
        server_name: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bridge an MCP elicitation request to the browser and await the user's answer.

        Runs on whatever loop invokes the MCP handler; resolution from the HTTP endpoint is
        cross-loop safe via ``_set_future_result``. Cancellation (turn interrupt / teardown)
        collapses to the MCP contract's ``{"action": "cancel"}``.
        """
        session = self._resolve_session_arg(session)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        request_id = self.add_elicitation_request(
            session,
            {
                "server": server_name,
                "message": params.get("message"),
                "url": params.get("url"),
                "mode": params.get("mode"),
                "requestedSchema": params.get("requestedSchema"),
                "schema": params.get("schema"),
            },
            future=future,
        )
        try:
            result = await future
        except asyncio.CancelledError:
            self.cancel_elicitation_request(request_id, session_id=session.session_id)
            return {"action": "cancel"}
        if isinstance(result, Mapping):
            return dict(result)
        return {"action": "cancel"}

    def add_elicitation_request(
        self,
        session: WebSession | str,
        payload: dict[str, Any],
        *,
        future: asyncio.Future[Any] | None = None,
    ) -> str:
        """Track a pending MCP elicitation request and append a replayable browser event."""
        session = self._resolve_session_arg(session)
        request_id = uuid.uuid4().hex
        pending = WebPendingElicitation(
            request_id=request_id,
            session_id=session.session_id,
            payload=normalize_elicitation_payload(payload, request_id=request_id, session_id=session.session_id),
            future=future or _new_future(),
            created_at=_utc_now(),
            schema=elicitation_schema_from_payload(payload),
        )
        session.pending_elicitations[request_id] = pending
        session.events.append("elicitation.request", pending.to_dict())
        return request_id

    def get_pending_elicitation(
        self,
        request_id: str,
        *,
        session_id: str | None = None,
    ) -> WebPendingElicitation | None:
        """Return a pending elicitation when it exists and belongs to the expected session."""
        for session in self._sessions.values():
            pending = session.pending_elicitations.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return None
            return pending
        return None

    def resolve_elicitation(
        self,
        request_id: str,
        result: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a pending elicitation request across loaded sessions."""
        for session in self._sessions.values():
            pending = session.pending_elicitations.get(request_id)
            if pending is not None:
                if session_id is not None and pending.session_id != session_id:
                    return {"requestId": request_id, "resolved": False}
                session.pending_elicitations.pop(request_id)
                _set_future_result(pending.future, dict(result))
                session.events.append(
                    "elicitation.resolved",
                    {
                        "requestId": request_id,
                        "answer": {"action": str(result.get("action", "cancel"))},
                    },
                )
                return {"requestId": request_id, "resolved": True}
        return {"requestId": request_id, "resolved": False}

    def discard_elicitation_request(self, request_id: str, *, session_id: str | None = None) -> None:
        """Remove a pending elicitation without publishing a user-visible answer."""
        for session in self._sessions.values():
            pending = session.pending_elicitations.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return
            session.pending_elicitations.pop(request_id, None)
            return

    def cancel_elicitation_request(self, request_id: str, *, session_id: str | None = None) -> None:
        """Resolve a pending elicitation as canceled so browser state can clear it."""
        for session in self._sessions.values():
            pending = session.pending_elicitations.get(request_id)
            if pending is None:
                continue
            if session_id is not None and pending.session_id != session_id:
                return
            session.pending_elicitations.pop(request_id, None)
            _set_future_result(pending.future, {"action": "cancel"})
            session.events.append(
                "elicitation.resolved",
                {
                    "requestId": request_id,
                    "answer": canceled_elicitation_answer(),
                },
            )
            return

    def cancel_pending_requests_for_session(
        self,
        session: WebSession | str,
        *,
        permission_result: bool = False,
        question_result: dict[str, str] | None = None,
    ) -> None:
        """Resolve and clear pending futures when the owning turn cannot continue."""
        session = self._resolve_session_arg(session)
        for pending in list(session.pending_elicitations.values()):
            session.pending_elicitations.pop(pending.request_id, None)
            _set_future_result(pending.future, {"action": "cancel"})
            session.events.append(
                "elicitation.resolved",
                {
                    "requestId": pending.request_id,
                    "answer": canceled_elicitation_answer(),
                },
            )
        for pending in list(session.pending_permissions.values()):
            session.pending_permissions.pop(pending.request_id, None)
            _set_future_result(pending.future, permission_result)
            session.events.append(
                "permission.resolved",
                {
                    "requestId": pending.request_id,
                    "answer": _canceled_permission_answer(),
                },
            )
        for pending in list(session.pending_questions.values()):
            session.pending_questions.pop(pending.request_id, None)
            answer = question_result if question_result is not None else _canceled_question_answer()
            _set_future_result(pending.future, question_result)
            session.events.append(
                "question.resolved",
                {
                    "requestId": pending.request_id,
                    "answer": answer,
                },
            )

    def classify_queued_input(self, session: WebSession | str, text: str) -> dict[str, Any]:
        """Classify mid-turn user input as a queued message or composer draft."""
        session = self._resolve_session_arg(session)
        stripped_text = text.strip()
        if not stripped_text or stripped_text.startswith(("/", "$", "!")):
            session.draft = text
            session.events.append(
                "draft.updated",
                {
                    "draft": session.draft,
                    "reason": "not_submittable_mid_turn",
                },
            )
            return {"accepted": False, "draft": session.draft}

        session.events.append(
            "queued-input.accepted",
            {
                "text": text,
                "draft": session.draft,
            },
        )
        session.queued_inputs.append(text)
        return {"accepted": True, "draft": session.draft}

    def enqueue_pipeline_input(
        self,
        session: WebSession | str,
        text: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue structured pipeline input through the same path the agent loop drains."""
        session = self._resolve_session_arg(session)
        payload: dict[str, Any] = {
            "text": text,
            "draft": session.draft,
        }
        if metadata:
            payload.update(dict(metadata))
        session.events.append("queued-input.accepted", payload)
        session.queued_inputs.append(text)
        return {"accepted": True, "draft": session.draft}

    def drain_queued_inputs_for_agent(self, session: WebSession | str, *, cwd: Path | str | None = None) -> list[str]:
        """Return and clear mid-turn text queued for the agent loop."""
        if isinstance(session, WebSession):
            resolved = session
        elif cwd is not None:
            resolved = self._sessions.get((str(cwd), session)) or self.get_session(session)
        else:
            resolved = self.get_session(session)
        if resolved is None:
            return []
        queued = list(resolved.queued_inputs)
        resolved.queued_inputs.clear()
        return queued

    def pop_next_queued_input(self, session: WebSession | str) -> str | None:
        """弹出最早的一条排队输入,用于“逐条、各自独立成 turn”的顺序处理。

        与 drain_queued_inputs_for_agent(整体排空、供旧的轮内批量注入)不同,这里每次
        只取队首一条,并发出 queued-input.removed(index=0)让前端移除对应 chip;随后由
        runtime 为这条启动独立 turn(自带 user.message 气泡)。队列为空时返回 None。
        """
        session = self._resolve_session_arg(session)
        if not session.queued_inputs:
            return None
        text = session.queued_inputs.pop(0)
        session.events.append("queued-input.removed", {"index": 0})
        return text

    def _validate_queued_index(self, session: WebSession, index: int, expected_text: str) -> None:
        """校验逐条排队操作的下标与预期文本；不匹配时抛 QueuedInputActionError。

        用“下标 + 预期原始文本”双重定位：队列在工具调用后可能被整体 drain，此时下标会
        失效或指向不同内容，返回 409 让前端重取并重渲染。文本按原始值比较（不 trim），
        因为队列存储的是入队时的原始文本。
        """
        if index < 0 or index >= len(session.queued_inputs):
            raise QueuedInputActionError("queued input index out of range", 409)
        if session.queued_inputs[index] != expected_text:
            raise QueuedInputActionError("queued input changed; please retry", 409)

    def delete_queued_input(self, session: WebSession | str, index: int, *, expected_text: str) -> dict[str, Any]:
        """从队列移除指定排队项。"""
        session = self._resolve_session_arg(session)
        self._validate_queued_index(session, index, expected_text)
        session.queued_inputs.pop(index)
        session.events.append("queued-input.removed", {"index": index})
        return {"removed": True, "index": index}

    def edit_queued_input(
        self, session: WebSession | str, index: int, *, text: str, expected_text: str
    ) -> dict[str, Any]:
        """修改指定排队项的文本。"""
        session = self._resolve_session_arg(session)
        self._validate_queued_index(session, index, expected_text)
        session.queued_inputs[index] = text
        session.events.append("queued-input.updated", {"index": index, "text": text})
        return {"updated": True, "index": index, "text": text}

    def steer_queued_input(self, session: WebSession | str, index: int, *, expected_text: str) -> dict[str, Any]:
        """“引导/立即插队”：把指定排队项立刻注入正在运行的 agent，先于批量排空。

        成功注入后同时发出 user.message（带唯一 messageId，避免与首条 prompt 的
        user-<turnId> 气泡冲突）与 queued-input.removed。若当前没有活跃 turn 或 agent
        暂不接受注入，则把文本回插到队首，保证消息不丢。
        """
        session = self._resolve_session_arg(session)
        self._validate_queued_index(session, index, expected_text)

        loop = session.active_agent_loop
        turn_task = session.active_turn_task
        turn_active = loop is not None and turn_task is not None and not turn_task.done()
        if not turn_active:
            raise QueuedInputActionError("no active turn to steer", 409)

        text = session.queued_inputs.pop(index)
        turn_id = session.active_turn_id
        message_id = "user-{}-steer-{}".format(turn_id or "t", uuid.uuid4().hex[:8])
        injected = bool(
            loop.try_inject_user_message(
                text,
                metadata={
                    "messageId": message_id,
                    "turnId": turn_id,
                    "source": "steer",
                },
            )
        )
        if not injected:
            # agent 当前不接受注入（如正处于终止步骤），回插队首，稍后由批量排空提交。
            session.queued_inputs.insert(0, text)
            return {"steered": False, "requeued": True, "index": index}

        session.events.append(
            "user.message",
            {
                "messageId": message_id,
                "turnId": turn_id,
                "text": text,
                "imageIds": [],
                "fileRefs": [],
                "source": "steer",
            },
        )
        session.events.append("queued-input.removed", {"index": index})
        return {"steered": True, "index": index, "injected": True, "messageId": message_id}

    def _unique_session_key_for_bare_ref(self, ref: str) -> tuple[str, str] | None:
        if not ref:
            return None
        entries = self.index.list_all_projects()
        keys = {(session.cwd, session.session_id) for session in self._sessions.values()}
        keys.update((entry.cwd, entry.session_id) for entry in entries)
        exact_matches = [key for key in keys if key[1] == ref]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if exact_matches:
            return None
        prefix_matches = [key for key in keys if key[1].startswith(ref)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        return None

    def _session_from_key(self, session_key: tuple[str, str]) -> WebSession | None:
        existing = self._sessions.get(session_key)
        if existing is not None:
            return existing
        for entry in self.index.list_all_projects():
            if (entry.cwd, entry.session_id) == session_key:
                return self._from_entry(entry)
        return None

    def _from_entry(self, entry: SessionEntry) -> WebSession:
        session_key = (entry.cwd, entry.session_id)
        existing = self._sessions.get(session_key)
        if existing is not None:
            # 标题在会话开始就冻结、不受压缩影响(Bug 2):压缩会重写 session.jsonl,
            # index 重扫得出的 auto_title 可能被换成保留尾部里的另一条提问(甚至
            # [Conversation Summary])。因此内存标题只在两种情况下才更新——
            #   1) 用户显式重命名(entry.name,目录元数据 name);
            #   2) 内存标题尚为空,需首次从 index 捕获实时首条提问(此时未被压缩污染)。
            # 其余一律保留内存里已冻结的标题。(这也涵盖了流水线会话:其内存标题来自
            # apply_pipeline_auto_title、index 恒为「(empty)」,不覆盖即可保住。)
            if entry.name:
                existing.title = entry.name
            elif (not existing.title or existing.title == "(empty)") and entry.auto_title:
                existing.title = entry.auto_title
            existing.git_branch = entry.git_branch
            return existing
        if entry.is_legacy:
            self.storage._ensure_directory_format(entry.cwd, entry.session_id)
        metadata = self.storage.read_metadata(entry.cwd, entry.session_id)
        web_metadata = _read_web_session_metadata(self.storage, entry.cwd, entry.session_id)
        file_time = _utc_from_timestamp(entry.mtime)
        # 标题优先级(Bug 2:会话开始冻结,不受压缩影响):
        #   显式重命名(entry.name)> 冻结于会话开始的 sidecar autoTitle
        #   > index 现扫的 auto_title(压缩重写 JSONL 后可能被污染)> 外来兜底 >(empty)。
        # 冻结值优先于 index 现扫值,是因为压缩会重写 session.jsonl、令 index 派生标题偏移。
        raw_sidecar_title = web_metadata.get("autoTitle")
        sidecar_title = (
            raw_sidecar_title.strip() if isinstance(raw_sidecar_title, str) and raw_sidecar_title.strip() else None
        )
        if entry.name:
            entry_title = entry.name
        elif sidecar_title:
            entry_title = sidecar_title
        elif entry.auto_title:
            entry_title = entry.auto_title
        else:
            entry_title = "(empty)"
        is_foreign = _is_foreign_session(self.storage, entry.cwd, entry.session_id)
        is_pipeline_replay = _pipeline_display_replay_path(self.storage, entry.cwd, entry.session_id).exists()
        raw_sidecar_updated_at = web_metadata.get("updatedAt")
        sidecar_updated_at = (
            raw_sidecar_updated_at
            if isinstance(raw_sidecar_updated_at, str) and _timestamp_from_utc(raw_sidecar_updated_at) > 0
            else None
        )
        if is_foreign and (not entry_title or entry_title == "(empty)"):
            short = entry.session_id[:8]
            entry_title = _("Pipeline · {}").format(short) if is_pipeline_replay else _("Session · {}").format(short)
        session = WebSession(
            session_id=entry.session_id,
            cwd=entry.cwd,
            mode=_mode_from_metadata_or_sidecar(
                self.storage,
                entry.cwd,
                entry.session_id,
                web_metadata.get("mode"),
            ),
            pipeline_name=(
                web_metadata.get("pipelineName") if isinstance(web_metadata.get("pipelineName"), str) else None
            ),
            context_id=web_metadata.get("contextId") if isinstance(web_metadata.get("contextId"), str) else None,
            task_id=web_metadata.get("taskId") if isinstance(web_metadata.get("taskId"), str) else None,
            allow_user_escapes=_normalize_allow_user_escapes(web_metadata.get("allowUserEscapes")),
            permission_mode=_permission_mode_from_metadata(web_metadata.get("permissionMode")),
            provider=web_metadata.get("provider") if isinstance(web_metadata.get("provider"), str) else None,
            model=web_metadata.get("model") if isinstance(web_metadata.get("model"), str) else None,
            effort=web_metadata.get("effort") if isinstance(web_metadata.get("effort"), str) else None,
            thinking_enabled=(
                web_metadata.get("thinkingEnabled") if isinstance(web_metadata.get("thinkingEnabled"), bool) else None
            ),
            origin="foreign" if is_foreign else "web",
            status="idle",
            title=entry_title,
            git_branch=entry.git_branch,
            created_at=(metadata.created_at if metadata and metadata.created_at else file_time),
            updated_at=(
                sidecar_updated_at or (metadata.updated_at if metadata and metadata.updated_at else None) or file_time
            ),
            pinned=web_metadata.get("pinned") is True,
            pinned_at=(web_metadata.get("pinnedAt") if isinstance(web_metadata.get("pinnedAt"), str) else None),
            archived=web_metadata.get("archived") is True,
            unread=web_metadata.get("unread") is True,
        )
        session.read_only = is_foreign and session.mode == "pipeline"
        self._sessions[session_key] = session
        return session


def __getattr__(name: str) -> Any:
    """惰性暴露 ``session_titler`` 子模块。

    顶层导入会触发 session_manager → session_titler → runtime/diagram_optimizer →
    session_manager 的导入循环(冷启动 ``iac_code.web.app`` 时 runtime 尚未定义
    ``WebModelSelection``),故延迟到首次属性访问——此时各模块均已加载。
    ``schedule_llm_title`` 内部走同名函数级导入拿到同一模块对象,测试对该模块
    ``generate_session_title`` 的 patch 因此可见。
    """
    if name == "session_titler":
        from iac_code.web import session_titler

        return session_titler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
