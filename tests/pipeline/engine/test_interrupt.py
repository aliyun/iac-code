"""Tests for InterruptController."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestInterruptVerdict:
    def test_verdict_creation(self):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        v = InterruptVerdict(
            action="hard_interrupt",
            reason="user wants cheaper",
            rollback_target="intent_parsing",
        )
        assert v.action == "hard_interrupt"
        assert v.reason == "user wants cheaper"
        assert v.rollback_target == "intent_parsing"
        assert v.candidate_scope is None
        assert v.supplement_target is None

    def test_verdict_with_candidate_scope(self):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        v = InterruptVerdict(
            action="hard_interrupt",
            reason="fix template",
            rollback_target="template_generating",
            candidate_scope="candidate:0",
        )
        assert v.candidate_scope == "candidate:0"


class TestInterruptController:
    def test_init(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        controller = InterruptController(pm, lambda: {})
        assert controller is not None

    @pytest.mark.asyncio
    async def test_judge_timeout_returns_continue(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()

        async def slow_complete(*args, **kwargs):
            await asyncio.sleep(100)

        pm.complete = slow_complete

        controller = InterruptController(pm, lambda: {"steps": [], "conclusions": {}})

        # Patch JUDGE_TIMEOUT_SECONDS to 0.1 for fast test
        import iac_code.pipeline.engine.interrupt as interrupt_module

        original_timeout = interrupt_module.JUDGE_TIMEOUT_SECONDS
        interrupt_module.JUDGE_TIMEOUT_SECONDS = 0.1
        try:
            verdict = await controller.judge("new message")
            assert verdict.action == "continue"
            assert "timeout" in verdict.reason
        finally:
            interrupt_module.JUDGE_TIMEOUT_SECONDS = original_timeout

    @pytest.mark.asyncio
    async def test_judge_parses_valid_response(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        response_json = json.dumps(
            {
                "action": "hard_interrupt",
                "reason": "用户改变了需求",
                "rollback_target": "intent_parsing",
                "candidate_scope": None,
                "supplement_target": None,
            }
        )
        mock_response = MagicMock()
        mock_response.text = response_json
        pm.complete = AsyncMock(return_value=mock_response)

        state = {
            "pipeline_name": "selling",
            "current_step_id": "architecture_planning",
            "steps": [
                {"step_id": "intent_parsing", "description": "解析意图", "is_current": False},
                {"step_id": "architecture_planning", "description": "设计架构", "is_current": True},
            ],
            "conclusions": {"intent": {"summary": "deploy nginx"}},
            "partial_output": "正在设计方案...",
        }

        controller = InterruptController(pm, lambda: state)
        verdict = await controller.judge("我不要nginx了，换成redis")
        assert verdict.action == "hard_interrupt"
        assert verdict.rollback_target == "intent_parsing"

    @pytest.mark.asyncio
    async def test_judge_invalid_json_returns_continue(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "this is not json"
        pm.complete = AsyncMock(return_value=mock_response)

        controller = InterruptController(pm, lambda: {"steps": [], "conclusions": {}})
        verdict = await controller.judge("hello")
        assert verdict.action == "continue"

    @pytest.mark.asyncio
    async def test_judge_strips_markdown_fences(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '```json\n{"action": "supplement", "reason": "extra info"}\n```'
        pm.complete = AsyncMock(return_value=mock_response)

        controller = InterruptController(pm, lambda: {"steps": [], "conclusions": {}})
        verdict = await controller.judge("补充一下")
        assert verdict.action == "supplement"

    @pytest.mark.asyncio
    async def test_judge_strips_multiline_fenced_json(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        multiline_json = (
            "```json\n"
            "{\n"
            '  "action": "hard_interrupt",\n'
            '  "reason": "user changed requirements",\n'
            '  "rollback_target": "intent_parsing",\n'
            '  "candidate_scope": null,\n'
            '  "supplement_target": null\n'
            "}\n"
            "```"
        )
        mock_response = MagicMock()
        mock_response.text = multiline_json
        pm.complete = AsyncMock(return_value=mock_response)

        controller = InterruptController(pm, lambda: {"steps": [], "conclusions": {}})
        verdict = await controller.judge("我想换个方案")
        assert verdict.action == "hard_interrupt"
        assert verdict.rollback_target == "intent_parsing"
        assert verdict.reason == "user changed requirements"

    @pytest.mark.asyncio
    async def test_judge_strips_fences_without_language_tag(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '```\n{"action": "supplement", "reason": "more info"}\n```'
        pm.complete = AsyncMock(return_value=mock_response)

        controller = InterruptController(pm, lambda: {"steps": [], "conclusions": {}})
        verdict = await controller.judge("补充")
        assert verdict.action == "supplement"

    @pytest.mark.asyncio
    async def test_judge_exception_returns_continue(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        pm.complete = AsyncMock(side_effect=RuntimeError("LLM error"))

        controller = InterruptController(pm, lambda: {"steps": [], "conclusions": {}})
        verdict = await controller.judge("test")
        assert verdict.action == "continue"
        assert "failed" in verdict.reason


class TestNullStringNormalization:
    def test_candidate_scope_null_string_normalized(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        controller = InterruptController(MagicMock(), lambda: {})
        raw = json.dumps(
            {
                "action": "supplement",
                "reason": "extra info",
                "candidate_scope": "null",
                "supplement_target": "null",
            }
        )
        verdict = controller._parse_verdict(raw)
        assert verdict.candidate_scope is None
        assert verdict.supplement_target is None

    def test_candidate_scope_real_null_stays_none(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        controller = InterruptController(MagicMock(), lambda: {})
        raw = json.dumps(
            {
                "action": "supplement",
                "reason": "extra info",
                "candidate_scope": None,
                "supplement_target": None,
            }
        )
        verdict = controller._parse_verdict(raw)
        assert verdict.candidate_scope is None
        assert verdict.supplement_target is None

    def test_candidate_scope_valid_value_preserved(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        controller = InterruptController(MagicMock(), lambda: {})
        raw = json.dumps(
            {
                "action": "hard_interrupt",
                "reason": "fix",
                "rollback_target": "t",
                "candidate_scope": "all",
                "rollback_context": "context",
            }
        )
        verdict = controller._parse_verdict(raw)
        assert verdict.candidate_scope == "all"


class TestRollbackContext:
    def test_verdict_with_rollback_context(self):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        v = InterruptVerdict(
            action="hard_interrupt",
            reason="user wants wordpress",
            rollback_target="intent_parsing",
            rollback_context="用户要求将业务类型改为 WordPress 网站，请根据此需求重新解析意图。",
        )
        assert v.rollback_context == "用户要求将业务类型改为 WordPress 网站，请根据此需求重新解析意图。"

    def test_parse_verdict_extracts_rollback_context(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        controller = InterruptController(MagicMock(), lambda: {})
        raw = json.dumps(
            {
                "action": "hard_interrupt",
                "reason": "changed",
                "rollback_target": "intent_parsing",
                "rollback_context": "用户想改成WordPress",
            }
        )
        verdict = controller._parse_verdict(raw)
        assert verdict.rollback_context == "用户想改成WordPress"

    def test_parse_verdict_hard_interrupt_missing_context_returns_needs_retry(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        controller = InterruptController(MagicMock(), lambda: {})
        raw = json.dumps(
            {
                "action": "hard_interrupt",
                "reason": "changed",
                "rollback_target": "intent_parsing",
            }
        )
        verdict = controller._parse_verdict(raw)
        assert verdict is None

    @pytest.mark.asyncio
    async def test_judge_retries_once_on_missing_rollback_context(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        first_response = MagicMock()
        first_response.text = json.dumps(
            {
                "action": "hard_interrupt",
                "reason": "changed",
                "rollback_target": "intent_parsing",
            }
        )
        second_response = MagicMock()
        second_response.text = json.dumps(
            {
                "action": "hard_interrupt",
                "reason": "changed",
                "rollback_target": "intent_parsing",
                "rollback_context": "用户要WordPress",
            }
        )
        pm.complete = AsyncMock(side_effect=[first_response, second_response])

        controller = InterruptController(pm, lambda: {"steps": [], "conclusions": {}})
        verdict = await controller.judge("改成wordpress")
        assert verdict.action == "hard_interrupt"
        assert verdict.rollback_context == "用户要WordPress"
        assert pm.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_judge_gives_up_after_one_retry(self):
        from iac_code.pipeline.engine.interrupt import InterruptController

        pm = MagicMock()
        bad_response = MagicMock()
        bad_response.text = json.dumps(
            {
                "action": "hard_interrupt",
                "reason": "changed",
                "rollback_target": "intent_parsing",
            }
        )
        pm.complete = AsyncMock(return_value=bad_response)

        controller = InterruptController(pm, lambda: {"steps": [], "conclusions": {}})
        verdict = await controller.judge("改成wordpress")
        assert verdict.action == "hard_interrupt"
        assert verdict.rollback_context is None
        assert pm.complete.call_count == 2


class TestNullStringNormalizationExtended:
    """Regression: 'null' / 'NULL' / 'Null' strings must coerce to None for
    rollback_target and rollback_context too (P-C2)."""

    @pytest.mark.parametrize(
        "raw_text,field,expected",
        [
            # rollback_target
            (
                '{"action": "hard_interrupt", "reason": "r", "rollback_target": "null", "rollback_context": "ok"}',
                "rollback_target",
                None,
            ),
            (
                '{"action": "hard_interrupt", "reason": "r", "rollback_target": "NULL", "rollback_context": "ok"}',
                "rollback_target",
                None,
            ),
            (
                '{"action": "hard_interrupt", "reason": "r", "rollback_target": "Null", "rollback_context": "ok"}',
                "rollback_target",
                None,
            ),
            (
                '{"action": "hard_interrupt", "reason": "r", '
                '"rollback_target": "intent_parsing", "rollback_context": "ok"}',
                "rollback_target",
                "intent_parsing",
            ),
            # rollback_context
            (
                '{"action": "hard_interrupt", "reason": "r", "rollback_target": "intent", "rollback_context": "null"}',
                "rollback_context",
                None,
            ),
            (
                '{"action": "hard_interrupt", "reason": "r", "rollback_target": "intent", "rollback_context": "NULL"}',
                "rollback_context",
                None,
            ),
            (
                '{"action": "hard_interrupt", "reason": "r", '
                '"rollback_target": "intent", "rollback_context": "real reason"}',
                "rollback_context",
                "real reason",
            ),
        ],
    )
    def test_null_string_normalized_for_all_fields(self, raw_text, field, expected):
        from iac_code.pipeline.engine.interrupt import InterruptController

        controller = InterruptController(MagicMock(), lambda: {})
        verdict = controller._parse_verdict(raw_text)
        # rollback_context="null" should also trigger the missing-context retry
        # (return None) — but we test the field after a "real" reason path here.
        # When verdict is None, this test row exercises the retry contract instead.
        if verdict is None:
            # rollback_context was treated as missing → caller will retry.
            # Acceptable behavior for "null" rollback_context.
            assert field == "rollback_context"
            assert expected is None
            return
        assert getattr(verdict, field) == expected


class TestFallbackPathNormalization:
    """Regression: _call_judge_llm fallback path must use _parse_verdict (P-I7)."""

    @pytest.mark.asyncio
    async def test_fallback_normalizes_null_strings(self):
        """When judge returns a hard_interrupt missing rollback_context twice,
        fallback fires. Fallback must still normalize 'null' strings."""
        from iac_code.pipeline.engine.interrupt import InterruptController

        # Both attempts: missing rollback_context (triggers fallback after retry)
        # AND candidate_scope is the string "null" (must be coerced to None)
        bad_response_text = (
            '{"action": "hard_interrupt", "reason": "user changed mind", '
            '"rollback_target": "intent_parsing", "candidate_scope": "null", '
            '"supplement_target": "null"}'
        )

        pm = MagicMock()
        response = MagicMock()
        response.text = bad_response_text
        pm.complete = AsyncMock(return_value=response)

        controller = InterruptController(pm, lambda: {})
        verdict = await controller._call_judge_llm("user msg")

        # Fallback emitted a verdict (no longer None after retry exhausted)
        assert verdict.action == "hard_interrupt"
        assert verdict.rollback_target == "intent_parsing"
        # CRITICAL: 'null' strings must be coerced even on the fallback path
        assert verdict.candidate_scope is None
        assert verdict.supplement_target is None


class TestPromptFormatConsistency:
    """P-I5: judge prompt + InterruptController inline format must use
    'candidate:N' for both candidate_scope and supplement_target."""

    def test_inlined_prompt_uses_unified_candidate_format(self):
        import pathlib

        src = pathlib.Path("src/iac_code/pipeline/engine/interrupt.py").read_text(encoding="utf-8")
        # supplement_target must use new format in the inline format spec.
        assert '"supplement_target": "candidate:N' in src, (
            "supplement_target should use unified 'candidate:N' format in interrupt.py prompt text"
        )

    def test_interrupt_judge_md_does_not_use_legacy_format_as_canonical(self):
        import pathlib

        prompt = pathlib.Path("src/iac_code/pipeline/engine/prompts/interrupt_judge.md").read_text(encoding="utf-8")
        # New format must be present.
        assert "candidate:" in prompt
        # Legacy format, if present, must be near a "legacy" or "deprecated" qualifier.
        if "candidate_index:" in prompt:
            pos = 0
            while pos < len(prompt):
                hit = prompt.find("candidate_index:", pos)
                if hit == -1:
                    break
                window = prompt[max(0, hit - 200) : hit + 200].lower()
                assert "legacy" in window or "deprecated" in window, (
                    f"candidate_index: appears as canonical (not flagged legacy) near pos {hit}"
                )
                pos = hit + 1


class TestJudgePromptParentChildRule:
    """P-I8: judge prompt must explicitly state that parent-level rollback_target
    requires candidate_scope=null. Reduces escalation frequency."""

    def test_prompt_has_parent_rollback_scope_rule(self):
        """The prompt must contain a clear directive that ties parent-level
        rollback_target to candidate_scope=null."""
        import pathlib

        prompt = pathlib.Path("src/iac_code/pipeline/engine/prompts/interrupt_judge.md").read_text(encoding="utf-8")
        # Must mention candidate_scope and have parent + null + must directive.
        assert "candidate_scope" in prompt
        body = prompt.lower()
        # Accept English ("parent" + "must" + "null") OR Chinese ("父级" + "必须" + "null").
        has_directive_en = "parent" in body and "must" in body and "null" in body
        has_directive_cn = "父级" in prompt and "必须" in prompt and "null" in body
        assert has_directive_en or has_directive_cn, (
            "prompt does not contain a clear parent-rollback → candidate_scope=null directive"
        )

    def test_engine_escalation_still_normalizes_inconsistent_verdict(self):
        """Defense in depth: even if LLM violates the new rule, engine's
        existing escalation in apply_hard_interrupt must still treat parent-level
        rollback_target as parent rollback (returning True), regardless of
        the bogus candidate_scope."""
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.interrupt import InterruptVerdict
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner._loaded = MagicMock()
        runner._loaded.sub_pipelines = {}  # parent step not in any sub-pipeline
        # mock parent step in main steps list
        intent_step = MagicMock(step_id="intent_parsing", conclusion_field=None)
        runner._loaded.steps = [intent_step]
        runner.state_machine = MagicMock()
        runner.state_machine.current_step = MagicMock(sub_pipeline_name=None)
        # apply_hard_interrupt's helpers
        runner._cancel_active_candidates = MagicMock(return_value=[])
        runner._pending_candidate_restarts = MagicMock()
        runner.context = MagicMock()
        runner._rollback_context = None
        runner._execution = {}
        runner._attempts = {}
        runner._save_rollback_sync = MagicMock()
        assert not hasattr(runner, "_observability")

        # LLM violates the rule: parent rollback target + non-null candidate_scope.
        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="user wants cheaper",
            rollback_target="intent_parsing",  # parent step
            candidate_scope="candidate:0",  # invalid combo
            rollback_context="cheap please",
        )

        # Engine escalation must still treat as parent rollback (True).
        result = runner.apply_hard_interrupt(verdict)
        assert result is True, (
            "engine escalation (defense in depth) must still normalize bogus "
            "candidate_scope to parent rollback when target is parent-level — "
            "P-I8 prompt clarification is preventive, not a replacement for the bottom-line."
        )


class TestSafeTruncate:
    """P-I15: _safe_truncate preserves CJK codepoints and supports head/tail modes."""

    @pytest.mark.parametrize(
        "text,max_chars,from_end,expected",
        [
            # CJK head truncate: first 10 codepoints + "..."
            ("中文测试" * 200, 10, False, "中文测试中文测试中文..."),
            # CJK tail truncate: last 10 codepoints of "...中文测试中文测试", no suffix
            ("中文测试" * 200, 10, True, "测试中文测试中文测试"),
            # Short text: no truncation
            ("abc", 100, False, "abc"),
            ("", 100, False, ""),
            # Mixed ASCII + CJK head truncate
            ("a中b文c测", 4, False, "a中b文..."),
        ],
    )
    def test_safe_truncate_preserves_codepoints(self, text, max_chars, from_end, expected):
        from iac_code.pipeline.engine.interrupt import _safe_truncate

        result = _safe_truncate(text, max_chars=max_chars, from_end=from_end)
        assert result == expected
        # Byte-level guarantee: result encodes cleanly to UTF-8 (no half codepoints).
        result.encode("utf-8")


class TestAmbiguousVerdictPrompt:
    """P-I18: judge prompt must instruct LLM to emit [ambiguous] marker in reason."""

    def test_prompt_documents_ambiguous_marker(self):
        import pathlib

        prompt = pathlib.Path("src/iac_code/pipeline/engine/prompts/interrupt_judge.md").read_text(encoding="utf-8")
        assert "[ambiguous]" in prompt, "prompt must instruct LLM to use [ambiguous] prefix in reason"
        assert "continue" in prompt.lower()
