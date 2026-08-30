"""Web 独有:后台把第 4 步候选架构图从确定性草图优化为 LLM 版并持久化。

镜像 CLI show_architecture_diagram 的 facts-mode「草图→语义规划→重渲染」流,但整条编排只在 web 层
——CLI 那把工具在 a2a/web 面禁用。由 live pipeline envelope 流在 step-4 confirm_and_select 选择提示
出现时触发。优化结果写 diagram_cache,会话恢复只读缓存不重算。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from iac_code.pipeline.engine.architecture_graph import (
    render_ros_template_architecture,
    render_ros_template_architecture_views,
)
from iac_code.pipeline.engine.architecture_semantic_planning import (
    browser_mermaid_source,
    create_semantic_plan_for_architecture_with_llm,
)
from iac_code.web.diagram_cache import read_cached, template_hash, write_cached
from iac_code.web.diagrams import CandidateTemplate, iter_candidate_templates
from iac_code.web.outputs import TEMPLATE_SUFFIXES, is_template_content
from iac_code.web.runtime import WebModelSelection, model_selection_for_session

logger = logging.getLogger(__name__)

_ERROR_MERMAID_PREFIX = "graph TD\n  Error["
_MATERIALIZED_STEP_ID = "materialize_selected_candidate"
_MATERIALIZED_OPTIMIZATION_KEY = "materialized"


@dataclass(frozen=True)
class _OptimizationTarget:
    cache_key: int | str
    candidate_index: int | None
    optimization_key: str | None
    name: str
    template_content: str


def provider_overrides_from(selection: WebModelSelection) -> dict[str, Any]:
    """把会话模型选择映射成引擎 provider 覆盖 kwargs(仅放非空项,空则引擎走全局默认)。"""
    overrides: dict[str, Any] = {}
    if selection.provider:
        overrides["provider_key_override"] = selection.provider
        if selection.provider_api_key:
            overrides["credentials_override"] = {selection.provider: selection.provider_api_key}
    if selection.provider_base_url:
        overrides["base_url_override"] = selection.provider_base_url
    if selection.provider_config_override is not None:
        overrides["provider_config_override"] = selection.provider_config_override
    if selection.provider_config_frozen:
        overrides["ignore_llm_source"] = True
    return overrides


def _is_candidate_selection(envelope: Mapping[str, Any]) -> bool:
    """仅 confirm_and_select 的候选选择提示为真:input_required 且 options 带 candidate_index。"""
    if envelope.get("eventType") != "input_required":
        return False
    data = envelope.get("data")
    if not isinstance(data, dict):
        return False
    options = data.get("options")
    if not isinstance(options, list):
        return False
    return any(isinstance(opt, dict) and "candidate_index" in opt for opt in options)


def _is_materialized_confirmation(envelope: Mapping[str, Any]) -> bool:
    if envelope.get("eventType") != "input_required":
        return False
    data = envelope.get("data")
    step = envelope.get("step")
    if not isinstance(data, dict):
        return False
    step_id = (step.get("id") if isinstance(step, dict) else None) or data.get("stepId")
    return step_id == _MATERIALIZED_STEP_ID and data.get("kind") == "deployment_confirmation"


def _candidate_target(candidate: CandidateTemplate) -> _OptimizationTarget:
    return _OptimizationTarget(
        cache_key=candidate.index,
        candidate_index=candidate.index,
        optimization_key=None,
        name=candidate.name,
        template_content=candidate.template_content,
    )


def _materialized_target(session: Any, envelope: Mapping[str, Any]) -> _OptimizationTarget | None:
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    template_url = data.get("template_url")
    if not template_url:
        return None
    raw_path = Path(str(template_url))
    if raw_path.suffix.lower() not in TEMPLATE_SUFFIXES:
        return None
    cwd = Path(session.cwd).expanduser().resolve()
    resolved = (raw_path if raw_path.is_absolute() else cwd / raw_path).resolve()
    try:
        content = resolved.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if not is_template_content(content, raw_path.suffix.lower()):
        return None
    return _OptimizationTarget(
        cache_key=_MATERIALIZED_OPTIMIZATION_KEY,
        candidate_index=None,
        optimization_key=_MATERIALIZED_OPTIMIZATION_KEY,
        name=resolved.stem,
        template_content=content,
    )


class DiagramOptimizationCoordinator:
    """会话生命周期内共享的协调器:去重触发 + 管理在途任务。"""

    def __init__(self) -> None:
        self._inflight: set[tuple[str, int | str, str]] = set()

    def optimizing_indices(self, context_id: str | None) -> set[int | str]:
        """当前会话仍在后台优化的候选 index 集合(空 context_id → 空集)。"""
        if not context_id:
            return set()
        return {identity for (ctx, identity, _template_hash) in self._inflight if ctx == context_id}

    def maybe_trigger(self, session: Any, manager: Any, envelope: Mapping[str, Any]) -> None:
        context_id = getattr(session, "context_id", None)
        if not context_id:
            return
        targets: list[_OptimizationTarget]
        if _is_candidate_selection(envelope):
            targets = [_candidate_target(candidate) for candidate in iter_candidate_templates(manager, session)]
        elif _is_materialized_confirmation(envelope):
            materialized = _materialized_target(session, envelope)
            targets = [materialized] if materialized is not None else []
        else:
            return
        for target in targets:
            identity: int | str = target.optimization_key or target.candidate_index or 0
            key = (context_id, identity, template_hash(target.template_content))
            if key in self._inflight:
                continue
            if read_cached(context_id, target.cache_key, target.template_content) is not None:
                continue
            self._inflight.add(key)
            task = asyncio.create_task(self._optimize_one(session, context_id, target, key))
            tasks = getattr(session, "active_local_tasks", None)
            if isinstance(tasks, set):
                tasks.add(task)
                task.add_done_callback(tasks.discard)

    async def _optimize_one(
        self,
        session: Any,
        context_id: str,
        target: _OptimizationTarget,
        inflight_key: tuple[str, int | str, str],
    ) -> None:
        idx = target.candidate_index
        name = target.name
        event_identity: dict[str, Any] = {"candidateName": name}
        if idx is not None:
            event_identity["candidateIndex"] = idx
        if target.optimization_key is not None:
            event_identity["optimizationKey"] = target.optimization_key
        try:
            await session.events.publish("diagram.optimizing", event_identity)
            selection = model_selection_for_session(session)
            base = await asyncio.to_thread(render_ros_template_architecture, target.template_content)
            if base.mermaid_source.startswith(_ERROR_MERMAID_PREFIX):
                raise RuntimeError("draft render failed; nothing to optimize")
            plan = await create_semantic_plan_for_architecture_with_llm(
                base.architecture_context,
                target.template_content,
                model=selection.model,
                effort_override="none",
                **provider_overrides_from(selection),
            )
            if not plan:
                raise RuntimeError("empty semantic plan")
            multi = await asyncio.to_thread(
                render_ros_template_architecture_views, target.template_content, semantic_plan=plan
            )
            views: list[dict] = []
            for v in multi.views:
                raw = v.mermaid_source
                if not raw or raw.startswith(_ERROR_MERMAID_PREFIX) or raw.strip() == "graph TD":
                    continue
                views.append({"id": v.id, "title": v.title, "mermaidSource": browser_mermaid_source(raw)})
            if not views:
                raise RuntimeError("optimized render did not produce a usable diagram")
            await asyncio.to_thread(
                write_cached,
                context_id,
                target.cache_key,
                target.template_content,
                views,
                selection.model,
            )
            await session.events.publish(
                "diagram.optimized",
                {
                    **event_identity,
                    "status": "done",
                    "views": views,
                    "mermaidSource": views[0]["mermaidSource"],
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to optimize diagram for candidate %s", idx)
            try:
                await session.events.publish(
                    "diagram.optimized",
                    {**event_identity, "status": "failed"},
                )
            except Exception:
                logger.exception("Failed to publish diagram.optimized(failed) for candidate %s", idx)
        finally:
            self._inflight.discard(inflight_key)
