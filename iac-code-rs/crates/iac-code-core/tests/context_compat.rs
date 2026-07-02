use iac_code_core::{context_window_config, ContextManager, TokenBudget};
use iac_code_protocol::json;
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessageContent, TextBlock, ToolResultBlock, ToolUseBlock,
};
use iac_code_protocol::provider::ToolDefinition;

#[test]
fn context_window_configs_match_python_prefixes() {
    let claude = context_window_config("claude-opus-4-7");
    assert_eq!(claude.context_window, 200_000);
    assert_eq!(claude.max_output_tokens, 8_192);
    assert_eq!(claude.compact_buffer, 20_000);
    assert_eq!(claude.compact_threshold, 0.93);
    assert_eq!(claude.preserve_recent_turns, 3);

    assert_eq!(
        context_window_config("qwen3.6-plus").context_window,
        131_072
    );
    assert_eq!(context_window_config("gpt-4-turbo").context_window, 128_000);
    assert_eq!(
        context_window_config("unknown-model").context_window,
        128_000
    );
}

#[test]
fn context_usage_counts_system_tools_and_messages_like_python() {
    let mut context = ContextManager::new("You are helpful.", "qwen");
    let base_total = context.total_tokens();

    context.set_tool_definitions(vec![ToolDefinition {
        name: "create_stack".into(),
        description: "Create a ROS stack".into(),
        input_schema: json::object([(
            "properties",
            json::object([("template", json::string("Template body"))]),
        )]),
    }]);
    context.add_user_message(AgentMessageContent::Text("Hello".into()));
    context.add_assistant_message(AgentMessageContent::Blocks(vec![AgentContentBlock::Text(
        TextBlock {
            text: "Hi there".into(),
        },
    )]));
    context.add_tool_results(vec![ToolResultBlock {
        tool_use_id: "toolu_1".into(),
        content: "result".into(),
        is_error: false,
    }]);

    let usage = context.usage();
    assert!(usage.system_prompt_tokens > 0);
    assert!(usage.tool_definition_tokens > 0);
    assert!(usage.user_message_tokens > 0);
    assert!(usage.assistant_message_tokens > 0);
    assert!(usage.tool_result_tokens > 0);
    assert_eq!(usage.context_window, 131_072);
    assert_eq!(usage.message_count, 3);
    assert_eq!(usage.total_tokens, context.total_tokens());
    assert!(context.total_tokens() > base_total);
    assert!(usage.usage_percent > 0.0);
    assert!(!context.needs_compaction());
}

#[test]
fn context_compaction_preserves_tool_round_trip_boundaries() {
    let mut context = ContextManager::new("sys", "qwen");
    context.add_user_message(AgentMessageContent::Text("User message 0".into()));
    context.add_assistant_message(AgentMessageContent::Blocks(vec![AgentContentBlock::Text(
        TextBlock {
            text: "Assistant response 0".into(),
        },
    )]));
    context.add_user_message(AgentMessageContent::Text("Please read a file".into()));
    context.add_assistant_message(AgentMessageContent::Blocks(vec![
        AgentContentBlock::ToolUse(ToolUseBlock {
            id: "toolu_read".into(),
            name: "read_file".into(),
            input: json::object([("path", json::string("a.txt"))]),
        }),
    ]));
    context.add_tool_results(vec![ToolResultBlock {
        tool_use_id: "toolu_read".into(),
        content: "file contents".into(),
        is_error: false,
    }]);
    context.add_assistant_message(AgentMessageContent::Blocks(vec![AgentContentBlock::Text(
        TextBlock {
            text: "Read complete".into(),
        },
    )]));
    context.add_user_message(AgentMessageContent::Text("User message 2".into()));
    context.add_assistant_message(AgentMessageContent::Blocks(vec![AgentContentBlock::Text(
        TextBlock {
            text: "Assistant response 2".into(),
        },
    )]));
    context.add_user_message(AgentMessageContent::Text("User message 3".into()));
    context.add_assistant_message(AgentMessageContent::Blocks(vec![AgentContentBlock::Text(
        TextBlock {
            text: "Assistant response 3".into(),
        },
    )]));

    let prompt = context.build_compaction_prompt();
    assert!(prompt.contains("USER: User message 0"));
    assert!(!prompt.contains("Read complete"));

    let (original, compacted) = context.apply_compaction("Summary of old conversation");
    assert!(compacted < original);
    let messages = context.messages();
    assert!(messages[0].get_text().contains("Summary"));
    assert_eq!(messages[1].role, "assistant");
    assert_eq!(messages[1].get_tool_use_blocks()[0].id, "toolu_read");
    assert_eq!(messages[2].role, "user");
    assert_eq!(
        messages[2].get_tool_result_blocks()[0].tool_use_id,
        "toolu_read"
    );
}

#[test]
fn token_budget_parses_python_shorthand_and_tracks_usage() {
    assert_eq!(TokenBudget::parse_shorthand("500k").unwrap(), 500_000);
    assert_eq!(TokenBudget::parse_shorthand("1.5k").unwrap(), 1_500);
    assert_eq!(TokenBudget::parse_shorthand("1 k").unwrap(), 1_000);
    assert_eq!(TokenBudget::parse_shorthand("1M").unwrap(), 1_000_000);
    assert_eq!(TokenBudget::parse_shorthand("+200k").unwrap(), 200_000);
    assert!(TokenBudget::parse_shorthand("abc").is_err());
    assert!(TokenBudget::parse_shorthand("1.").is_err());
    assert!(TokenBudget::parse_shorthand(".5k").is_err());

    let mut budget = TokenBudget::from_shorthand("100k").unwrap();
    assert_eq!(budget.remaining(), Some(100_000));
    budget.consume(25_000);
    assert_eq!(budget.used(), 25_000);
    assert_eq!(budget.remaining(), Some(75_000));
    assert_eq!(budget.usage_percent(), 25.0);
    assert!(!budget.is_exhausted());

    let mut unlimited = TokenBudget::unlimited();
    unlimited.consume(999_999_999);
    assert_eq!(unlimited.remaining(), None);
    assert!(!unlimited.is_exhausted());
}
