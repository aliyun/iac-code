import json
from types import SimpleNamespace

import pytest

from iac_code.agent.message import (
    COMPACTION_SUMMARY_TAIL_METADATA_KEY,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    compaction_summary_tail_count,
    create_compaction_summary_message,
    is_compaction_summary_message,
)
from iac_code.pipeline.engine.cleanup import (
    CLEANUP_PROMPT_METADATA_TYPE,
    create_cleanup_prompt_message,
    is_cleanup_prompt_message,
)
from iac_code.services.context_manager import (
    _SUMMARY_BLOCK_TEXT_LIMIT,
    ContextManager,
    get_context_window_config,
)


class TestContextWindowConfig:
    def test_claude_model(self):
        config = get_context_window_config("claude-3-opus")
        assert config.context_window == 200_000

    def test_qwen_model(self):
        config = get_context_window_config("qwen3.6-plus")
        assert config.context_window == 1_000_000

    def test_gpt4_model(self):
        config = get_context_window_config("gpt-4-turbo")
        assert config.context_window == 128_000

    def test_dashscope_kimi_k3_uses_documented_context_window(self):
        config = get_context_window_config("kimi/kimi-k3")
        assert config.context_window == 1_000_000
        assert config.max_output_tokens == 8_192

    @pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna"])
    def test_gpt56_models_use_documented_capacity(self, model):
        config = get_context_window_config(model)
        assert config.context_window == 1_050_000
        assert config.max_output_tokens == 128_000

    def test_claude_fable5_uses_documented_capacity(self):
        config = get_context_window_config("claude-fable-5")
        assert config.context_window == 1_000_000
        assert config.max_output_tokens == 128_000

    def test_claude_sonnet5_uses_documented_capacity(self):
        config = get_context_window_config("claude-sonnet-5")
        assert config.context_window == 1_000_000
        assert config.max_output_tokens == 128_000

    @pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-4-8", "gpt-5.5", "gpt-5.4"])
    def test_new_frontier_models_use_documented_long_context_capacity(self, model):
        config = get_context_window_config(model)
        assert config.context_window in {1_000_000, 1_050_000}
        assert config.max_output_tokens == 128_000

    @pytest.mark.parametrize("model", ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.3-codex", "gpt-5.2"])
    def test_openai_400k_models_use_documented_capacity(self, model):
        config = get_context_window_config(model)
        assert config.context_window == 400_000
        assert config.max_output_tokens == 128_000

    @pytest.mark.parametrize(
        "model",
        [
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.1-pro-preview",
            "gemini-3.1-pro-preview-customtools",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
    )
    def test_gemini_models_use_documented_capacity(self, model):
        config = get_context_window_config(model)
        assert config.context_window == 1_048_576
        assert config.max_output_tokens == 65_536

    @pytest.mark.parametrize("model", ["kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5"])
    def test_kimi_k2_models_use_documented_context_capacity(self, model):
        assert get_context_window_config(model).context_window == 262_144

    @pytest.mark.parametrize(
        ("model", "context_window"),
        [
            ("qwen3.8-max", 1_000_000),
            ("qwen3.8-max-prime", 1_000_000),
            ("qwen3.8-flash", 1_000_000),
            ("qwen3.8-2.4t-a95b", 1_000_000),
            ("qwen3.8-27b", 1_000_000),
            ("qwen3.8-max-preview", 1_000_000),
            ("qwen3.7-max", 1_000_000),
            ("qwen3.7-plus", 1_000_000),
            ("qwen3.7-flash", 1_000_000),
            ("qwen3.6-flash", 1_000_000),
            ("qwen3.6-35b-a3b", 262_144),
            ("qwen3.6-27b", 262_144),
            ("deepseek-v4-pro", 1_000_000),
            ("deepseek-v4-pro-0813", 1_000_000),
            ("deepseek-v4-flash-0731", 1_000_000),
            ("deepseek-v4-flash", 1_000_000),
            ("glm-5.1", 202_752),
            ("MiniMax-M3", 1_000_000),
            ("MiniMax/MiniMax-M3", 196_608),
            ("xiaomi/mimo-v2.5-pro", 1_048_576),
            ("stepfun/step-3.7-flash", 262_144),
        ],
    )
    def test_current_dashscope_models_use_documented_context_capacity(self, model, context_window):
        assert get_context_window_config(model).context_window == context_window

    @pytest.mark.parametrize(
        "model", ["deepseek-v4-pro", "deepseek-v4-pro-0813", "deepseek-v4-flash", "deepseek-v4-flash-0731"]
    )
    def test_deepseek_v4_models_use_documented_output_capacity(self, model):
        assert get_context_window_config(model).max_output_tokens == 393_216

    def test_direct_glm52_uses_documented_context_and_output_capacity(self):
        config = get_context_window_config("glm-5.2")
        assert config.context_window == 1_000_000
        assert config.max_output_tokens == 128_000

    def test_glm53_uses_documented_context_and_output_capacity(self):
        config = get_context_window_config("glm-5.3")
        assert config.context_window == 1_000_000
        assert config.max_output_tokens == 128_000

    def test_glm53_flash_uses_documented_context_and_output_capacity(self):
        config = get_context_window_config("glm-5.3-flash")
        assert config.context_window == 1_000_000
        assert config.max_output_tokens == 128_000

    def test_dashscope_zhipu_glm53_uses_hosted_capacity(self):
        config = get_context_window_config("ZHIPU/GLM-5.3")
        assert config.context_window == 1_048_576
        assert config.max_output_tokens == 131_072

    def test_dashscope_glm52_fast_preview_uses_documented_capacity(self):
        config = get_context_window_config("glm-5.2-fast-preview")
        assert config.context_window == 1_048_576
        assert config.max_output_tokens == 131_072

    def test_shared_kimi_k3_id_keeps_conservative_capacity(self):
        config = get_context_window_config("kimi-k3")
        assert config.context_window == 1_000_000
        assert config.max_output_tokens == 8_192

    @pytest.mark.parametrize(
        ("model", "max_output_tokens"),
        [
            ("qwen3.8-2.4t-a95b", 131_072),
            ("qwen3.8-27b", 131_072),
            ("qwen3.6-35b-a3b", 65_536),
            ("qwen3.6-27b", 65_536),
            ("xiaomi/mimo-v2.5-pro", 131_072),
            ("stepfun/step-3.7-flash", 262_144),
        ],
    )
    def test_new_bailian_models_use_documented_output_capacity(self, model, max_output_tokens):
        assert get_context_window_config(model).max_output_tokens == max_output_tokens

    def test_unknown_model_uses_default(self):
        config = get_context_window_config("unknown-model-xyz")
        assert config.context_window == 128_000

    def test_preserve_recent_turns(self):
        config = get_context_window_config("claude-3-opus")
        assert config.preserve_recent_turns == 3


class TestContextManager:
    def test_add_user_message(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        msg = cm.add_user_message("Hello")
        assert msg.role == "user"
        assert msg.token_count > 0

    def test_add_assistant_message(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        cm.add_user_message("Hello")
        msg = cm.add_assistant_message([TextBlock(text="Hi there")])
        assert msg.role == "assistant"

    def test_add_tool_results(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        cm.add_user_message("Hello")
        blocks = [ToolResultBlock(tool_use_id="t1", content="result")]
        msg = cm.add_tool_results(blocks)
        assert msg.role == "user"

    def test_get_total_tokens_includes_system_prompt(self):
        cm = ContextManager(system_prompt="A long system prompt " * 100, model="qwen")
        total = cm.get_total_tokens()
        assert total > 100

    def test_needs_compaction_false_when_small(self):
        cm = ContextManager(system_prompt="Short.", model="qwen")
        cm.add_user_message("Hello")
        assert cm.needs_compaction() is False

    def test_sonnet5_compaction_uses_one_million_token_threshold(self, monkeypatch):
        cm = ContextManager(system_prompt="Short.", model="claude-sonnet-5")
        for index in range(4):
            cm.add_user_message(f"message {index}")
            cm.add_assistant_message(f"response {index}")
        monkeypatch.setattr(cm, "get_total_tokens", lambda: 900_000)
        assert cm.needs_compaction() is False

        monkeypatch.setattr(cm, "get_total_tokens", lambda: 940_000)
        assert cm.needs_compaction() is True

    def _tiny_window(self, cm: ContextManager) -> None:
        cm._config = cm._config.__class__(
            context_window=50,
            max_output_tokens=8192,
            compact_buffer=10,
            compact_threshold=0.5,
            preserve_recent_turns=3,
        )

    def test_needs_compaction_false_when_only_recent_tail_over_threshold(self):
        # 单条超大工具结果使 token 全落在保留尾部：超阈值但没有可压缩的旧消息，压缩是空操作。
        # 若此时仍判定需要压缩，会每回合空转触发、不留会话记录（见问题 #2 无记录 / #3 反复触发）。
        cm = ContextManager(system_prompt="", model="qwen")
        self._tiny_window(cm)
        cm.add_user_message("read the giant file")
        cm.add_assistant_message("ok")
        cm.add_tool_results([ToolResultBlock(tool_use_id="t1", content="lorem ipsum " * 200)])
        old, _recent = cm._split_messages_for_compaction()
        assert cm.get_total_tokens() > 25  # 超过阈值（window * 0.5）
        assert old == []  # 没有可压缩的旧消息——权重全在保留尾部
        assert cm.needs_compaction() is False

    def test_needs_compaction_true_when_old_messages_exist(self):
        # 存在可压缩的旧消息时仍应正常触发压缩。
        cm = ContextManager(system_prompt="", model="qwen")
        self._tiny_window(cm)
        for i in range(20):
            cm.add_user_message(f"message {i} with enough content to use tokens")
        old, _recent = cm._split_messages_for_compaction()
        assert old  # 存在可压缩的旧消息
        assert cm.needs_compaction() is True

    def test_get_usage_returns_breakdown(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        cm.add_user_message("Hello")
        usage = cm.get_usage()
        assert "system_prompt_tokens" in usage
        assert "tool_definition_tokens" in usage
        assert "user_message_tokens" in usage
        assert "assistant_message_tokens" in usage
        assert "tool_result_tokens" in usage
        assert "total_tokens" in usage
        assert "context_window" in usage
        assert "usage_percent" in usage

    def test_tool_definitions_count_toward_total_and_usage(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        base_total = cm.get_total_tokens()
        tool = SimpleNamespace(
            name="create_stack",
            description="Create a ROS stack",
            input_schema={
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "Template body"},
                },
            },
        )

        cm.set_tool_definitions([tool])

        usage = cm.get_usage()
        assert usage["tool_definition_tokens"] > 0
        assert cm.get_total_tokens() == base_total + usage["tool_definition_tokens"]
        assert usage["total_tokens"] == cm.get_total_tokens()
        assert usage["usage_percent"] > 0

    def test_set_tool_definitions_copies_input_list(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        tool = SimpleNamespace(name="read_file", description="Read file", input_schema={"type": "object"})
        tools = [tool]

        cm.set_tool_definitions(tools)
        before = cm.get_usage()["tool_definition_tokens"]
        tools.append(
            SimpleNamespace(
                name="write_file",
                description="Write a much larger file",
                input_schema={"type": "object", "properties": {"content": {"type": "string"}}},
            )
        )

        assert cm.get_usage()["tool_definition_tokens"] == before


class TestSegmentedCompaction:
    def test_build_compaction_prompt_excludes_recent(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Assistant response {i}")])

        prompt = cm.build_compaction_prompt()
        assert "User message 0" in prompt
        assert "User message 5" not in prompt

    def test_build_compaction_prompt_includes_tool_activity(self):
        # 问题 #2：流水线步骤的真实内容全在 tool_use/tool_result 里；若只取 TextBlock
        # 文本，摘要模型只能看到启动指令 → 退化摘要「无历史可总结」。摘要 prompt 必须纳入
        # 工具名与（截断后的）工具结果，反映实际做了什么。
        cm = ContextManager(system_prompt="", model="qwen")
        cm.add_user_message("请完成当前步骤：template_generating。")
        cm.add_assistant_message([ToolUseBlock(id="t1", name="read_file", input={"path": "main.tf"})])
        cm.add_tool_results([ToolResultBlock(tool_use_id="t1", content="RESOURCE_MARKER resource ecs {}")])
        cm.add_assistant_message([ToolUseBlock(id="t2", name="write_file", input={"content": "TEMPLATE_MARKER"})])
        cm.add_tool_results([ToolResultBlock(tool_use_id="t2", content="ok")])
        for i in range(3):
            cm.add_user_message(f"recent-user-{i}")
            cm.add_assistant_message([TextBlock(text=f"recent-assistant-{i}")])

        prompt = cm.build_compaction_prompt()

        assert prompt  # 不再退化为空
        assert "请完成当前步骤：template_generating。" in prompt
        assert "read_file" in prompt
        assert "RESOURCE_MARKER" in prompt
        assert "write_file" in prompt
        assert "TEMPLATE_MARKER" in prompt
        # 保留尾部（recent）不进摘要 prompt。
        assert "recent-user-0" not in prompt

    def test_render_message_for_summary_truncates_large_tool_result(self):
        cm = ContextManager(system_prompt="", model="qwen")
        big = "X" * (_SUMMARY_BLOCK_TEXT_LIMIT + 500)
        msg = Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content=big)])
        rendered = cm._render_message_for_summary(msg)
        assert rendered.endswith("…")
        assert len(rendered) <= _SUMMARY_BLOCK_TEXT_LIMIT + len("[工具结果] ") + 1

    @pytest.mark.parametrize("diagnostics", ["", "\nDelegated diagnostics: preflight passed"])
    def test_aliyun_body_only_avoids_envelope_induced_summary_truncation(self, diagnostics):
        marker = "BUSINESS_TAIL_MARKER"
        empty_body = json.dumps({"payload": "", "tail": marker}, ensure_ascii=False, indent=2)
        payload_size = _SUMMARY_BLOCK_TEXT_LIMIT - len(empty_body) - len(diagnostics)
        body = json.dumps({"payload": "X" * payload_size, "tail": marker}, ensure_ascii=False, indent=2)
        new_content = body + diagnostics
        old_content = (
            json.dumps(
                {
                    "status": 200,
                    "headers": {"requestid": "req-1"},
                    "body": {"payload": "X" * payload_size, "tail": marker},
                    "content_type": "application/json",
                    "content_encoding": None,
                    "size": len(body),
                },
                ensure_ascii=False,
                indent=2,
            )
            + diagnostics
        )
        cm = ContextManager(system_prompt="", model="qwen")

        new_rendered = cm._render_message_for_summary(
            Message(role="user", content=[ToolResultBlock(tool_use_id="new", content=new_content)])
        )
        old_rendered = cm._render_message_for_summary(
            Message(role="user", content=[ToolResultBlock(tool_use_id="old", content=old_content)])
        )

        assert len(new_content.strip()) <= _SUMMARY_BLOCK_TEXT_LIMIT < len(old_content.strip())
        assert marker in new_rendered
        assert not new_rendered.endswith("…")
        assert marker not in old_rendered
        assert old_rendered.endswith("…")

    def test_build_compaction_prompt_excludes_recalled_memory_messages(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_recalled_memory_message("# Recalled Memory\nhidden memory body", ["hidden-topic.md"])
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Assistant response {i}")])

        prompt = cm.build_compaction_prompt()

        assert "User message 0" in prompt
        assert "hidden memory body" not in prompt
        assert "hidden-topic.md" not in prompt

    def test_build_compaction_prompt_excludes_cleanup_prompt_messages(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_raw_message(
            {
                "role": "user",
                "content": "cleanup hidden prompt",
                "metadata": {"type": CLEANUP_PROMPT_METADATA_TYPE, "source": "pipeline_cleanup"},
            }
        )
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Assistant response {i}")])

        prompt = cm.build_compaction_prompt()

        assert "User message 0" in prompt
        assert "cleanup hidden prompt" not in prompt

    def test_apply_compaction_preserves_cleanup_prompt_messages(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cleanup_message = create_cleanup_prompt_message("cleanup hidden prompt")
        cm.add_raw_message(cleanup_message.to_dict())
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Assistant response {i}")])

        cm.apply_compaction("summary")

        messages = cm.get_messages()
        assert any(
            is_cleanup_prompt_message(message) and message.content == "cleanup hidden prompt" for message in messages
        )
        # 新语义：完整历史保留，压缩标记进入有效切片而非 get_messages()[0]。
        assert cm.get_context_messages()[0].content == "[Conversation Summary]\nsummary"

    def test_remove_cleanup_prompt_messages_removes_hidden_prompts(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("real prompt")
        cm.add_raw_message(create_cleanup_prompt_message("cleanup hidden prompt").to_dict())

        removed = cm.remove_cleanup_prompt_messages()

        assert removed == 1
        assert [message.content for message in cm.get_messages()] == ["real prompt"]

    def test_apply_compaction_preserves_recent(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Response {i}")])

        original_count = len(cm.get_messages())
        assert original_count == 12

        cm.apply_compaction("Summary of old conversation")
        # 新语义：完整历史保留（get_messages 不再收缩），有效切片=标记+尾部。
        messages = cm.get_context_messages()
        assert len(messages) == 7
        assert "Summary" in messages[0].get_text()
        assert "User message 3" in messages[1].get_text()

    def test_apply_compaction_returns_token_counts(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        for i in range(6):
            cm.add_user_message(f"Message {i} with some content")
            cm.add_assistant_message([TextBlock(text=f"Response {i}")])

        original, new = cm.apply_compaction("Brief summary")
        assert new < original

    def test_apply_compaction_does_not_split_tool_round_trip(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("User message 0")
        cm.add_assistant_message([TextBlock(text="Assistant response 0")])
        cm.add_user_message("Please read a file")
        cm.add_assistant_message([ToolUseBlock(id="toolu_read", name="read_file", input={"path": "a.txt"})])
        cm.add_tool_results([ToolResultBlock(tool_use_id="toolu_read", content="file contents")])
        cm.add_assistant_message([TextBlock(text="Read complete")])
        cm.add_user_message("User message 2")
        cm.add_assistant_message([TextBlock(text="Assistant response 2")])
        cm.add_user_message("User message 3")
        cm.add_assistant_message([TextBlock(text="Assistant response 3")])

        cm.apply_compaction("Summary of old conversation")

        # 新语义：完整历史保留；断言有效切片(标记+尾部)。
        messages = cm.get_context_messages()
        assert "Summary" in messages[0].get_text()
        # 保留尾部从引导该工具往返的 user 提问开头开始,工具往返随后完整保留。
        assert messages[1].role == "user"
        assert messages[1].get_text() == "Please read a file"
        assert messages[2].role == "assistant"
        assert messages[2].get_tool_use_blocks()[0].id == "toolu_read"
        assert messages[3].role == "user"
        assert isinstance(messages[3].content, list)
        assert messages[3].content[0].tool_use_id == "toolu_read"

    def test_compaction_tail_starts_at_user_turn_not_assistant(self):
        # 朴素切分点(len-preserve_count)落在一次工具往返中间时,旧的切分只保证
        # 工具配对、会让保留尾部从 assistant 中途开始——引导它的 user 提问被并入摘要,
        # 弱模型在缺少提问上下文时易空转循环。修复后尾部必须从一条真正的 user 回合开头开始。
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("u0")  # 0
        cm.add_assistant_message([TextBlock(text="a0")])  # 1
        cm.add_user_message("leading prompt")  # 2  <- 期望尾部从这里开始
        cm.add_assistant_message([ToolUseBlock(id="t", name="bash", input={"command": "echo x"})])  # 3
        cm.add_tool_results([ToolResultBlock(tool_use_id="t", content="x")])  # 4  <- 朴素切分点(10-6)
        cm.add_assistant_message([TextBlock(text="a-mid")])  # 5
        cm.add_user_message("u2")  # 6
        cm.add_assistant_message([TextBlock(text="a2")])  # 7
        cm.add_user_message("u3")  # 8
        cm.add_assistant_message([TextBlock(text="a3")])  # 9

        cm.apply_compaction("Summary of old conversation")

        # 新语义：完整历史保留；断言有效切片(标记+尾部)。
        messages = cm.get_context_messages()
        assert "Summary" in messages[0].get_text()
        # 保留尾部第一条(摘要之后)必须是携带引导提问的 user 消息,而非 assistant 回复。
        assert messages[1].role == "user"
        assert "leading prompt" in messages[1].get_text()
        # 尾部不得以未配对的 tool_result 开头。
        assert not any(isinstance(block, ToolResultBlock) for block in messages[1].content)

    def test_compaction_keeps_unfinished_tool_use_in_recent_messages(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("User message 0")
        cm.add_assistant_message([TextBlock(text="Assistant response 0")])
        cm.add_user_message("Start a tool")
        cm.add_assistant_message([ToolUseBlock(id="toolu_pending", name="bash", input={"command": "sleep 1"})])
        cm.add_user_message("Follow-up after interrupted tool use")
        cm.add_assistant_message([TextBlock(text="Assistant response after interruption")])
        cm.add_user_message("User message 3")
        cm.add_assistant_message([TextBlock(text="Assistant response 3")])

        cm.apply_compaction("Summary of old conversation")

        # 新语义：完整历史保留；断言有效切片(标记+尾部)。
        messages = cm.get_context_messages()
        assert "Summary" in messages[0].get_text()
        assert any(
            msg.get_tool_use_blocks() and msg.get_tool_use_blocks()[0].id == "toolu_pending" for msg in messages[1:]
        )

    def test_single_user_turn_pipeline_step_still_compacts(self):
        # 流水线步骤形状:整段只有一个 user 回合开头(初始指令),其余全是 assistant/tool_result
        # 工具往返。严格切分会一路回退到 index 0 → old 为空 → needs_compaction 永远 False、
        # 压缩空转(见问题 #2:进度过半仍不触发)。两级策略:严格切分塌缩到 0 时放宽为从一条
        # 非 tool_result 载体(assistant)的消息开头,由压缩标记(user 摘要)在其前提供引导上下文。
        cm = ContextManager(system_prompt="", model="qwen")
        cm._config = cm._config.__class__(
            context_window=50,
            max_output_tokens=8192,
            compact_buffer=10,
            compact_threshold=0.5,
            preserve_recent_turns=3,
        )
        cm.add_user_message("步骤指令:生成模板")  # 0 —— 唯一的 user 回合开头
        for i in range(6):
            cm.add_assistant_message([ToolUseBlock(id=f"t{i}", name="bash", input={"command": f"echo {i}"})])
            cm.add_tool_results([ToolResultBlock(tool_use_id=f"t{i}", content="lorem ipsum " * 20)])

        old, _recent = cm._split_messages_for_compaction()
        assert old, "single-user-turn 形状下仍应切出可压缩的旧消息"
        assert cm.get_total_tokens() > 25  # 超过阈值 window * 0.5
        assert cm.needs_compaction() is True

        before = cm.get_total_tokens()
        _original, new = cm.apply_compaction("summary of tool rounds")
        assert new < before  # 压缩真正推进,不是空操作

        messages = cm.get_context_messages()
        assert "summary" in messages[0].get_text().lower()  # 压缩标记(user 摘要)
        # 尾部第一条(标记之后)不得是 tool_result 载体——避免孤立 tool_result;
        # 由标记提供引导上下文,允许从 assistant 开头。
        first_tail = messages[1]
        tail_blocks = first_tail.content if isinstance(first_tail.content, list) else []
        assert not any(isinstance(block, ToolResultBlock) for block in tail_blocks)

    def test_prior_summary_plus_single_huge_tool_turn_still_reduces(self):
        # 极端形态(线上会话 dec3dcd9):有效切片开头是上一次压缩留下的摘要标记,其后只有
        # 一个 user 回合(初始指令)+ 一长串超大 tool_result。严格切分为对齐 user 回合开头会一路
        # 回退,把整条工具链塞进 recent、old 只剩那条摘要标记 → 重压只是把摘要再总结一遍,大块
        # 工具结果原样保留 → 毫无缩减。修复:严格切出的 old 无可压缩内容时放宽切分,把较早的
        # 大块工具往返折进摘要。
        cm = ContextManager(system_prompt="", model="qwen")
        cm._config = cm._config.__class__(
            context_window=50,
            max_output_tokens=8192,
            compact_buffer=10,
            compact_threshold=0.5,
            preserve_recent_turns=3,
        )
        marker = create_compaction_summary_message("上一次压缩的摘要")  # 有效切片 index 0
        marker.token_count = 3
        cm._conversation.messages.append(marker)
        cm.add_user_message("步骤指令:研究更省钱的方案")  # 唯一的 user 回合开头
        for i in range(6):
            cm.add_assistant_message([ToolUseBlock(id=f"t{i}", name="bash", input={"command": f"echo {i}"})])
            cm.add_tool_results([ToolResultBlock(tool_use_id=f"t{i}", content="lorem ipsum " * 20)])

        # 严格切分:old 只剩摘要标记,无任何可压缩内容(病态形态)。
        naive = len(cm.get_context_messages()) - cm._config.preserve_recent_turns * 2
        strict = cm._find_safe_compaction_split(cm.get_context_messages(), naive)
        assert not cm._has_compactible_content(cm.get_context_messages()[:strict])

        # 修复后:两级切分兜住,old 含可压缩的工具往返,压缩真正推进。
        old, _recent = cm._split_messages_for_compaction()
        assert cm._has_compactible_content(old)
        assert cm.needs_compaction() is True

        before = cm.get_total_tokens()
        _original, new = cm.apply_compaction("折叠早期工具往返")
        assert new < before  # 不再是空操作

        messages = cm.get_context_messages()
        assert "折叠早期工具往返" in messages[0].get_text()  # 新摘要标记
        first_tail = messages[1]
        tail_blocks = first_tail.content if isinstance(first_tail.content, list) else []
        assert not any(isinstance(block, ToolResultBlock) for block in tail_blocks)


class TestSetModel:
    def test_set_model_preserves_messages(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("Hello")
        cm.add_assistant_message([TextBlock(text="Hi there")])
        assert len(cm.get_messages()) == 2

        cm.set_model("claude-opus-4-7")

        messages = cm.get_messages()
        assert len(messages) == 2
        assert messages[0].get_text() == "Hello"
        assert messages[1].get_text() == "Hi there"

    def test_set_model_swaps_context_window_config(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        assert cm._config.context_window == 131_072

        cm.set_model("claude-opus-4-7")
        assert cm._config.context_window == 200_000

    def test_set_model_recomputes_system_prompt_tokens(self):
        cm = ContextManager(system_prompt="A long system prompt " * 50, model="qwen")
        before = cm._system_prompt_tokens

        cm.set_model("claude-opus-4-7")
        after = cm._system_prompt_tokens
        # Both tokenizers count the same English text similarly, but the count
        # is recomputed against the new tokenizer — the value should be > 0.
        assert after > 0
        assert before > 0

    def test_set_model_noop_for_same_model(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("Hello")
        original_token_count = cm.get_messages()[0].token_count

        cm.set_model("qwen")
        assert cm.get_messages()[0].token_count == original_token_count

    def test_set_system_prompt_updates_tokens(self):
        cm = ContextManager(system_prompt="short", model="qwen")
        before = cm._system_prompt_tokens

        cm.set_system_prompt("a much longer system prompt " * 20)
        after = cm._system_prompt_tokens
        assert after > before
        assert cm.system_prompt == "a much longer system prompt " * 20

    def test_set_model_recomputes_tool_definition_tokens(self, monkeypatch):
        class FakeTokenCounter:
            def __init__(self, model=""):
                self.model = model

            def count_text(self, text):
                return len(text)

            def count_message(self, message):
                return 1

            def count_tool_definitions(self, tools):
                return 10 if self.model == "qwen" else 30

        monkeypatch.setattr("iac_code.services.context_manager.TokenCounter", FakeTokenCounter)
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.set_tool_definitions([SimpleNamespace(name="read_file", description="Read file", input_schema={})])
        assert cm.get_usage()["tool_definition_tokens"] == 10

        cm.set_model("claude-opus-4-7")

        assert cm.get_usage()["tool_definition_tokens"] == 30


def test_add_recalled_memory_message_tracks_surfaced_files():
    cm = ContextManager(system_prompt="sys", model="qwen")

    msg = cm.add_recalled_memory_message(
        "# Recalled Memory\nUse YAML for ROS templates",
        ["ros-yaml.md"],
    )

    assert msg.role == "user"
    assert msg.metadata["type"] == "recalled_memory"
    assert cm.get_surfaced_memory_files() == {"ros-yaml.md"}
    assert "Use YAML for ROS templates" in cm.get_api_messages()[0]["content"]


def test_compaction_surfaced_files_come_from_retained_metadata_only():
    cm = ContextManager(system_prompt="sys", model="qwen")
    cm.add_recalled_memory_message("# Recalled Memory\nOld memory", ["old.md"])
    for i in range(6):
        cm.add_user_message(f"User message {i}")
        cm.add_assistant_message(f"Assistant response {i}")
    cm.add_recalled_memory_message("# Recalled Memory\nRecent memory", ["recent.md"])

    cm.apply_compaction("Summary mentions old.md and recent.md")

    # 语义：get_surfaced_memory_files 反映"有效上下文已包含哪些浮出记忆",
    # 遍历有效切片而非完整历史。压缩后 old.md 的 recalled_memory 消息落在
    # 标记之前(被压缩出有效上下文),故不再被视为已浮出,可再次被召回。
    assert cm.get_surfaced_memory_files() == {"recent.md"}


def test_add_raw_message_preserves_metadata():
    cm = ContextManager(system_prompt="sys", model="qwen")

    msg = cm.add_raw_message(
        {
            "role": "user",
            "content": "hidden cleanup prompt",
            "metadata": {"type": CLEANUP_PROMPT_METADATA_TYPE, "source": "pipeline_cleanup"},
        }
    )

    assert msg.metadata == {"type": CLEANUP_PROMPT_METADATA_TYPE, "source": "pipeline_cleanup"}
    assert cm.get_messages()[0].metadata["type"] == CLEANUP_PROMPT_METADATA_TYPE


def _mgr_with_turns(n_turns: int) -> ContextManager:
    mgr = ContextManager(system_prompt="sys", model="claude")
    for i in range(n_turns):
        mgr.add_user_message(f"u{i}")
        mgr.add_assistant_message(f"a{i}")
    return mgr


def test_get_context_messages_no_marker_is_full_history():
    mgr = _mgr_with_turns(2)
    assert mgr.get_context_messages() == mgr.get_messages()


def test_get_context_messages_slices_from_last_marker():
    mgr = _mgr_with_turns(2)
    marker = create_compaction_summary_message("S")
    mgr._conversation.messages.insert(2, marker)  # [u0,a0,marker,u1,a1]
    ctx = mgr.get_context_messages()
    assert ctx[0] is marker
    assert len(ctx) == 3  # marker + u1 + a1


def test_get_context_messages_picks_latest_of_multiple_markers():
    mgr = _mgr_with_turns(3)
    m1 = create_compaction_summary_message("S1")
    m2 = create_compaction_summary_message("S2")
    mgr._conversation.messages.insert(1, m1)
    mgr._conversation.messages.insert(4, m2)
    ctx = mgr.get_context_messages()
    assert ctx[0] is m2


def test_get_api_messages_uses_effective_context():
    mgr = _mgr_with_turns(2)
    marker = create_compaction_summary_message("SUMMARY")
    mgr._conversation.messages.insert(2, marker)
    api = mgr.get_api_messages()
    assert len(api) == 3
    assert "[Conversation Summary]" in str(api[0]["content"])


def test_get_total_tokens_counts_only_effective_slice():
    mgr = _mgr_with_turns(4)
    full_tokens = mgr.get_total_tokens()
    marker = create_compaction_summary_message("S")
    # 在靠后位置插标记 → 有效切片显著变短 → token 下降
    mgr._conversation.messages.insert(6, marker)
    assert mgr.get_total_tokens() < full_tokens


def test_current_user_text_with_summary_prefix_is_not_a_compaction_boundary():
    mgr = _mgr_with_turns(4)
    ordinary = Message(role="user", content="[Conversation Summary]\nplease inspect this literal text")
    mgr._conversation.messages.insert(4, ordinary)

    assert is_compaction_summary_message(ordinary) is False
    assert mgr.get_context_messages() == mgr.get_messages()


def test_apply_compaction_preserves_full_history():
    mgr = _mgr_with_turns(4)  # 8 msgs, preserve 6 → old=2
    original_objs = list(mgr.get_messages())
    mgr.apply_compaction("SUM")
    full = mgr.get_messages()
    assert len(full) == len(original_objs) + 1  # 仅多一个标记
    for obj in original_objs:
        assert obj in full  # 旧对象仍在（未丢）


def test_apply_compaction_effective_slice_is_marker_plus_recent():
    mgr = _mgr_with_turns(4)
    mgr.apply_compaction("SUM")
    ctx = mgr.get_context_messages()
    assert is_compaction_summary_message(ctx[0])
    assert ctx[0].get_text() == "[Conversation Summary]\nSUM"
    assert len(ctx) == 1 + 6  # marker + 最近 3 轮


def test_apply_compaction_records_tail_count():
    mgr = _mgr_with_turns(4)  # 8 msgs, preserve 6 → old=2, recent=6
    mgr.apply_compaction("SUM")
    marker = mgr.get_context_messages()[0]
    assert is_compaction_summary_message(marker)
    # 标记之后、时间上早于它的保留尾部 = recent 6 条（无隐藏 cleanup）。
    assert marker.metadata[COMPACTION_SUMMARY_TAIL_METADATA_KEY] == 6
    assert compaction_summary_tail_count(marker) == 6


def test_apply_compaction_tail_count_includes_preserved_cleanup():
    mgr = _mgr_with_turns(4)
    cleanup = Message(role="user", content="CLEANUP", metadata={"type": CLEANUP_PROMPT_METADATA_TYPE})
    cleanup.token_count = 1
    mgr._conversation.messages.insert(0, cleanup)  # 落在会被压缩的旧段
    mgr.apply_compaction("SUM")
    marker = mgr.get_context_messages()[0]
    # 上浮的隐藏 cleanup(1) + recent(6) 都排在标记之后,均计入尾部。
    assert compaction_summary_tail_count(marker) == 7


def test_apply_compaction_noop_when_nothing_old():
    mgr = _mgr_with_turns(2)  # 4 msgs <= preserve 6 → old 空
    before = list(mgr.get_messages())
    orig, new = mgr.apply_compaction("SUM")
    assert mgr.get_messages() == before
    assert orig == new


def test_apply_compaction_floats_cleanup_prompt_once():
    mgr = _mgr_with_turns(4)
    cleanup = Message(role="user", content="CLEANUP", metadata={"type": CLEANUP_PROMPT_METADATA_TYPE})
    cleanup.token_count = 1
    mgr._conversation.messages.insert(0, cleanup)  # 落在会被压缩的旧段
    mgr.apply_compaction("SUM")
    full = mgr.get_messages()
    assert sum(1 for m in full if m.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE) == 1
    assert cleanup in mgr.get_context_messages()  # 上浮进有效切片


def test_second_compaction_keeps_both_markers_and_summarizes_first():
    mgr = _mgr_with_turns(4)
    mgr.apply_compaction("S1")
    prompt = mgr.build_compaction_prompt()  # 基于有效切片；旧段含 marker1
    assert "S1" in prompt
    mgr.apply_compaction("S2")
    full = mgr.get_messages()
    markers = [m for m in full if is_compaction_summary_message(m)]
    assert len(markers) == 2
    assert mgr.get_context_messages()[0].get_text() == "[Conversation Summary]\nS2"
