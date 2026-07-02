use std::fs;
use std::thread;

use iac_code_config::settings::save_active_provider_config;
use iac_code_core::SessionStorage;
use iac_code_exec::EXIT_OK;
use iac_code_protocol::json;
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessage, AgentMessageContent, Conversation, TextBlock,
};
use iac_code_tools::TaskManager;
use iac_code_tui::{ResumePickerState, ResumeSessionEntry};

use crate::interactive_session::{raw_prompt_action_context, InteractiveSessionState};
use crate::raw_model_context::raw_model_picker_context;
use crate::raw_picker::RawPickerSearchQuery;
use crate::raw_prompt_context::RawPromptActionContext;
use crate::raw_prompt_input::read_raw_interactive_prompt_input_with_context_and_image_source;
use crate::raw_resume::{
    raw_resume_picker_clear_sequence, render_raw_resume_picker, RawResumeSessionEntry,
};
use crate::raw_resume_preview::raw_resume_preview_body_lines;
use crate::test_support::{
    assert_bytes_contains, english_locale_guard, paths_for, raw_prompt_render,
    raw_strip_ansi_sequences, raw_visible_lines_from_terminal_output, read_fd_exact,
    read_fd_until_contains, terminal_mode_bytes, unique_temp_dir, write_fd, EnvVarGuard,
    PseudoTerminal, StaticRawPromptImageSource,
};

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_resume_opens_picker_and_inserts_selected_session() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-resume-picker");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let root_text = root.to_string_lossy().into_owned();
            let context = RawPromptActionContext {
                resume_current_project_entries: vec![
                    RawResumeSessionEntry::new(
                        "session-alpha",
                        root_text.clone(),
                        "workspace",
                        "alpha stack",
                    ),
                    RawResumeSessionEntry::new(
                        "session-deploy",
                        root_text,
                        "workspace",
                        "deploy prod",
                    )
                    .with_name("deploy-prod"),
                ],
                ..RawPromptActionContext::default()
            };
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/resume\rdeploy\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    let visible_lines = raw_visible_lines_from_terminal_output(&output);
    assert!(
        visible_lines.iter().any(|line| line.contains("/resume")),
        "{visible_lines:?}"
    );
    assert!(
        visible_lines.iter().all(|line| !line.contains("resume>")),
        "{visible_lines:?}"
    );
    assert!(
        visible_lines
            .iter()
            .any(|line| line.contains("❯ deploy prod")),
        "{visible_lines:?}"
    );
    assert_bytes_contains(&output, &raw_prompt_render("/resume session-deploy"));
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/resume session-deploy");
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_resume_picker_keeps_command_line_visible() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit_tail = b"\x1b[?2004l";
    let root = unique_temp_dir("iac-code-rs-raw-prompt-resume-keeps-command");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let context = RawPromptActionContext {
                resume_current_project_entries: vec![RawResumeSessionEntry::new(
                    "session-alpha",
                    root.to_string_lossy(),
                    "workspace",
                    "alpha stack",
                )],
                ..RawPromptActionContext::default()
            };
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/resume\r");
    let mut output = read_fd_until_contains(pty.master, b"Resume Session");
    let visible_lines = raw_visible_lines_from_terminal_output(&output);
    assert!(
        visible_lines.iter().any(|line| line.contains("/resume")),
        "{visible_lines:?}"
    );
    write_fd(pty.master, b"\x1b");
    if !output
        .windows(expected_exit_tail.len())
        .any(|window| window == expected_exit_tail)
    {
        output.extend(read_fd_until_contains(pty.master, expected_exit_tail));
    }
    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/resume");
    assert!(input.prehandled);
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_resume_space_opens_alt_screen_preview() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    pty.set_size(8, 40);
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-resume-preview-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-resume-preview");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    let root_text = root.to_string_lossy().into_owned();
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("preview user prompt".to_owned()));
    conversation.add_assistant_message(AgentMessageContent::Text(
        "preview assistant answer".to_owned(),
    ));
    SessionStorage::new(paths.subdirs().projects)
        .expect("session storage should init")
        .save(
            &root_text,
            "session-preview",
            &conversation.messages,
            Some("main"),
        )
        .expect("preview session should be saved");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        let paths = paths.clone();
        move || {
            let root_text = root.to_string_lossy().into_owned();
            let state = InteractiveSessionState {
                resume: String::new(),
                continue_session: false,
                exit_code: EXIT_OK,
                turn_count: 0,
                token_count: 0,
                debug_enabled: false,
                debug_log_path: None,
                current_session_id: None,
                task_manager: TaskManager::new(),
                input_history: None,
                transcript_lines: Vec::new(),
            };
            let context = raw_prompt_action_context(Some(&paths), &root_text, &state);
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/resume\r");
    let mut output = read_fd_until_contains(pty.master, "恢复会话".as_bytes());
    write_fd(pty.master, b" ");
    output.extend(read_fd_until_contains(
        pty.master,
        b"preview assistant answer",
    ));
    assert_bytes_contains(&output, b"\x1b[?1049h");
    assert_bytes_contains(&output, b"preview user prompt");
    write_fd(pty.master, b"\x1b");
    output.extend(read_fd_until_contains(pty.master, "恢复会话".as_bytes()));
    assert!(
        raw_visible_lines_from_terminal_output(&output)
            .iter()
            .all(|line| !line.contains("resume>")),
        "{}",
        String::from_utf8_lossy(&output)
    );
    assert_bytes_contains(&output, b"\x1b[?1049l");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/resume session-preview"));
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/resume session-preview");
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_resume_picker_renders_python_style_chinese_selector() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    pty.set_size(24, 140);
    let entries = vec![
        ResumeSessionEntry::new(
            "b4780888-9a18-4521-a859-c5fb782b40dc",
            "/Users/prodesire/projects/ali-github/iac-code",
            "iac-code",
            "图片里有什么字",
            1_700_000_000,
            39 * 1024,
        )
        .with_git_branch("feature/iac-code-ui"),
        ResumeSessionEntry::new(
            "a111",
            "/Users/prodesire/projects/ali-github/iac-code",
            "iac-code",
            "整体看下这个项目干嘛的",
            1_699_900_000,
            13 * 1024,
        )
        .with_git_branch("feature/iac-code-ui"),
    ];
    let state = ResumePickerState::new(entries.clone(), entries, None, None, 10);
    let query = RawPickerSearchQuery::new();

    let line_count = render_raw_resume_picker(pty.slave, 0, &query, &state)
        .expect("resume picker should render");
    let output = read_fd_until_contains(pty.master, "Esc 取消".as_bytes());
    let visible_lines = raw_visible_lines_from_terminal_output(&output);

    assert_eq!(line_count, 11);
    assert!(visible_lines.iter().any(|line| line == "恢复会话 (1 of 2)"));
    assert!(visible_lines.iter().any(|line| line == "> 搜索…"));
    assert!(visible_lines.iter().any(|line| line == "iac-code"));
    assert!(visible_lines
        .iter()
        .any(|line| { line.contains("❯ 图片里有什么字") }));
    assert!(visible_lines
        .iter()
        .any(|line| { line.contains("feature/iac-code-ui") && line.contains("39.0KB") }));
    assert!(
        visible_lines.iter().any(|line| line.contains("Ctrl+A")
            && line.contains("Ctrl+B")
            && line.contains("Space")),
        "{visible_lines:?}"
    );
    assert!(
        visible_lines.iter().all(|line| !line.contains("resume>")),
        "{visible_lines:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_resume_picker_clear_sequence_moves_cursor_off_search_box() {
    let tall = raw_resume_picker_clear_sequence(18);
    assert!(
        tall.starts_with("\x1b[15B"),
        "expected a down-move of 18-3 before clearing, got {tall:?}"
    );
    let short = raw_resume_picker_clear_sequence(3);
    assert!(
        !short.contains('B'),
        "no down-move expected for short blocks, got {short:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_resume_preview_replays_markdown_and_inline_tool_results() {
    use iac_code_protocol::message::{ToolResultBlock, ToolUseBlock};

    let messages = vec![
        AgentMessage {
            role: "user".to_owned(),
            content: AgentMessageContent::Text("整体看下这个项目".to_owned()),
            token_count: 0,
            elapsed_seconds: 0.0,
        },
        AgentMessage {
            role: "assistant".to_owned(),
            content: AgentMessageContent::Blocks(vec![
                AgentContentBlock::Text(TextBlock {
                    text: "这是一个 **Rust** 项目".to_owned(),
                }),
                AgentContentBlock::ToolUse(ToolUseBlock {
                    id: "tool-1".to_owned(),
                    name: "read_file".to_owned(),
                    input: json::object([("path", json::string("Cargo.toml"))]),
                }),
            ]),
            token_count: 0,
            elapsed_seconds: 0.0,
        },
        AgentMessage {
            role: "user".to_owned(),
            content: AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(
                ToolResultBlock {
                    tool_use_id: "tool-1".to_owned(),
                    content: "第 1-80 行（共 200 行）".to_owned(),
                    is_error: false,
                },
            )]),
            token_count: 0,
            elapsed_seconds: 0.0,
        },
    ];

    let lines = raw_resume_preview_body_lines(&messages, 80);
    let raw = lines.join("\n");
    let stripped = raw_strip_ansi_sequences(&raw);

    assert!(stripped.contains("❯ 整体看下这个项目"), "{stripped}");
    assert!(
        raw.contains("\x1b[1m\x1b[36m❯ "),
        "user prompt should be bold cyan"
    );
    assert_eq!(stripped.matches("❯ ").count(), 1, "{stripped}");

    assert!(stripped.contains("Rust"), "{stripped}");
    assert!(
        !stripped.contains("**Rust**"),
        "markdown should be rendered: {stripped}"
    );

    assert!(stripped.contains("● "), "{stripped}");
    assert!(stripped.contains("⎿"), "{stripped}");
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_resume_picker_cancel_reports_cancelled() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit_tail = b"\x1b[?2004l";
    let root = unique_temp_dir("iac-code-rs-raw-prompt-resume-cancel");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let context = RawPromptActionContext {
                resume_current_project_entries: vec![RawResumeSessionEntry::new(
                    "session-alpha",
                    root.to_string_lossy(),
                    "workspace",
                    "alpha stack",
                )],
                ..RawPromptActionContext::default()
            };
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/resume\r\x1b");
    let mut output = read_fd_until_contains(pty.master, "已取消恢复".as_bytes());
    if !output
        .windows(expected_exit_tail.len())
        .any(|window| window == expected_exit_tail)
    {
        output.extend(read_fd_until_contains(pty.master, expected_exit_tail));
    }
    assert_bytes_contains(&output, &raw_prompt_render("/resume"));
    assert_bytes_contains(&output, "已取消恢复".as_bytes());

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/resume");
    assert!(input.prehandled);
    assert_eq!(
        input.transcript_lines,
        vec!["❯ /resume".to_owned(), "  └ 已取消恢复".to_owned()]
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_rename_prompts_for_session_name() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-rename-name");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let context = RawPromptActionContext::default();
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/rename\r");
    let mut output = read_fd_until_contains(pty.master, "会话名称:".as_bytes());
    assert_bytes_contains(&output, &raw_prompt_render("/rename"));
    write_fd(pty.master, b"deploy-prod\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, "会话名称:".as_bytes());
    assert_bytes_contains(&output, b"deploy-prod");

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/rename deploy-prod");
    assert!(!input.prehandled);
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_model_opens_picker_and_inserts_selected_model() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-model-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-model-picker");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    save_active_provider_config(&paths, "dashscope", "qwen3.7-max", None)
        .expect("provider config should be saved");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let root_text = root.to_string_lossy().into_owned();
            let state = InteractiveSessionState {
                resume: String::new(),
                continue_session: false,
                exit_code: EXIT_OK,
                turn_count: 0,
                token_count: 0,
                debug_enabled: false,
                debug_log_path: None,
                current_session_id: None,
                task_manager: TaskManager::new(),
                input_history: None,
                transcript_lines: Vec::new(),
            };
            let context = raw_prompt_action_context(Some(&paths), &root_text, &state);
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/model\r");
    let mut output =
        read_fd_until_contains(pty.master, "为 Alibaba Cloud Bailian 选择模型".as_bytes());
    assert_bytes_contains(&output, b"\x1b[?1049h");
    assert_bytes_contains(&output, b"> qwen3.7-max");
    assert_bytes_contains(&output, "自定义模型...".as_bytes());
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> qwen3.7-plus"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, b"Alibaba Cloud Bailian");
    assert_bytes_contains(&output, b"> qwen3.7-plus");
    assert_bytes_contains(&output, b"\x1b[?1049l");
    assert_bytes_contains(&output, &raw_prompt_render("/model qwen3.7-plus"));
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/model qwen3.7-plus");
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_model_picker_inserts_selected_model_and_effort() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-model-effort-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-model-effort-picker");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    save_active_provider_config(&paths, "openai", "gpt-5.5", None)
        .expect("provider config should be saved");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let root_text = root.to_string_lossy().into_owned();
            let state = InteractiveSessionState {
                resume: String::new(),
                continue_session: false,
                exit_code: EXIT_OK,
                turn_count: 0,
                token_count: 0,
                debug_enabled: false,
                debug_log_path: None,
                current_session_id: None,
                task_manager: TaskManager::new(),
                input_history: None,
                transcript_lines: Vec::new(),
            };
            let context = raw_prompt_action_context(Some(&paths), &root_text, &state);
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/model\r");
    let mut output = read_fd_until_contains(pty.master, b"Select model for OpenAI");
    assert_bytes_contains(&output, b"> gpt-5.5");
    write_fd(pty.master, b"\x1b[C");
    output.extend(read_fd_until_contains(pty.master, "◆◆◆◆".as_bytes()));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/model gpt-5.5 xhigh"));

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/model gpt-5.5 xhigh");
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_model_picker_context_includes_current_custom_model() {
    let config_dir = unique_temp_dir("iac-code-rs-raw-model-custom-config");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    let paths = paths_for(&config_dir);
    save_active_provider_config(&paths, "dashscope", "custom-qwen-model", None)
        .expect("provider config should be saved");

    let (initial_model, groups) = raw_model_picker_context(&paths);

    assert_eq!(initial_model, "custom-qwen-model");
    assert_eq!(groups.len(), 1);
    assert_eq!(groups[0].models[0].model, "custom-qwen-model");
    fs::remove_dir_all(&config_dir).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_skills_picker_saves_disabled_skill() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-skills-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-skills-picker");
    fs::create_dir_all(config_dir.join("skills").join("user-helper"))
        .expect("user skill dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(
        config_dir
            .join("skills")
            .join("user-helper")
            .join("SKILL.md"),
        "---\ndescription: User helper\n---\n\nUse this helper.\n",
    )
    .expect("user skill should be written");
    let paths = paths_for(&config_dir);

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        let paths = paths.clone();
        move || {
            let root_text = root.to_string_lossy().into_owned();
            let state = InteractiveSessionState {
                resume: String::new(),
                continue_session: false,
                exit_code: EXIT_OK,
                turn_count: 0,
                token_count: 0,
                debug_enabled: false,
                debug_log_path: None,
                current_session_id: None,
                task_manager: TaskManager::new(),
                input_history: None,
                transcript_lines: Vec::new(),
            };
            let context = raw_prompt_action_context(Some(&paths), &root_text, &state);
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/skills\r");
    let mut output = read_fd_until_contains(pty.master, b"Search skills...");
    let visible_lines = raw_visible_lines_from_terminal_output(&output);
    assert!(
        visible_lines.iter().any(|line| line.contains("/skills")),
        "{visible_lines:?}"
    );
    assert!(
        visible_lines.iter().all(|line| !line.contains("↑↓ 导航")),
        "{visible_lines:?}"
    );
    write_fd(pty.master, b"user");
    output.extend(read_fd_until_contains(pty.master, b"  - on user-helper"));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> - on user-helper"));
    write_fd(pty.master, b" ");
    output.extend(read_fd_until_contains(pty.master, b"> x off user-helper"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Skills updated"));
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, b"Search skills...");
    assert_bytes_contains(&output, b"user-helper");
    assert!(!output
        .windows(b"User helper".len())
        .any(|window| window == b"User helper"));
    assert_bytes_contains(&output, &raw_prompt_render("/skills"));
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/skills");
    assert!(input.prehandled);
    assert_eq!(input.prompt_content, None);
    let settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be written");
    assert!(settings.contains("user-helper"), "{settings}");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_skills_picker_edits_query_at_cursor() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-skills-cursor-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-skills-cursor");
    fs::create_dir_all(config_dir.join("skills").join("acbd-helper"))
        .expect("user skill dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(
        config_dir
            .join("skills")
            .join("acbd-helper")
            .join("SKILL.md"),
        "---\ndescription: Cursor editing target\n---\n\nUse this helper.\n",
    )
    .expect("user skill should be written");
    let paths = paths_for(&config_dir);

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        let paths = paths.clone();
        move || {
            let root_text = root.to_string_lossy().into_owned();
            let state = InteractiveSessionState {
                resume: String::new(),
                continue_session: false,
                exit_code: EXIT_OK,
                turn_count: 0,
                token_count: 0,
                debug_enabled: false,
                debug_log_path: None,
                current_session_id: None,
                task_manager: TaskManager::new(),
                input_history: None,
                transcript_lines: Vec::new(),
            };
            let context = raw_prompt_action_context(Some(&paths), &root_text, &state);
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/skills\r");
    let mut output = read_fd_until_contains(pty.master, b"Search skills...");
    write_fd(pty.master, b"acd");
    output.extend(read_fd_until_contains(pty.master, b"acd\x1b[s\x1b[7m"));
    write_fd(pty.master, b"\x1b[D");
    output.extend(read_fd_until_contains(
        pty.master,
        b"ac\x1b[s\x1b[7md\x1b[0m",
    ));
    write_fd(pty.master, b"b");
    output.extend(read_fd_until_contains(
        pty.master,
        b"acb\x1b[s\x1b[7md\x1b[0m",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Skills updated"));
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, b"acbd-helper");
    assert_bytes_contains(&output, b"matched description");
    assert_bytes_contains(&output, &raw_prompt_render("/skills"));

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/skills");
    assert!(input.prehandled);
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_skills_picker_renders_query_cursor() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit_tail = b"\x1b[?2004l";
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-skills-cursor-render-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-skills-cursor-render");
    fs::create_dir_all(config_dir.join("skills").join("acbd-helper"))
        .expect("user skill dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(
        config_dir
            .join("skills")
            .join("acbd-helper")
            .join("SKILL.md"),
        "---\ndescription: Cursor rendering target\n---\n\nUse this helper.\n",
    )
    .expect("user skill should be written");
    let paths = paths_for(&config_dir);

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        let paths = paths.clone();
        move || {
            let root_text = root.to_string_lossy().into_owned();
            let state = InteractiveSessionState {
                resume: String::new(),
                continue_session: false,
                exit_code: EXIT_OK,
                turn_count: 0,
                token_count: 0,
                debug_enabled: false,
                debug_log_path: None,
                current_session_id: None,
                task_manager: TaskManager::new(),
                input_history: None,
                transcript_lines: Vec::new(),
            };
            let context = raw_prompt_action_context(Some(&paths), &root_text, &state);
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/skills\r");
    let mut output = read_fd_until_contains(pty.master, b"Search skills...");
    write_fd(pty.master, b"acd");
    output.extend(read_fd_until_contains(pty.master, b"acd\x1b[s\x1b[7m"));
    write_fd(pty.master, b"\x1b[D");
    output.extend(read_fd_until_contains(
        pty.master,
        b"ac\x1b[s\x1b[7md\x1b[0m",
    ));
    assert_bytes_contains(&output, b"ac\x1b[s\x1b[7md\x1b[0m");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Skills updated"));
    if !output
        .windows(expected_exit_tail.len())
        .any(|window| window == expected_exit_tail)
    {
        output.extend(read_fd_until_contains(pty.master, expected_exit_tail));
    }
    assert_bytes_contains(&output, expected_exit_tail);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/skills");
    assert!(input.prehandled);
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_effort_picker_inserts_selected_effort() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-effort-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-effort-picker");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    save_active_provider_config(&paths, "openai", "gpt-5.4", None)
        .expect("provider config should be saved");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let root_text = root.to_string_lossy().into_owned();
            let state = InteractiveSessionState {
                resume: String::new(),
                continue_session: false,
                exit_code: EXIT_OK,
                turn_count: 0,
                token_count: 0,
                debug_enabled: false,
                debug_log_path: None,
                current_session_id: None,
                task_manager: TaskManager::new(),
                input_history: None,
                transcript_lines: Vec::new(),
            };
            let context = raw_prompt_action_context(Some(&paths), &root_text, &state);
            let mut image_source = StaticRawPromptImageSource::new(vec![]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &context,
                &mut image_source,
                &mut Vec::new(),
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/effort\r");
    let mut output = read_fd_until_contains(pty.master, b"gpt-5.4");
    assert_bytes_contains(&output, b"\x1b[?1049h");
    assert_bytes_contains(&output, "> ◆◆◆ high".as_bytes());
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        "> ◆◆◆◆ xhigh".as_bytes(),
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, b"gpt-5.4");
    assert_bytes_contains(&output, b"\x1b[?1049l");
    assert_bytes_contains(&output, &raw_prompt_render("/effort xhigh"));
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/effort xhigh");
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}
