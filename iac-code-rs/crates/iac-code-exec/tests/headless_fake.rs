use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_exec::{HeadlessRunner, OutputFormat};
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessageContent, ImageBlock, TextBlock, ToolResultBlock,
};
use iac_code_protocol::provider::ToolDefinition;
use iac_code_protocol::{
    json, MessageEndEvent, StreamEvent, ToolResultEvent, ToolUseEndEvent, ToolUseStartEvent, Usage,
};
use iac_code_providers::fake::{FakeProvider, FakeScenario};
use iac_code_providers::EventProvider;
use iac_code_tools::{ToolCallRequest, ToolExecutor, ToolResult};

#[derive(Debug)]
struct CommandFixture {
    exit_code: i32,
    stdout: String,
    stderr: String,
}

#[test]
fn headless_fake_text_matches_python_fixture() {
    assert_fixture("text", OutputFormat::Text, FakeScenario::Text, 100);
}

#[test]
fn headless_fake_json_matches_python_fixture() {
    assert_fixture("json", OutputFormat::Json, FakeScenario::Text, 100);
}

#[test]
fn headless_fake_stream_json_matches_python_fixture() {
    assert_fixture(
        "stream_json",
        OutputFormat::StreamJson,
        FakeScenario::Text,
        100,
    );
}

#[test]
fn headless_fake_max_turns_matches_python_fixture() {
    assert_fixture("max_turns", OutputFormat::Text, FakeScenario::Text, 0);
}

#[test]
fn headless_fake_result_tracks_token_count() {
    let runner = HeadlessRunner::new(
        FakeProvider::new(FakeScenario::Text),
        OutputFormat::Text,
        100,
    );
    let actual = runner.run("hello");

    assert_eq!(actual.token_count, 10);
}

#[test]
fn headless_runner_passes_model_to_context_token_counter() {
    let runner = HeadlessRunner::new(
        FakeProvider::new(FakeScenario::Text),
        OutputFormat::Text,
        100,
    )
    .with_model("qwen3.6-plus");

    let actual = runner.run("基础设施代码");

    assert_eq!(actual.conversation.messages[0].token_count, 10);
}

#[test]
fn headless_runner_accepts_structured_user_content() {
    let runner = HeadlessRunner::new(
        FakeProvider::new(FakeScenario::Text),
        OutputFormat::Text,
        100,
    );
    let content = AgentMessageContent::Blocks(vec![
        AgentContentBlock::Text(TextBlock {
            text: "describe".into(),
        }),
        AgentContentBlock::Image(ImageBlock {
            media_type: "image/png".into(),
            data: "base64-image".into(),
        }),
    ]);

    let actual = runner.run_content(content.clone());

    assert_eq!(actual.conversation.messages[0].content, content);
    assert_eq!(actual.stdout.trim(), "fixture response: describe");
}

#[test]
fn headless_runner_passes_result_storage_dir_to_agent_loop() {
    let storage_dir = unique_temp_dir("iac-code-rs-exec-large-tool-results");
    let full_result = "x".repeat(50_001);
    let expected_path = storage_dir.join("toolu_1.txt");
    let expected_preview = format!(
        "{}\n\n... [truncated \u{2014} full output (50001 chars) saved to {}]",
        "x".repeat(2_000),
        expected_path.display()
    );
    let runner = HeadlessRunner::new(ToolCallingProvider, OutputFormat::Text, 1)
        .with_result_storage_dir(&storage_dir);

    let actual = runner.run_with_tool_executor(
        "run large tool",
        LargeToolResultExecutor {
            content: full_result.clone(),
        },
    );

    assert!(actual.events.iter().any(|event| matches!(
        event,
        StreamEvent::ToolResult(ToolResultEvent { result, .. }) if result == &expected_preview
    )));
    assert_eq!(
        fs::read_to_string(&expected_path).expect("externalized result should be readable"),
        full_result
    );
    assert_eq!(
        actual.conversation.messages[2].content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(ToolResultBlock {
            tool_use_id: "toolu_1".into(),
            content: expected_preview,
            is_error: false,
        })])
    );

    fs::remove_dir_all(storage_dir).ok();
}

fn assert_fixture(name: &str, output_format: OutputFormat, scenario: FakeScenario, max_turns: u32) {
    let expected = fixture(name);
    let runner = HeadlessRunner::new(FakeProvider::new(scenario), output_format, max_turns);
    let actual = runner.run("hello");

    assert_eq!(actual.exit_code, expected.exit_code, "{name} exit code");
    assert_eq!(actual.stdout, expected.stdout, "{name} stdout");
    assert_eq!(actual.stderr, expected.stderr, "{name} stderr");
}

fn fixture(name: &str) -> CommandFixture {
    let path = fixture_root().join(format!("{name}.json"));
    let text = fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("failed to read {}: {err}", path.display()));
    CommandFixture {
        exit_code: json_i32_field(&text, "exit_code"),
        stdout: json_string_field(&text, "stdout"),
        stderr: json_string_field(&text, "stderr"),
    }
}

fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .expect("workspace root")
        .join("fixtures")
        .join("compatibility")
        .join("headless_fake")
}

fn json_i32_field(text: &str, field: &str) -> i32 {
    let marker = format!("\"{field}\":");
    let start = text
        .find(&marker)
        .unwrap_or_else(|| panic!("missing field {field}"))
        + marker.len();
    let rest = text[start..].trim_start();
    let end = rest.find([',', '\n', '}']).unwrap_or(rest.len());
    rest[..end]
        .trim()
        .parse()
        .unwrap_or_else(|err| panic!("invalid integer field {field}: {err}"))
}

fn json_string_field(text: &str, field: &str) -> String {
    let marker = format!("\"{field}\":");
    let start = text
        .find(&marker)
        .unwrap_or_else(|| panic!("missing field {field}"))
        + marker.len();
    parse_json_string(text[start..].trim_start()).0
}

fn parse_json_string(text: &str) -> (String, usize) {
    assert!(text.starts_with('"'), "expected JSON string");
    let mut out = String::new();
    let mut escaped = false;
    for (idx, ch) in text[1..].char_indices() {
        if escaped {
            match ch {
                '"' => out.push('"'),
                '\\' => out.push('\\'),
                '/' => out.push('/'),
                'b' => out.push('\u{0008}'),
                'f' => out.push('\u{000c}'),
                'n' => out.push('\n'),
                'r' => out.push('\r'),
                't' => out.push('\t'),
                other => panic!("unsupported JSON escape: {other}"),
            }
            escaped = false;
        } else if ch == '\\' {
            escaped = true;
        } else if ch == '"' {
            return (out, idx + 2);
        } else {
            out.push(ch);
        }
    }
    panic!("unterminated JSON string")
}

#[derive(Clone)]
struct ToolCallingProvider;

impl EventProvider for ToolCallingProvider {
    fn stream_events(
        &self,
        _conversation: &iac_code_protocol::message::Conversation,
        _system: &str,
        _tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        vec![
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
                input: json::object([("text", json::string("hello"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage::default(),
            }),
        ]
    }
}

struct LargeToolResultExecutor {
    content: String,
}

impl ToolExecutor for LargeToolResultExecutor {
    fn execute(&self, _request: ToolCallRequest) -> ToolResult {
        ToolResult::success(self.content.clone())
    }
}

fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}
