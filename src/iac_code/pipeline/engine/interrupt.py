"""InterruptController — LLM-based judge for user interrupt messages."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from iac_code.agent.message import ImageBlock
from iac_code.pipeline.engine.user_input import (
    IMAGE_INPUT_PLACEHOLDER,
    PipelineUserInput,
    normalize_pipeline_user_input,
)

logger = logging.getLogger(__name__)

# LLM judge calls typically take 2-8s, but in parallel-pipeline mode candidate
# LLM streams can hold the provider's connection pool until they finish the
# current turn (10-30s), forcing the judge to queue. 90s gives both candidate
# turns and the judge call enough headroom to complete on most providers.
JUDGE_TIMEOUT_SECONDS = 90


def _safe_truncate(text: str, max_chars: int = 500, suffix: str = "...", from_end: bool = False) -> str:
    """Truncate a string to at most max_chars codepoints, preserving CJK characters.

    Args:
        text: source string
        max_chars: max codepoint count
        suffix: appended when truncating from head (ignored when from_end=True)
        from_end: if True, take the last max_chars chars (semantics of `text[-N:]`)
    """
    if len(text) <= max_chars:
        return text
    if from_end:
        return text[-max_chars:]
    return text[:max_chars] + suffix


def _coerce_null(value: Any) -> Any:
    """Coerce LLM hallucinated string 'null' / 'Null' / 'NULL' to real None.

    Some LLM providers occasionally serialize JSON null as the literal
    string ``"null"``. Without this normalization, downstream code treats
    the value as truthy and either crashes (e.g. state_machine.rollback)
    or pollutes prompts (rollback_context becomes the literal "null").
    """
    if isinstance(value, str) and value.strip().lower() == "null":
        return None
    return value


@dataclass(frozen=True)
class InterruptVerdict:
    action: Literal["continue", "supplement", "hard_interrupt"]
    reason: str
    rollback_target: str | None = None
    candidate_scope: str | None = None
    supplement_target: str | None = None
    rollback_context: str | None = None
    paused: bool = False


class InterruptController:
    """Judges user interrupt messages using a parallel LLM call."""

    def __init__(
        self,
        provider_manager: Any,
        pipeline_state_getter: Callable[[], dict],
        pipeline_dir: Path | None = None,
    ) -> None:
        self._provider_manager = provider_manager
        self._get_state = pipeline_state_getter
        self._pipeline_dir = pipeline_dir

    async def judge(self, user_message: str | PipelineUserInput) -> InterruptVerdict:
        """Judge a user message. Returns verdict. Defaults to 'continue' on failure."""
        import time

        pipeline_input = normalize_pipeline_user_input(user_message)
        started = time.monotonic()
        logger.info("interrupt judge START: message=%r", pipeline_input.display_text[:200])
        try:
            verdict = await asyncio.wait_for(
                self._call_judge_llm(pipeline_input),
                timeout=JUDGE_TIMEOUT_SECONDS,
            )
            logger.info(
                "interrupt judge OK: action=%s rollback_target=%s candidate_scope=%s "
                "supplement_target=%s elapsed=%.2fs reason=%r",
                verdict.action,
                verdict.rollback_target,
                verdict.candidate_scope,
                verdict.supplement_target,
                time.monotonic() - started,
                verdict.reason[:200],
            )
            return verdict
        except (asyncio.TimeoutError, TimeoutError):
            elapsed = time.monotonic() - started
            logger.warning("interrupt judge TIMEOUT after %.1fs (limit=%ds)", elapsed, JUDGE_TIMEOUT_SECONDS)
            return InterruptVerdict(
                action="continue",
                reason=f"judge failed: timeout after {elapsed:.1f}s",
            )
        except Exception as e:
            logger.warning("interrupt judge FAILED: %s", e, exc_info=True)
            return InterruptVerdict(action="continue", reason=f"judge failed: {type(e).__name__}: {e}")

    async def _call_judge_llm(self, user_message: str | PipelineUserInput) -> InterruptVerdict:
        """Make the actual LLM call and parse the response."""
        from iac_code.providers.base import ContentBlock as ProviderContentBlock
        from iac_code.providers.base import Message as ProviderMessage

        pipeline_input = normalize_pipeline_user_input(user_message)
        state = self._get_state()
        system_prompt = self._build_judge_system_prompt(state)
        user_prompt = self._build_judge_user_prompt(pipeline_input, state)
        provider_content: str | list[ProviderContentBlock]
        if pipeline_input.has_images and isinstance(pipeline_input.content, list):
            provider_blocks = [ProviderContentBlock(type="text", text=user_prompt)]
            for block in pipeline_input.content:
                if isinstance(block, ImageBlock):
                    provider_blocks.append(
                        ProviderContentBlock(type="image", media_type=block.media_type, data=block.data)
                    )
            provider_content = provider_blocks
        else:
            provider_content = user_prompt

        max_attempts = 2
        last_response_text = ""
        for attempt in range(max_attempts):
            response = await self._provider_manager.complete(
                messages=[ProviderMessage(role="user", content=provider_content)],
                system=system_prompt,
            )
            last_response_text = response.text
            verdict = self._parse_verdict(response.text)
            if verdict is not None:
                return verdict
            logger.info("Retry judge call (%d/%d): hard_interrupt missing rollback_context", attempt + 1, max_attempts)

        # Retry exhausted — final attempt: try one more time with the last
        # response, accepting it even if rollback_context is missing.
        # Apply the same _coerce_null normalization the success path uses,
        # and gracefully handle JSON-decode errors instead of crashing (P-I7).
        text = last_response_text.strip()
        text = re.sub(r"^```\w*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Fallback parse failed: not JSON. raw=%r", _safe_truncate(text, max_chars=500))
            return InterruptVerdict(
                action="continue",
                reason=f"fallback parse failed: not JSON. raw={text[:120]!r}",
            )
        action = data.get("action", "hard_interrupt")
        if action not in ("continue", "supplement", "hard_interrupt"):
            action = "hard_interrupt"
        return InterruptVerdict(
            action=action,
            reason=data.get("reason", ""),
            rollback_target=_coerce_null(data.get("rollback_target")),
            candidate_scope=_coerce_null(data.get("candidate_scope")),
            supplement_target=_coerce_null(data.get("supplement_target")),
            rollback_context=_coerce_null(data.get("rollback_context")),
        )

    def _build_judge_system_prompt(self, state: dict) -> str:
        """Build the system prompt for the judge LLM call.

        Looks for interrupt_judge.md in: 1) pipeline_dir/prompts/, 2) engine bundled prompts.
        Appends pipeline name context from state.
        """
        prompt_content: str | None = None

        if self._pipeline_dir:
            pipeline_prompt = self._pipeline_dir / "prompts" / "interrupt_judge.md"
            if pipeline_prompt.exists():
                prompt_content = pipeline_prompt.read_text(encoding="utf-8")

        if not prompt_content:
            engine_prompt = Path(__file__).parent / "prompts" / "interrupt_judge.md"
            if engine_prompt.exists():
                prompt_content = engine_prompt.read_text(encoding="utf-8")

        if not prompt_content:
            prompt_content = self._default_judge_system_prompt()

        pipeline_name = state.get("pipeline_name", "")
        if pipeline_name:
            prompt_content += f"\n\n当前 Pipeline: {pipeline_name}"

        return prompt_content

    def _default_judge_system_prompt(self) -> str:
        return (
            "你是一个 pipeline 中断判断器。根据用户新消息和当前 pipeline 状态，"
            "判断应该继续执行、补充信息还是中断回滚。\n"
            "输出严格的 JSON 格式，不要包含其他文字。"
        )

    def _build_judge_user_prompt(self, user_message: str | PipelineUserInput, state: dict) -> str:
        """Build the user prompt with full pipeline context."""
        pipeline_input = normalize_pipeline_user_input(user_message)
        sections = []

        # Pipeline structure
        steps = state.get("steps", [])
        if steps:
            step_lines = []
            for s in steps:
                marker = " [当前]" if s.get("is_current") else ""
                step_lines.append(f"  - {s['step_id']}: {s.get('description', '')}{marker}")
            sections.append("=== Pipeline 步骤 ===\n" + "\n".join(step_lines))

        # Completed conclusions
        conclusions = state.get("conclusions", {})
        if conclusions:
            truncated = {}
            for k, v in conclusions.items():
                text = json.dumps(v, ensure_ascii=False)
                truncated[k] = _safe_truncate(text, max_chars=500)
            sections.append("=== 已完成结论 ===\n" + json.dumps(truncated, ensure_ascii=False, indent=2))

        # Current step partial output
        partial = state.get("partial_output", "")
        if partial:
            sections.append(f"=== 当前步骤输出（最近内容）===\n{_safe_truncate(partial, max_chars=500, from_end=True)}")

        # Candidate states (parallel sub-pipeline)
        candidate_states = state.get("candidate_states", [])
        if candidate_states:
            lines = []
            for cs in candidate_states:
                lines.append(f"  - 候选方案{cs['index']} ({cs.get('name', '')}): {cs['current_sub_step']} [进行中]")
            sections.append("=== 各 Candidate 当前状态 ===\n" + "\n".join(lines))

        # Sub-pipeline steps (if applicable)
        sub_steps = state.get("sub_pipeline_steps", [])
        if sub_steps:
            lines = [f"  - {s['step_id']}: {s.get('description', '')}" for s in sub_steps]
            sections.append("=== Sub-pipeline 可回滚步骤 ===\n" + "\n".join(lines))

        # User message
        user_text = pipeline_input.display_text
        if pipeline_input.has_images:
            user_text = user_text if user_text.strip() else IMAGE_INPUT_PLACEHOLDER
            user_text += (
                "\n\n用户同时提供了图片输入。请检查图片内容，并在 reason 或 rollback_context "
                "中写出与路由相关的图像信息。"
            )
        sections.append(f"=== 用户新消息 ===\n{user_text}")

        # Available actions
        sections.append(
            "=== 可选动作 ===\n"
            "- continue: 消息与当前步骤无关，继续执行\n"
            "- supplement: 对当前步骤的补充信息，注入到当前对话\n"
            "- hard_interrupt: 用户方向已改变，需要中断并回滚\n\n"
            "输出 JSON 格式:\n"
            '{"action": "...", "reason": "...", "rollback_target": "...|null", '
            '"candidate_scope": "candidate:N|all|null", "supplement_target": "candidate:N|all|null"}'
        )

        return "\n\n".join(sections)

    def _parse_verdict(self, text: str) -> InterruptVerdict | None:
        """Parse LLM response into InterruptVerdict. Returns None if retry needed."""
        text = text.strip()
        text = re.sub(r"^```\w*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse judge response as JSON. raw=%r", _safe_truncate(text, max_chars=500))
            return InterruptVerdict(
                action="continue",
                reason=f"parse failed: not JSON. raw={text[:120]!r}",
            )

        action = data.get("action", "continue")
        if action not in ("continue", "supplement", "hard_interrupt"):
            logger.warning("Judge LLM returned invalid action %r, raw=%r", action, _safe_truncate(text, max_chars=500))
            return InterruptVerdict(
                action="continue",
                reason=f"parse failed: invalid action {action!r}",
            )

        # Coerce LLM-hallucinated 'null' strings to real None for ALL nullable
        # string fields (P-C2). Without this, rollback_target='null' crashes
        # state_machine.interrupt_rollback, and rollback_context='null' passes
        # the missing-context retry check below and gets injected literally.
        rollback_target = _coerce_null(data.get("rollback_target"))
        rollback_context = _coerce_null(data.get("rollback_context"))
        candidate_scope = _coerce_null(data.get("candidate_scope"))
        supplement_target = _coerce_null(data.get("supplement_target"))

        if action == "hard_interrupt" and not rollback_context:
            logger.warning("hard_interrupt missing rollback_context, will retry")
            return None

        return InterruptVerdict(
            action=action,
            reason=data.get("reason", ""),
            rollback_target=rollback_target,
            candidate_scope=candidate_scope,
            supplement_target=supplement_target,
            rollback_context=rollback_context,
        )
