"""StepExecutor — uses AgentLoop to execute a single pipeline step."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from iac_code.agent.message import ContentBlock, Message
from iac_code.agent.system_prompt import SECTION_BUILDERS, build_base_sections
from iac_code.mcp.prompt_dispatch import mcp_prompt_command_stream
from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
from iac_code.pipeline.engine.completion_guard_state import (
    ensure_completion_guard_state,
    record_completion_guard_tool_result,
    record_ros_deploy_observed_stack,
)
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.events import PipelineEvent
from iac_code.pipeline.engine.observability import PipelineObservability
from iac_code.pipeline.engine.recovery import last_successful_tool_input, reconstruct_completion_guard_state
from iac_code.pipeline.engine.step_spec import IncludeExcludeConfig, LoadedPipeline, StepSpec, render_prompt
from iac_code.pipeline.engine.types import StepConfig, StepResult, StepStatus
from iac_code.services.session_layout import (
    SESSION_LAYOUT_VERSION_V2,
    SessionPaths,
    ensure_session_owned_dir,
    ensure_session_owned_parent,
    require_supported_session_layout,
)
from iac_code.services.session_usage import SessionUsageStore
from iac_code.services.telemetry.names import PipelineAttr
from iac_code.tools.base import ToolRegistry
from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY
from iac_code.types.stream_events import (
    ContextUsageEvent,
    MessageEndEvent,
    ResourceObservedEvent,
    StreamEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

logger = logging.getLogger(__name__)


def _content_blocks_text(content: str | list[ContentBlock]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        result = getattr(block, "content", None)
        if isinstance(result, str):
            parts.append(result)
    return "\n".join(parts)


def _completion_guard_tool_result_content(event: ToolResultEvent) -> str:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    raw_path = metadata.get(EXTERNALIZED_RESULT_PATH_METADATA_KEY)
    if not isinstance(raw_path, str) or not raw_path:
        return event.result
    try:
        return Path(raw_path).read_text(encoding="utf-8")
    except OSError:
        logger.warning("Failed to read externalized tool result for completion guard", exc_info=True)
        return event.result


def _is_substantive_conclusion(conclusion: Any) -> bool:
    """A step may only be marked completed when its conclusion carries content."""
    return isinstance(conclusion, dict) and bool(conclusion)


@dataclass
class StepAgentLoopContext:
    """AgentLoop context built by the same path used for step execution."""

    agent_loop: Any | None
    initial_prompt: str | list[ContentBlock]
    resume_messages: list[Message]
    completion_guard_state: dict[str, Any]
    restored_step_result: StepResult | None = None


class StepExecutor:
    """Executes a single pipeline step by wrapping an AgentLoop."""

    def __init__(
        self,
        provider_manager: Any,
        base_tool_registry: ToolRegistry,
        pipeline: LoadedPipeline,
        pipeline_dir: Path,
        session_storage: Any = None,
        root_session_storage: Any = None,
        cwd: str | None = None,
        pause_event: asyncio.Event | None = None,
        permission_context_getter: Callable[[], Any] | None = None,
        memory_content_getter: Callable[[], str] | None = None,
        auto_trigger_skills: list[Any] | None = None,
        surface: str = "repl",
        tool_context_env_overrides: dict[str, str] | None = None,
        aliyun_delegated_executor_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._provider_manager = provider_manager
        self._base_tool_registry = base_tool_registry
        self._pipeline = pipeline
        self._pipeline_dir = pipeline_dir
        self._session_storage = session_storage
        self._root_session_storage = root_session_storage or session_storage
        self._cwd = cwd
        self._pause_event = pause_event
        self._permission_context_getter = permission_context_getter
        self._memory_content_getter = memory_content_getter
        self._auto_trigger_skills = auto_trigger_skills or []
        self._surface = surface
        self._tool_context_env_overrides = dict(tool_context_env_overrides or {})
        self._aliyun_delegated_executor_factory = aliyun_delegated_executor_factory
        self._current_agent_loop = None
        self._telemetry_correlation: dict[str, str] = {}
        self._telemetry_scope: dict[str, str | int] = {}
        pipeline_name = getattr(pipeline, "name", "")
        if not isinstance(pipeline_name, str):
            pipeline_name = ""
        self._observability = PipelineObservability(
            pipeline_name=pipeline_name,
            session_id="",
            cwd=self._cwd or "",
        )

    def set_telemetry_correlation(
        self,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        self._telemetry_correlation = {
            key: value
            for key, value in {
                PipelineAttr.RUN_ID: pipeline_run_id,
            }.items()
            if value
        }
        self._observability.set_correlation(
            task_id=task_id,
            context_id=context_id,
            pipeline_run_id=pipeline_run_id,
        )

    def set_telemetry_scope(
        self,
        *,
        parent_step_id: str | None = None,
        sub_pipeline_name: str | None = None,
        sub_pipeline_id: str | None = None,
        candidate_index: int | None = None,
    ) -> None:
        self._telemetry_scope = {
            key: value
            for key, value in {
                PipelineAttr.PARENT_STEP_ID: parent_step_id,
                PipelineAttr.SUB_PIPELINE_NAME: sub_pipeline_name,
                PipelineAttr.SUB_PIPELINE_ID: sub_pipeline_id,
                PipelineAttr.CANDIDATE_INDEX: candidate_index,
            }.items()
            if value is not None
        }

    def _agent_telemetry_attributes(self, step: StepSpec) -> dict[str, str | int]:
        attributes: dict[str, str | int] = {
            PipelineAttr.NAME: self._pipeline.name,
            **self._telemetry_correlation,
            **self._telemetry_scope,
        }
        if PipelineAttr.SUB_PIPELINE_ID in self._telemetry_scope:
            attributes[PipelineAttr.SUB_STEP_ID] = step.step_id
        else:
            attributes[PipelineAttr.STEP_ID] = step.step_id
        return attributes

    @property
    def current_agent_loop(self):
        """The currently executing AgentLoop, or None if no step is running."""
        return self._current_agent_loop

    async def execute(
        self,
        step: StepSpec,
        context: PipelineContext,
        session_id: str,
        user_message: str | list[ContentBlock] | None = None,
        *,
        attempt_id: str | None = None,
        transcript_id: str | None = None,
        resume_messages: list | None = None,
        precompleted_tools: dict[str, dict[str, Any]] | None = None,
        resume_candidate_selection: bool = False,
        rollback_targets: list[str] | None = None,
        rollback_count: int = 0,
        max_rollbacks: int = 5,
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        """Execute a step, yielding AgentLoop events and a final StepResult."""
        preserved_selection = self._preserved_candidate_selection(
            step,
            context,
            resume_candidate_selection=resume_candidate_selection,
        )
        compact_candidate_selection = preserved_selection is not None
        step = self._step_for_surface(step, compact_candidate_selection=compact_candidate_selection)
        self._observability.session_id = session_id

        if step.on_enter:
            step.on_enter(context)

        agent_context = self.build_agent_loop_context(
            step,
            context,
            session_id,
            user_message=user_message,
            attempt_id=attempt_id,
            transcript_id=transcript_id,
            resume_messages=resume_messages,
            precompleted_tools=precompleted_tools,
            compact_candidate_selection=compact_candidate_selection,
            rollback_targets=rollback_targets,
            rollback_count=rollback_count,
            max_rollbacks=max_rollbacks,
        )
        if agent_context.restored_step_result is not None:
            restored_conclusion = agent_context.restored_step_result.conclusion
            if restored_conclusion is None:
                restored_conclusion = {}
            restored_conclusion = self._merge_preserved_candidate_selection(
                preserved_selection,
                restored_conclusion,
            )
            agent_context.restored_step_result = replace(
                agent_context.restored_step_result,
                conclusion=restored_conclusion,
            )
            context.set_conclusion(step.conclusion_field, restored_conclusion)
            if step.on_exit:
                step.on_exit(context, restored_conclusion)
            yield agent_context.restored_step_result
            return
        agent_loop = agent_context.agent_loop
        assert agent_loop is not None
        self._current_agent_loop = agent_loop
        # 步骤/候选刚启动时，首个 MessageEndEvent 往往要 7-8s 后才到（思考 + 工具往返），
        # 在此之前没有任何 ContextUsageEvent，前端只能回退到单一「普通会话」圆环（见问题 #3）。
        # 先按当前上下文用量抢发一次，让用量圆环立即带上正确的 step/候选名称。尽力而为：
        # 若该 loop 此刻还报不出用量，就退回原来的「首个 MessageEndEvent 后再发」行为。
        prime_context_usage = getattr(agent_loop, "get_context_usage", None)
        if callable(prime_context_usage):
            yield ContextUsageEvent(usage=prime_context_usage())
        completion_guard_state = agent_context.completion_guard_state

        complete_step_ids: set[str] = set()
        pending_tool_inputs: dict[str, dict[str, Any]] = {}
        pending_complete_input: dict[str, dict] = {}
        complete_step_input: dict | None = None
        terminal_failed_step_result: StepResult | None = None
        max_nudges = 2
        last_complete_step_error: str | None = None
        last_complete_step_input: dict | None = None

        async def consume_complete_step_events(
            stream: AsyncIterator[Any],
        ) -> AsyncGenerator[StreamEvent | PipelineEvent, None]:
            nonlocal complete_step_input
            nonlocal last_complete_step_error
            nonlocal last_complete_step_input
            nonlocal terminal_failed_step_result
            try:
                async for event in stream:
                    if isinstance(event, ToolUseStartEvent) and event.name == "complete_step":
                        complete_step_ids.add(event.tool_use_id)
                    elif isinstance(event, ToolUseEndEvent):
                        pending_tool_inputs[event.tool_use_id] = {
                            "tool_name": event.name,
                            "input": copy.deepcopy(event.input),
                        }
                        if event.tool_use_id in complete_step_ids:
                            pending_complete_input[event.tool_use_id] = event.input
                    elif isinstance(event, ResourceObservedEvent):
                        tool_record = pending_tool_inputs.get(event.tool_use_id or "")
                        if isinstance(tool_record, dict) and tool_record.get("tool_name") == "ros_deploy":
                            tool_input = tool_record.get("input")
                            if isinstance(tool_input, dict) and event.resource_type == "stack":
                                record_ros_deploy_observed_stack(
                                    completion_guard_state,
                                    tool_input=tool_input,
                                    stack_id=event.resource_id,
                                )
                    elif isinstance(event, ToolResultEvent):
                        tool_record = pending_tool_inputs.get(event.tool_use_id)
                        if isinstance(tool_record, dict):
                            tool_input_raw = tool_record.get("input")
                            tool_input: dict[str, Any] = tool_input_raw if isinstance(tool_input_raw, dict) else {}
                            record_completion_guard_tool_result(
                                completion_guard_state,
                                tool_name=str(tool_record.get("tool_name") or event.tool_name),
                                tool_input=tool_input,
                                content=_completion_guard_tool_result_content(event),
                                is_error=event.is_error,
                                cwd=self._cwd,
                                metadata=event.metadata,
                            )
                        if event.tool_use_id in complete_step_ids:
                            step_result = (event.metadata or {}).get("step_result")
                            if isinstance(step_result, StepResult) and step_result.status == StepStatus.FAILED:
                                terminal_failed_step_result = step_result
                            if not event.is_error:
                                if self._pause_event is not None and not self._pause_event.is_set():
                                    return
                                complete_step_input = pending_complete_input.get(event.tool_use_id)
                            else:
                                last_complete_step_error = event.result
                                last_complete_step_input = pending_complete_input.get(event.tool_use_id)
                    yield event
                    if isinstance(event, MessageEndEvent) and self._current_agent_loop is not None:
                        yield ContextUsageEvent(usage=self._current_agent_loop.get_context_usage())
            finally:
                aclose = getattr(stream, "aclose", None)
                if callable(aclose):
                    with contextlib.suppress(Exception):
                        await aclose()

        try:
            first_stream_had_event = False
            if agent_context.resume_messages and user_message is None:
                first_stream = agent_loop.continue_streaming()
            else:
                first_stream = await mcp_prompt_command_stream(
                    agent_loop=agent_loop,
                    commands=self._auto_trigger_skills,
                    prompt=agent_context.initial_prompt,
                    session_id=transcript_id or session_id,
                )
                if first_stream is None:
                    first_stream = agent_loop.run_streaming(agent_context.initial_prompt)
            async for event in consume_complete_step_events(first_stream):
                first_stream_had_event = True
                yield event

            nudge_count = 0
            skip_resume_nudge = (
                bool(agent_context.resume_messages) and user_message is None and not first_stream_had_event
            )
            while (
                complete_step_input is None
                and terminal_failed_step_result is None
                and nudge_count < max_nudges
                and not skip_resume_nudge
            ):
                nudge_count += 1
                self._observability.step_nudged(
                    step_id=step.step_id,
                    nudge_count=nudge_count,
                    max_nudges=max_nudges,
                    session_id=session_id,
                )
                logger.info(
                    "Pipeline step nudge issued: step_id=%s nudge_count=%d max_nudges=%d session_id=%s",
                    step.step_id,
                    nudge_count,
                    max_nudges,
                    session_id,
                    extra={
                        "pipeline": self._pipeline.name,
                        "step_id": step.step_id,
                        "nudge_count": nudge_count,
                        "max_nudges": max_nudges,
                        "session_id": session_id,
                    },
                )
                nudge_msg = self._build_complete_step_nudge(last_complete_step_error, last_complete_step_input, step)
                async for event in consume_complete_step_events(agent_loop.run_streaming(nudge_msg)):
                    yield event
            if (
                complete_step_input is None
                and terminal_failed_step_result is None
                and not skip_resume_nudge
                and self._should_try_fresh_complete_step_recovery(
                    last_complete_step_error,
                    last_complete_step_input,
                )
            ):
                recovery_context = self.build_agent_loop_context(
                    step,
                    context,
                    session_id,
                    user_message=None,
                    attempt_id=attempt_id,
                    transcript_id=transcript_id,
                    resume_messages=None,
                    precompleted_tools=None,
                    completion_guard_state_seed=completion_guard_state,
                    compact_candidate_selection=compact_candidate_selection,
                    rollback_targets=rollback_targets,
                    rollback_count=rollback_count,
                    max_rollbacks=max_rollbacks,
                )
                recovery_loop = recovery_context.agent_loop
                assert recovery_loop is not None
                self._current_agent_loop = recovery_loop
                recovery_msg = self._build_fresh_complete_step_recovery_nudge(
                    last_complete_step_error,
                    last_complete_step_input,
                    step,
                )
                async for event in consume_complete_step_events(recovery_loop.run_streaming(recovery_msg)):
                    yield event
        finally:
            self._current_agent_loop = None

        if terminal_failed_step_result is not None:
            step_result = terminal_failed_step_result
        elif complete_step_input is not None:
            conclusion = complete_step_input.get("conclusion")
            conclusion = self._merge_preserved_candidate_selection(preserved_selection, conclusion)
            if not _is_substantive_conclusion(conclusion):
                step_result = StepResult(
                    step_id=step.step_id,
                    status=StepStatus.FAILED,
                    error="complete_step reported success without a non-empty conclusion",
                )
            else:
                rollback = complete_step_input.get("rollback_request")
                rollback_tuple = (rollback["target_step"], rollback["reason"]) if rollback else None
                step_result = StepResult(
                    step_id=step.step_id,
                    status=StepStatus.COMPLETED,
                    conclusion=conclusion,
                    rollback_request=rollback_tuple,
                )
                context.set_conclusion(step.conclusion_field, conclusion)
                if step.on_exit:
                    step.on_exit(context, conclusion)
        else:
            step_result = StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error="No conclusion extracted",
            )

        yield step_result

    def build_agent_loop_context(
        self,
        step: StepSpec,
        context: PipelineContext,
        session_id: str,
        user_message: str | list[ContentBlock] | None = None,
        *,
        attempt_id: str | None = None,
        transcript_id: str | None = None,
        resume_messages: list | None = None,
        precompleted_tools: dict[str, dict[str, Any]] | None = None,
        completion_guard_state_seed: dict[str, Any] | None = None,
        compact_candidate_selection: bool = False,
        rollback_targets: list[str] | None = None,
        rollback_count: int = 0,
        max_rollbacks: int = 5,
    ) -> StepAgentLoopContext:
        """Build the AgentLoop exactly as ``execute`` would, without running it."""
        step = self._step_for_surface(step, compact_candidate_selection=compact_candidate_selection)
        initial_prompt = user_message or f"请完成当前步骤：{step.step_id}。"

        repaired_messages = list(resume_messages or [])
        completion_guard_state: dict[str, Any] = ensure_completion_guard_state(
            reconstruct_completion_guard_state(repaired_messages)
        )
        if self._cwd:
            completion_guard_state["cwd"] = self._cwd
        if precompleted_tools:
            completion_guard_state["successful_tools"].update(precompleted_tools.keys())
            completion_guard_state["tool_results"].update(precompleted_tools)
        if completion_guard_state_seed:
            seed = ensure_completion_guard_state(completion_guard_state_seed)
            completion_guard_state["successful_tools"].update(seed.get("successful_tools", set()))
            completion_guard_state["tool_results"].update(seed.get("tool_results", {}))
            completion_guard_state["tool_result_records"].extend(seed.get("tool_result_records", []))
            owned_stack_ids = seed.get("ros_deploy_owned_stack_ids")
            if isinstance(owned_stack_ids, dict):
                completion_guard_state.setdefault("ros_deploy_owned_stack_ids", {}).update(
                    copy.deepcopy(owned_stack_ids)
                )

        build_tool_kwargs: dict[str, Any] = {
            "compact_candidate_selection": compact_candidate_selection,
            "rollback_targets": rollback_targets,
            "rollback_count": rollback_count,
            "max_rollbacks": max_rollbacks,
        }
        try:
            parameters = inspect.signature(self._build_step_tools).parameters
        except (TypeError, ValueError):
            parameters = {}
        if not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            build_tool_kwargs = {key: value for key, value in build_tool_kwargs.items() if key in parameters}
        tool_registry = self._build_step_tools(
            step,
            context,
            _content_blocks_text(initial_prompt),
            completion_guard_state,
            **build_tool_kwargs,
        )
        restored_step_result = self._restore_completed_step_result(step, tool_registry, repaired_messages)
        if restored_step_result is not None:
            return StepAgentLoopContext(
                agent_loop=None,
                initial_prompt=initial_prompt,
                resume_messages=repaired_messages,
                completion_guard_state=completion_guard_state,
                restored_step_result=restored_step_result,
            )

        system_prompt = self._build_full_system_prompt(step, context)

        from iac_code.agent.agent_loop import AgentLoop

        agent_session_id = transcript_id or session_id
        session_usage_store = None
        result_storage_dir = None
        transcript_image_cache_dir = None
        audit_log_path = None
        if (
            transcript_id
            and self._root_session_storage is not None
            and hasattr(self._root_session_storage, "session_dir")
        ):
            root_session_dir = Path(self._root_session_storage.session_dir(self._cwd or "", session_id))
            if require_supported_session_layout(root_session_dir) == SESSION_LAYOUT_VERSION_V2:
                session_paths = SessionPaths.require_supported(root_session_dir)
                transcript_dir = ensure_session_owned_dir(root_session_dir, session_paths.transcript_dir(transcript_id))
                result_storage_dir = ensure_session_owned_dir(
                    root_session_dir,
                    session_paths.transcript_tool_results_dir(transcript_id),
                )
                transcript_image_cache_dir = ensure_session_owned_dir(
                    root_session_dir,
                    session_paths.transcript_image_cache_dir(transcript_id),
                )
                audit_log_path = session_paths.transcript_permission_audit_path(transcript_id)
                usage_path = session_paths.transcript_usage_path(transcript_id)
                ensure_session_owned_parent(root_session_dir, audit_log_path)
                ensure_session_owned_parent(root_session_dir, usage_path)
                assert transcript_dir == session_paths.transcript_dir(transcript_id)
                session_usage_store = SessionUsageStore(path_provider=lambda _cwd, _sid, path=usage_path: path)
        step_skill_roots = self._resolve_step_skill_roots(step)
        trusted_read_roots = list(step_skill_roots)
        if result_storage_dir is not None:
            trusted_read_roots.append(str(result_storage_dir))
        if transcript_image_cache_dir is not None:
            trusted_read_roots.append(str(transcript_image_cache_dir))
        agent_loop = AgentLoop(
            provider_manager=self._provider_manager,
            system_prompt=system_prompt,
            tool_registry=tool_registry,
            max_turns=step.max_agent_turns,
            session_storage=self._session_storage,
            session_usage_store=session_usage_store,
            session_id=agent_session_id,
            root_session_id=session_id if transcript_id else None,
            transcript_id=transcript_id,
            result_storage_dir=result_storage_dir,
            audit_log_path=audit_log_path,
            resume_messages=repaired_messages or None,
            cwd=self._cwd,
            pause_event=self._pause_event,
            permission_context_getter=self._permission_context_getter,
            auto_trigger_skills=self._resolve_auto_trigger_skills(step),
            tool_context_trusted_read_directories=trusted_read_roots,
            tool_context_relative_read_directories=step_skill_roots,
            pipeline_mode=True,
            tool_context_env_overrides=self._tool_context_env_overrides,
            telemetry_attributes=self._agent_telemetry_attributes(step),
        )
        return StepAgentLoopContext(
            agent_loop=agent_loop,
            initial_prompt=initial_prompt,
            resume_messages=repaired_messages,
            completion_guard_state=completion_guard_state,
        )

    @staticmethod
    def _restore_completed_step_result(
        step: StepSpec,
        tool_registry: ToolRegistry,
        messages: list[Message],
    ) -> StepResult | None:
        complete_step_input = last_successful_tool_input(messages, "complete_step")
        if complete_step_input is None:
            return None

        normalized_input = copy.deepcopy(complete_step_input)
        complete_step_tool = tool_registry.get("complete_step")
        if isinstance(complete_step_tool, CompleteStepTool):
            if complete_step_tool.validate_completion_input(normalized_input) is not None:
                return None
        elif complete_step_tool is not None:
            complete_step_tool.normalize_input(normalized_input)

        conclusion = normalized_input.get("conclusion")
        if not _is_substantive_conclusion(conclusion):
            return None
        rollback = normalized_input.get("rollback_request")
        rollback_tuple = None
        if isinstance(rollback, dict) and rollback.get("target_step") and rollback.get("reason"):
            rollback_tuple = (str(rollback["target_step"]), str(rollback["reason"]))

        return StepResult(
            step_id=step.step_id,
            status=StepStatus.COMPLETED,
            conclusion=conclusion,
            rollback_request=rollback_tuple,
        )

    def _build_full_system_prompt(self, step: StepSpec, context: PipelineContext) -> str:
        sections_config = step.base_prompt_sections or self._pipeline.base_prompt_sections
        resolved_sections = self._resolve_include_exclude(sections_config, list(SECTION_BUILDERS.keys()))
        memory_content = self._memory_content_getter() if self._memory_content_getter else ""
        base_prompt = build_base_sections(
            resolved_sections,
            cwd=self._cwd or "",
            memory_content=memory_content,
            provider_display=self._runtime_provider_display(),
            model=self._runtime_model(),
        )

        prompt_file = step.prompt_file_for_surface(self._surface)
        prompt_path = self._pipeline_dir / prompt_file
        step_prompt = prompt_path.read_text(encoding="utf-8") if prompt_file else ""
        rendered_step_prompt = render_prompt(
            step_prompt,
            context,
            step.context_fields,
            extra_context={"step_config": step.config},
        )

        skill_content = ""
        if step.skill:
            skill_content = self._resolve_skill_prompt(step.skill) or ""

        parts = [p for p in [base_prompt, skill_content, rendered_step_prompt] if p]
        return "\n\n---\n\n".join(parts)

    def _step_for_surface(self, step: StepSpec, *, compact_candidate_selection: bool = False) -> StepSpec:
        conclusion_schema = step.conclusion_schema_for_surface(self._surface)
        if compact_candidate_selection:
            conclusion_schema = self._candidate_selection_response_schema(conclusion_schema)
        if conclusion_schema is step.conclusion_schema:
            return step
        return replace(step, conclusion_schema=conclusion_schema)

    def _preserved_candidate_selection(
        self,
        step: StepSpec,
        context: PipelineContext | None,
        *,
        resume_candidate_selection: bool,
    ) -> dict[str, Any] | None:
        if not resume_candidate_selection:
            return None
        override = step.surface_overrides.get(self._surface)
        if step.ui_mode != "candidate_selection" or override is None or override.conclusion_schema is None:
            return None
        if context is None:
            return None
        if context.get_field(step.conclusion_field).stale:
            return None
        current = context.get_conclusion(step.conclusion_field)
        if not isinstance(current, dict):
            return None
        if not isinstance(current.get("options"), list) or not isinstance(current.get("user_input"), str):
            return None
        return copy.deepcopy(current)

    @staticmethod
    def _candidate_selection_response_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(schema, dict):
            return schema
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return schema
        field_names = (
            "selected_candidate_name",
            "selected_candidate_index",
            "selected_evaluated_candidate_index",
            "parameter_overrides",
        )
        selected_properties = {
            name: copy.deepcopy(properties[name]) for name in field_names if isinstance(properties.get(name), dict)
        }
        required = [
            name
            for name in (
                "selected_candidate_name",
                "selected_candidate_index",
                "selected_evaluated_candidate_index",
            )
            if name in selected_properties
        ]
        if not required:
            return schema
        return {
            "type": "object",
            "required": required,
            "additionalProperties": False,
            "properties": selected_properties,
        }

    @staticmethod
    def _merge_preserved_candidate_selection(
        preserved: dict[str, Any] | None,
        conclusion: Any,
    ) -> dict[str, Any]:
        current = conclusion if isinstance(conclusion, dict) else {}
        if preserved is None:
            return current
        merged = copy.deepcopy(preserved)
        merged.update(current)
        return merged

    def _runtime_provider_display(self) -> str:
        getter = getattr(self._provider_manager, "get_provider_display", None)
        if not callable(getter):
            return ""
        try:
            display = getter()
        except Exception:
            return ""
        return display if isinstance(display, str) else ""

    def _runtime_model(self) -> str:
        getter = getattr(self._provider_manager, "get_model_name", None)
        if not callable(getter):
            return ""
        try:
            model = getter()
        except Exception:
            return ""
        return model if isinstance(model, str) else ""

    def _resolve_auto_trigger_skills(self, step: StepSpec) -> list[Any] | None:
        """问题 2：step 自带 skill 时，禁用 auto_trigger（避免重复加载）。"""
        if step.skill:
            return None
        return self._auto_trigger_skills or None

    @staticmethod
    def _build_complete_step_nudge(
        error: str | None,
        invalid_input: dict | None,
        step: StepSpec | None = None,
    ) -> str:
        step_line = f"当前步骤：{step.step_id}\n" if step is not None else ""
        schema_hint = StepExecutor._complete_step_schema_hint(step)
        example = json.dumps(
            {"conclusion": StepExecutor._example_from_schema(step.conclusion_schema if step else None)},
            ensure_ascii=False,
        )
        wrapper_instruction = (
            f"{step_line}"
            'complete_step 的参数必须是 {"conclusion": {...}}，不要提交空参数，'
            "也不要把 conclusion 内的字段放在工具参数顶层。\n"
            f"{schema_hint}\n"
            f"参数外层示例：{example}"
        )
        if not error:
            return (
                "你还没有成功调用 complete_step 工具提交结论。请立即调用 complete_step 完成当前步骤。\n"
                f"{wrapper_instruction}"
            )

        invalid_json = json.dumps(invalid_input or {}, ensure_ascii=False)
        if "ask_user_question" in error:
            return (
                f"上一次 complete_step 调用失败：{error}\n"
                f"不要重复上一次无效参数：{invalid_json}\n"
                "请先调用 ask_user_question 向用户澄清；收到 ask_user_question 的工具结果后，"
                '再用 {"conclusion": {...}} 调用 complete_step。\n'
                "不要再次直接调用 complete_step。"
            )
        return (
            f"上一次 complete_step 调用失败：{error}\n"
            f"不要重复上一次无效参数：{invalid_json}\n"
            f"{wrapper_instruction}\n"
            "请立即用修正后的参数调用 complete_step。"
        )

    @staticmethod
    def _should_try_fresh_complete_step_recovery(error: str | None, invalid_input: dict | None) -> bool:
        if not error:
            return False
        if invalid_input == {}:
            return True
        return (
            "'conclusion' is a required property" in error
            or '"conclusion" is a required property' in error
            or "complete_step 的参数必须" in error
        )

    @staticmethod
    def _build_fresh_complete_step_recovery_nudge(
        error: str | None,
        invalid_input: dict | None,
        step: StepSpec,
    ) -> str:
        invalid_json = json.dumps(invalid_input or {}, ensure_ascii=False)
        example = json.dumps(
            {"conclusion": StepExecutor._example_from_schema(step.conclusion_schema)},
            ensure_ascii=False,
        )
        return (
            "重新执行当前步骤的收口。\n"
            f"当前步骤：{step.step_id}\n"
            f"之前多次 complete_step 调用失败：{error or '未提交有效结论'}\n"
            f"失败参数：{invalid_json}\n"
            "不要再提交空参数 `{}`。不要解释、不要闲聊、不要重复无效工具调用。\n"
            "请基于当前 pipeline context 和当前步骤要求，直接生成完整结构化结论。\n"
            '你现在只能用 {"conclusion": {...}} 调用 complete_step 一次。\n'
            f"{StepExecutor._complete_step_schema_hint(step)}\n"
            f"完整参数示例：{example}\n"
            "示例中的值需要替换为本步骤真实结论。"
        )

    @staticmethod
    def _complete_step_schema_hint(step: StepSpec | None) -> str:
        schema = step.conclusion_schema if step else None
        if not schema:
            return "当前 conclusion 必须是非空对象；请根据当前步骤的输出要求填写完整结构化结论。"
        compact = StepExecutor._compact_schema(schema)
        return "当前 conclusion 必须符合此 schema 摘要：\n" + json.dumps(compact, ensure_ascii=False)

    @staticmethod
    def _compact_schema(schema: Any, *, depth: int = 0) -> Any:
        if depth > 4 or not isinstance(schema, dict):
            return schema

        compact: dict[str, Any] = {}
        for key in ("type", "required", "enum", "description", "minItems"):
            if key in schema:
                compact[key] = schema[key]

        properties = schema.get("properties")
        if isinstance(properties, dict):
            compact["properties"] = {
                name: StepExecutor._compact_schema(value, depth=depth + 1) for name, value in properties.items()
            }

        items = schema.get("items")
        if isinstance(items, dict):
            compact["items"] = StepExecutor._compact_schema(items, depth=depth + 1)

        return compact or schema

    @staticmethod
    def _example_from_schema(schema: Any) -> Any:
        if not isinstance(schema, dict):
            return {"result": "<按当前步骤要求填写>"}

        schema_type = schema.get("type")
        if schema_type == "object" or "properties" in schema:
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            keys = required or list(properties)[:3]
            if not keys:
                return {"result": "<按当前步骤要求填写>"}
            return {str(key): StepExecutor._example_from_schema(properties.get(key)) for key in keys}
        if schema_type == "array":
            return [StepExecutor._example_from_schema(schema.get("items"))]
        if schema_type == "string":
            return "<string>"
        if schema_type == "integer":
            return 0
        if schema_type == "number":
            return 0
        if schema_type == "boolean":
            return True
        return "<value>"

    def _resolve_skill_content(self, skill_name: str) -> str | None:
        """Resolve skill content: first from pipeline co-located skills, then bundled."""
        content = self._pipeline.skills.get(skill_name)
        if content:
            return content

        try:
            from iac_code.skills.bundled import get_bundled_skills

            for skill_def in get_bundled_skills():
                if skill_def.name == skill_name:
                    return skill_def.content
        except ImportError:
            pass

        return None

    def _resolve_skill_prompt(self, skill_name: str) -> str | None:
        """Resolve skill prompt content with its base directory for relative references."""
        content = self._pipeline.skills.get(skill_name)
        if content:
            return self._with_skill_base_directory(content, self._pipeline.skill_roots.get(skill_name, ""))

        try:
            from iac_code.skills.bundled import get_bundled_skills

            for skill_def in get_bundled_skills():
                if skill_def.name == skill_name:
                    return self._with_skill_base_directory(skill_def.content, skill_def.skill_root)
        except ImportError:
            pass

        return None

    def _resolve_step_skill_roots(self, step: StepSpec) -> list[str]:
        if not step.skill:
            return []
        root = self._resolve_skill_root(step.skill)
        return [root] if root else []

    def _resolve_skill_root(self, skill_name: str) -> str:
        root = self._pipeline.skill_roots.get(skill_name, "")
        if root:
            return root

        try:
            from iac_code.skills.bundled import get_bundled_skills

            for skill_def in get_bundled_skills():
                if skill_def.name == skill_name:
                    return skill_def.skill_root
        except ImportError:
            pass

        return ""

    @staticmethod
    def _with_skill_base_directory(content: str, skill_root: str) -> str:
        if not skill_root:
            return content
        return (
            f"Base directory for this skill: {skill_root}\n"
            "Resolve relative paths in this skill relative to the base directory above.\n\n"
            f"{content}"
        )

    def _build_step_tools(
        self,
        step: StepSpec,
        context: PipelineContext,
        user_message: str = "",
        completion_guard_state: dict[str, Any] | None = None,
        *,
        compact_candidate_selection: bool = False,
        rollback_targets: list[str] | None = None,
        rollback_count: int = 0,
        max_rollbacks: int = 5,
    ) -> ToolRegistry:
        step = self._step_for_surface(step, compact_candidate_selection=compact_candidate_selection)
        if step.tools is None:
            registry = self._base_tool_registry.clone()
        else:
            if step.tools.include:
                registry = self._base_tool_registry.filter(step.tools.include)
                for name in step.tools.include:
                    if registry.get(name) is not None:
                        continue
                    tool_cls = self._pipeline.pipeline_tools.get(name)
                    if tool_cls is not None:
                        registry.register(self._instantiate_pipeline_tool(tool_cls, step))
            else:
                registry = self._base_tool_registry.clone()
            if step.tools.exclude:
                registry = registry.exclude(step.tools.exclude)

        step_config = StepConfig(
            step_id=step.step_id,
            conclusion_field=step.conclusion_field,
            forward=step.forward,
            auto_advance=step.auto_advance,
            complete_step_terminal=step.complete_step_terminal,
            max_agent_turns=step.max_agent_turns,
            conclusion_schema=step.conclusion_schema,
            rollback_targets=rollback_targets if rollback_targets is not None else [],
            max_conclusion_retries=step.max_conclusion_retries,
            rollback_count=rollback_count,
            max_rollbacks=max_rollbacks,
        )
        guard_state = ensure_completion_guard_state(
            completion_guard_state if completion_guard_state is not None else {}
        )
        guard_state["context_snapshot"] = context.snapshot()
        registry.register(
            CompleteStepTool(
                step_config,
                completion_guards=step.completion_guards,
                completion_guard_state=guard_state,
                user_message=user_message,
            )
        )

        inject_tools = step.inject_tools_for_surface(self._surface)
        if inject_tools:
            self._register_injectable_tools(registry, inject_tools, guard_state, step=step)

        return registry

    def _register_injectable_tools(
        self,
        registry: ToolRegistry,
        tool_names: list[str],
        completion_guard_state: dict[str, Any],
        *,
        step: StepSpec,
    ) -> None:
        from iac_code.pipeline.engine.ask_user_question_tool import AskUserQuestionTool
        from iac_code.pipeline.engine.show_diagram_tool import ShowArchitectureDiagramTool

        engine_tools: dict[str, type] = {
            "ask_user_question": AskUserQuestionTool,
            "show_architecture_diagram": ShowArchitectureDiagramTool,
        }
        for name in tool_names:
            tool_cls = self._pipeline.pipeline_tools.get(name)
            if tool_cls is None:
                tool_cls = engine_tools.get(name)
            if tool_cls is not None:
                if name == "ask_user_question":

                    def observe_question_answered(
                        tool_call_id: str | None, option_count: int, answer_type: str
                    ) -> None:
                        self._observability.question_answered(
                            step_id=step.step_id,
                            tool_call_id=tool_call_id,
                            option_count=option_count,
                            answer_type=answer_type,
                        )

                    registry.register(
                        AskUserQuestionTool(
                            completion_guard_state,
                            question_answered_observer=observe_question_answered,
                        )
                    )
                elif name == "ros_deploy":
                    registry.register(tool_cls(completion_guard_state=completion_guard_state))
                else:
                    registry.register(self._instantiate_pipeline_tool(tool_cls, step=None))

    def _instantiate_pipeline_tool(self, tool_cls: type, step: StepSpec | None) -> Any:
        try:
            parameters = inspect.signature(tool_cls).parameters
        except (TypeError, ValueError):
            parameters = {}
        if step is not None and "step_config" in parameters:
            return tool_cls(step_config=step.config)
        if "delegated_executor" in parameters and self._aliyun_delegated_executor_factory is not None:
            action = getattr(tool_cls, "action", "")
            return tool_cls(delegated_executor=self._aliyun_delegated_executor_factory(str(action)))
        return tool_cls()

    @staticmethod
    def _resolve_include_exclude(config: IncludeExcludeConfig, all_available: list[str]) -> list[str]:
        if config.include:
            base_set = config.include
        else:
            base_set = all_available
        if config.exclude:
            return [name for name in base_set if name not in config.exclude]
        return list(base_set)
