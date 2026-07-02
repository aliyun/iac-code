use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use iac_code_core::{
    normalize_session_name, sanitize_path, SessionIndex, SessionStorage, SESSION_JSONL_FILENAME,
    SESSION_METADATA_FILENAME,
};
use iac_code_protocol::json;
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessage, AgentMessageContent, TextBlock, ToolResultBlock, ToolUseBlock,
};

const CWD: &str = "/tmp/proj-x";

#[test]
fn new_session_uses_directory_format_stamps_messages_and_skips_lite_meta() {
    let root = unique_temp_dir("iac-code-rs-session-new");
    let storage = SessionStorage::new(&root).expect("session storage should initialize");

    storage
        .append(CWD, "s1", &user_text("Hello"), Some("main"))
        .expect("user message should be appended");
    storage
        .append(CWD, "s1", &assistant_text("Hi!"), Some("main"))
        .expect("assistant message should be appended");
    storage
        .append_meta(
            CWD,
            "s1",
            json::object([
                ("type", json::string("last-prompt")),
                ("last_prompt", json::string("Hello")),
            ]),
        )
        .expect("lite metadata should be appended");

    assert_eq!(
        storage.session_path(CWD, "s1"),
        root.join(sanitize_path(CWD))
            .join("s1")
            .join(SESSION_JSONL_FILENAME)
    );
    assert!(!storage.legacy_session_path(CWD, "s1").exists());

    let raw = fs::read_to_string(storage.session_path(CWD, "s1"))
        .expect("session jsonl should be readable");
    let first_line = raw.lines().next().expect("session should have a first row");
    assert!(first_line.contains("\"session_id\":\"s1\""));
    assert!(first_line.contains("\"cwd\":\"/tmp/proj-x\""));
    assert!(first_line.contains("\"git_branch\":\"main\""));
    assert!(first_line.contains("\"version\":"));

    let loaded = storage.load(CWD, "s1").expect("session should load");
    assert_eq!(loaded, vec![user_text("Hello"), assistant_text("Hi!")]);

    fs::remove_dir_all(root).ok();
}

#[test]
fn structured_messages_round_trip_through_jsonl() {
    let root = unique_temp_dir("iac-code-rs-session-structured");
    let storage = SessionStorage::new(&root).expect("session storage should initialize");
    let messages = vec![
        user_text("read status"),
        AgentMessage {
            role: "assistant".into(),
            content: AgentMessageContent::Blocks(vec![AgentContentBlock::ToolUse(ToolUseBlock {
                id: "toolu_1".into(),
                name: "read_file".into(),
                input: json::object([("path", json::string("status.txt"))]),
            })]),
            token_count: 0,
            elapsed_seconds: 0.0,
        },
        AgentMessage {
            role: "user".into(),
            content: AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(
                ToolResultBlock {
                    tool_use_id: "toolu_1".into(),
                    content: "alpha\n".into(),
                    is_error: false,
                },
            )]),
            token_count: 0,
            elapsed_seconds: 0.0,
        },
    ];

    storage
        .save(CWD, "tools", &messages, None)
        .expect("session should be saved");

    assert_eq!(
        storage.load(CWD, "tools").expect("session should load"),
        messages
    );

    fs::remove_dir_all(root).ok();
}

#[test]
fn existing_legacy_session_stays_legacy_until_rename_then_writes_metadata() {
    let root = unique_temp_dir("iac-code-rs-session-legacy");
    let storage = SessionStorage::new(&root).expect("session storage should initialize");
    let legacy_path = storage.legacy_session_path(CWD, "legacy");
    fs::create_dir_all(
        legacy_path
            .parent()
            .expect("legacy path should have parent"),
    )
    .expect("legacy project dir should be created");
    fs::write(&legacy_path, "{\"role\":\"user\",\"content\":\"old\"}\n")
        .expect("legacy jsonl should be written");

    storage
        .append(CWD, "legacy", &assistant_text("next"), None)
        .expect("append should keep legacy file");

    assert!(legacy_path.exists());
    assert!(!storage.session_dir(CWD, "legacy").exists());
    assert_eq!(
        storage
            .load(CWD, "legacy")
            .expect("legacy session should load"),
        vec![user_text("old"), assistant_text("next")]
    );

    assert_eq!(
        storage
            .rename_session(CWD, "legacy", "deploy-prod", Some("main"))
            .expect("legacy session should be renamed"),
        "renamed"
    );
    let session_dir = storage.session_dir(CWD, "legacy");
    assert!(!legacy_path.exists());
    assert!(session_dir.join(SESSION_JSONL_FILENAME).exists());
    assert!(session_dir.join(SESSION_METADATA_FILENAME).exists());

    let metadata = storage
        .read_metadata(CWD, "legacy")
        .expect("metadata should exist");
    assert_eq!(metadata.session_id, "legacy");
    assert_eq!(metadata.name.as_deref(), Some("deploy-prod"));
    assert_eq!(metadata.cwd.as_deref(), Some(CWD));
    assert_eq!(metadata.git_branch.as_deref(), Some("main"));
    assert_eq!(metadata.schema_version, 1);

    fs::remove_dir_all(root).ok();
}

#[test]
fn session_name_validation_and_duplicate_detection_match_python() {
    let root = unique_temp_dir("iac-code-rs-session-rename");
    let storage = SessionStorage::new(&root).expect("session storage should initialize");

    assert_eq!(
        normalize_session_name(" deploy-prod ").expect("valid name"),
        "deploy-prod"
    );
    assert!(normalize_session_name("deploy prod").is_err());
    assert!(normalize_session_name("中文").is_err());

    storage
        .append(CWD, "one", &user_text("one"), None)
        .expect("first session should be written");
    storage
        .append(CWD, "two", &user_text("two"), None)
        .expect("second session should be written");
    storage
        .rename_session(CWD, "one", "deploy-prod", None)
        .expect("first session should be named");

    let error = storage
        .rename_session(CWD, "two", "deploy-prod", None)
        .expect_err("duplicate name should be rejected");
    assert!(error.to_string().contains("already exists"));

    storage
        .append("/tmp/other", "three", &user_text("three"), None)
        .expect("other project session should be written");
    assert_eq!(
        storage
            .rename_session("/tmp/other", "three", "deploy-prod", None)
            .expect("same name in another project is allowed"),
        "renamed"
    );

    fs::remove_dir_all(root).ok();
}

#[test]
fn sanitize_path_long_names_match_python_blake2b_suffix() {
    let name = format!("/tmp/{}/repo", "a".repeat(210));

    assert_eq!(
        sanitize_path(&name),
        "-tmp-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-a9a8fdbc1267"
    );
}

#[test]
fn cross_project_lookup_latest_and_index_ignore_usage_sidecars() {
    let root = unique_temp_dir("iac-code-rs-session-index");
    let storage = SessionStorage::new(&root).expect("session storage should initialize");

    storage
        .append("/tmp/a", "older", &user_text("old"), Some("main"))
        .expect("older session should be written");
    thread::sleep(Duration::from_millis(20));
    storage
        .append("/tmp/b", "newer", &user_text("new"), Some("feature"))
        .expect("newer session should be written");
    fs::write(
        storage.session_dir("/tmp/b", "newer").join("usage.jsonl"),
        "{\"type\":\"usage\",\"input_tokens\":10}\n",
    )
    .expect("usage sidecar should be written");
    fs::write(
        root.join(sanitize_path("/tmp/b")).join("newer.usage.jsonl"),
        "{\"type\":\"usage\",\"input_tokens\":20}\n",
    )
    .expect("legacy usage sidecar should be written");
    storage
        .append_meta(
            "/tmp/b",
            "newer",
            json::object([
                ("type", json::string("last-prompt")),
                ("last_prompt", json::string("new prompt")),
            ]),
        )
        .expect("lite metadata should be appended");
    storage
        .rename_session("/tmp/b", "newer", "deploy-prod", Some("feature"))
        .expect("session should be named");

    let found = storage
        .find_session_anywhere("newer")
        .expect("session should be found by id across projects");
    assert_eq!(found.0, "/tmp/b");
    assert_eq!(
        found.1.file_name().and_then(|name| name.to_str()),
        Some(SESSION_JSONL_FILENAME)
    );
    assert_eq!(
        storage
            .get_latest_session_anywhere()
            .expect("latest session should be found"),
        ("/tmp/b".to_owned(), "newer".to_owned())
    );

    let index = SessionIndex::new(&root);
    let entries = index
        .list_for_cwd("/tmp/b")
        .expect("current project index should load");
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].session_id, "newer");
    assert_eq!(entries[0].name.as_deref(), Some("deploy-prod"));
    assert_eq!(entries[0].title, "deploy-prod");
    assert_eq!(entries[0].git_branch.as_deref(), Some("feature"));
    assert!(!entries[0].is_legacy);
    assert_eq!(
        index
            .find_by_id_or_prefix("new")
            .expect("unique prefix should resolve")
            .session_id,
        "newer"
    );

    fs::remove_dir_all(root).ok();
}

#[test]
fn session_index_ignores_middle_last_prompt_outside_python_tail_window() {
    let root = unique_temp_dir("iac-code-rs-session-index-tail-window");
    let storage = SessionStorage::new(&root).expect("session storage should initialize");

    storage
        .append("/tmp/large", "large", &user_text("first prompt"), None)
        .expect("session should be written");
    let path = storage.session_path("/tmp/large", "large");
    let mut file = OpenOptions::new()
        .append(true)
        .open(&path)
        .expect("session should be appendable");
    writeln!(
        file,
        "{{\"type\":\"last-prompt\",\"last_prompt\":\"middle prompt\",\"session_id\":\"large\"}}"
    )
    .expect("middle lite metadata should be written");
    for _ in 0..1400 {
        writeln!(
            file,
            "{{\"type\":\"padding\",\"value\":\"{}\"}}",
            "x".repeat(80)
        )
        .expect("padding should be written");
    }

    let entries = SessionIndex::new(&root)
        .list_for_cwd("/tmp/large")
        .expect("session index should load");

    assert_eq!(entries[0].title, "first prompt");
    assert_eq!(entries[0].auto_title.as_deref(), Some("first prompt"));

    fs::remove_dir_all(root).ok();
}

#[test]
fn interrupted_tool_use_detection_and_repair_add_synthetic_results() {
    let messages = vec![
        user_text("do something"),
        AgentMessage {
            role: "assistant".into(),
            content: AgentMessageContent::Blocks(vec![AgentContentBlock::ToolUse(ToolUseBlock {
                id: "t1".into(),
                name: "bash".into(),
                input: json::object([("command", json::string("ls"))]),
            })]),
            token_count: 0,
            elapsed_seconds: 0.0,
        },
    ];

    assert!(SessionStorage::detect_interruption(&messages));
    let repaired = SessionStorage::repair_interrupted(&messages);

    assert_eq!(repaired.len(), 3);
    assert_eq!(repaired[2].role, "user");
    assert_eq!(
        repaired[2].content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(ToolResultBlock {
            tool_use_id: "t1".into(),
            content: "Session interrupted before tool execution completed.".into(),
            is_error: true,
        })])
    );
}

fn user_text(text: &str) -> AgentMessage {
    AgentMessage {
        role: "user".into(),
        content: AgentMessageContent::Text(text.into()),
        token_count: 0,
        elapsed_seconds: 0.0,
    }
}

fn assistant_text(text: &str) -> AgentMessage {
    AgentMessage {
        role: "assistant".into(),
        content: AgentMessageContent::Blocks(vec![AgentContentBlock::Text(TextBlock {
            text: text.into(),
        })]),
        token_count: 0,
        elapsed_seconds: 0.0,
    }
}

fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}
