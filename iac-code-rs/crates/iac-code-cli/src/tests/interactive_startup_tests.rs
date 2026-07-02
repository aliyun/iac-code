use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_config::paths::ConfigPaths;
use iac_code_core::SessionStorage;
use iac_code_protocol::message::{AgentContentBlock, AgentMessageContent, ImageBlock};

use crate::cli_args::Cli;
use crate::debug_logging::interactive_startup_banner_debug_log_display_path;
use crate::interactive_banner::{
    interactive_clear_screen_sequence, interactive_startup_banner_ansi_lines,
    interactive_startup_banner_lines, interactive_startup_banner_provider_display,
    should_print_interactive_startup_banner, InteractiveStartupBannerAnsiOptions,
};
use crate::interactive_prompt_handler::{
    interactive_slash_command_history_mode, InteractiveInputHistoryMode,
};
use crate::interactive_runtime::{
    initialize_interactive_startup_session, interactive_ctrl_c_exit_requested,
    interactive_ctrl_c_warning_line, interactive_exit_text_lines, should_use_raw_interactive_input,
    INTERACTIVE_CTRL_C_EXIT_WINDOW,
};
use crate::prompt_content::{ensure_prompt_content_supported, local_image_path_prompt_content};
use crate::session_utils::current_working_directory;
use crate::test_support::{
    english_locale_config_dir_guard, english_locale_guard, paths_for, unique_temp_dir, EnvVarGuard,
};

#[test]
fn interactive_startup_banner_requires_tty_stdin_and_stdout() {
    assert!(should_print_interactive_startup_banner(true, true));
    assert!(!should_print_interactive_startup_banner(false, true));
    assert!(!should_print_interactive_startup_banner(true, false));
    assert!(!should_print_interactive_startup_banner(false, false));
}

#[cfg(unix)]
#[test]
fn raw_interactive_input_requires_tty_stdin_and_stdout() {
    assert!(should_use_raw_interactive_input(true, true));
    assert!(!should_use_raw_interactive_input(false, true));
    assert!(!should_use_raw_interactive_input(true, false));
    assert!(!should_use_raw_interactive_input(false, false));
}

#[test]
fn interactive_ctrl_c_warning_line_uses_locale() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);

    assert_eq!(interactive_ctrl_c_warning_line(), "再次按 Ctrl+C 退出。");
}

#[test]
fn interactive_exit_text_lines_include_resume_hint_when_session_known() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);

    assert_eq!(
        interactive_exit_text_lines(Some("session-123")),
        vec![
            "再见！".to_owned(),
            "恢复此会话请运行：".to_owned(),
            "iac-code --resume session-123".to_owned(),
        ]
    );
}

#[test]
fn interactive_exit_text_lines_omit_resume_hint_without_session() {
    let _env = english_locale_guard();

    assert_eq!(
        interactive_exit_text_lines(None),
        vec!["Goodbye!".to_owned()]
    );
}

#[test]
fn interactive_clear_screen_sequence_erases_screen_and_scrollback_like_python() {
    // `/clear` must wipe both the visible screen and the scrollback, exactly
    // like the Python `clear_command` — otherwise it appears to do nothing.
    assert_eq!(interactive_clear_screen_sequence(), "\x1b[H\x1b[2J\x1b[3J");
}

#[test]
fn interactive_ctrl_c_exit_requires_second_press_within_window() {
    let first_press = Instant::now();

    assert!(!interactive_ctrl_c_exit_requested(None, first_press));
    assert!(interactive_ctrl_c_exit_requested(
        Some(first_press),
        first_press + INTERACTIVE_CTRL_C_EXIT_WINDOW,
    ));
    assert!(!interactive_ctrl_c_exit_requested(
        Some(first_press),
        first_press + INTERACTIVE_CTRL_C_EXIT_WINDOW + Duration::from_millis(1),
    ));
}

#[test]
fn interactive_startup_session_is_persisted_for_resume_hint() {
    let root = unique_temp_dir("iac-code-rs-interactive-startup-session");
    let _env = english_locale_config_dir_guard(&root);
    let cli = Cli::default();
    let mut debug_log_path = None;

    let session_id = initialize_interactive_startup_session(&cli, true, true, &mut debug_log_path)
        .expect("startup session should be created");

    let paths = ConfigPaths::from_env().expect("paths should load");
    let cwd = current_working_directory().expect("cwd should resolve");
    let storage = SessionStorage::new(paths.subdirs().projects).expect("storage should initialize");
    assert!(storage.exists(&cwd, &session_id));
    assert!(storage
        .load(&cwd, &session_id)
        .expect("session should load")
        .is_empty());
    assert_eq!(
        interactive_exit_text_lines(Some(&session_id)),
        vec![
            "Goodbye!".to_owned(),
            "Resume this session with:".to_owned(),
            format!("iac-code --resume {session_id}"),
        ]
    );
    assert!(debug_log_path.is_none());
    fs::remove_dir_all(root).ok();
}

#[test]
fn interactive_startup_session_does_not_replace_explicit_resume() {
    let root = unique_temp_dir("iac-code-rs-interactive-startup-session-skip");
    let _env = EnvVarGuard::set("IAC_CODE_CONFIG_DIR", root.to_string_lossy().as_ref());
    let cli = Cli {
        resume: "existing-session".to_owned(),
        ..Cli::default()
    };
    let mut debug_log_path = None;

    assert!(
        initialize_interactive_startup_session(&cli, true, true, &mut debug_log_path).is_none()
    );
    assert!(debug_log_path.is_none());
    fs::remove_dir_all(root).ok();
}

#[test]
fn interactive_startup_session_is_tty_only() {
    let root = unique_temp_dir("iac-code-rs-interactive-startup-session-non-tty");
    let _env = EnvVarGuard::set("IAC_CODE_CONFIG_DIR", root.to_string_lossy().as_ref());
    let cli = Cli::default();
    let mut debug_log_path = None;

    assert!(
        initialize_interactive_startup_session(&cli, false, true, &mut debug_log_path).is_none()
    );
    assert!(debug_log_path.is_none());
    fs::remove_dir_all(root).ok();
}

#[test]
fn interactive_startup_banner_lines_use_tui_banner_state() {
    let cwd = PathBuf::from("/tmp/iac-code-workspace");
    let debug_log_path = PathBuf::from("/tmp/iac-code-debug.log");

    let lines = interactive_startup_banner_lines(
        "qwen3-coder-plus",
        &cwd,
        Some("aliyun-codingplan"),
        "alice",
        Some("session-123"),
        Some("deploy-prod"),
        Some(&debug_log_path),
    );

    assert!(lines
        .iter()
        .any(|line| line.contains("Welcome back Alice!")));
    assert!(lines.iter().any(|line| line.contains("iac-code v0.4.1")));
    assert!(lines
        .iter()
        .any(|line| line.contains("aliyun-codingplan / qwen3-coder-plus")));
    assert!(lines
        .iter()
        .any(|line| line.contains("/tmp/iac-code-workspace")));
    assert!(lines
        .iter()
        .any(|line| line.contains("Session: deploy-prod (session-123)")));
    assert!(lines
        .iter()
        .any(|line| line.contains("Log file: /tmp/iac-code-debug.log")));
}

#[test]
fn interactive_startup_banner_ansi_lines_localize_and_style_like_python_banner() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let cwd = PathBuf::from("/Users/prodesire/projects/ali-github/iac-code");
    let debug_log_path = PathBuf::from("/Users/prodesire/.iac-code/logs/latest.log");
    let provider_display = interactive_startup_banner_provider_display("dashscope")
        .expect("dashscope should have a display name");

    let lines = interactive_startup_banner_ansi_lines(InteractiveStartupBannerAnsiOptions {
        model: "qwen3.7-plus",
        cwd: &cwd,
        provider_display: Some(&provider_display),
        username: "prodesire",
        session_id: Some("session-123"),
        session_name: None,
        debug_log_path: Some(&debug_log_path),
        terminal_width: 120,
    });
    let text = lines.join("\n");

    assert!(text.contains("\x1b[96m╭"), "{text:?}");
    assert!(text.contains("\x1b[96m│"), "{text:?}");
    assert!(text.contains("\x1b[96m         ▄▄███▄▄"), "{text:?}");
    assert!(
        text.contains("\x1b[1m  欢迎回来 Prodesire!\x1b[0m"),
        "{text:?}"
    );
    assert!(
        text.contains("\x1b[3;37m您的 AI 驱动的基础设施即代码助手\x1b[0m"),
        "{text:?}"
    );
    assert!(text.contains("\x1b[2m  iac-code v0.4.1\x1b[0m"), "{text:?}");
    assert!(
        text.contains("\x1b[2m  阿里云百炼 / qwen3.7-plus\x1b[0m"),
        "{text:?}"
    );
    assert!(
        text.contains("\x1b[2m  会话: session-123\x1b[0m"),
        "{text:?}"
    );
    assert!(text.contains("\x1b[1;33m  调试模式\x1b[0m"), "{text:?}");
    assert!(
        text.contains("\x1b[2;33m  日志文件: /Users/prodesire/.iac-code/logs/latest.log\x1b[0m"),
        "{text:?}"
    );
}

#[test]
fn interactive_startup_banner_debug_log_display_path_uses_latest_log_like_python() {
    let log_path = PathBuf::from("/Users/prodesire/.iac-code/logs/interactive-123.log");

    assert_eq!(
        interactive_startup_banner_debug_log_display_path(&log_path),
        PathBuf::from("/Users/prodesire/.iac-code/logs/latest.log")
    );
}

#[test]
fn local_image_path_prompt_converts_to_image_content() {
    let dir = unique_temp_dir("iac-code-rs-image-prompt");
    fs::create_dir_all(&dir).expect("temp dir should be created");
    let image_path = dir.join("sample.png");
    fs::write(&image_path, b"\x89PNG\r\n\x1a\npayload").expect("image should be written");

    let content = local_image_path_prompt_content(&format!("'{}'", image_path.display()))
        .expect("image prompt should parse")
        .expect("image prompt should produce content");

    assert_eq!(
        content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::Image(ImageBlock {
            media_type: "image/png".into(),
            data: STANDARD.encode(b"\x89PNG\r\n\x1a\npayload"),
        })])
    );

    let _ = fs::remove_dir_all(dir);
}

#[test]
fn file_url_image_prompt_decodes_path_and_media_type() {
    let dir = unique_temp_dir("iac-code-rs-image-url");
    let spaced = dir.join("space name");
    fs::create_dir_all(&spaced).expect("temp dir should be created");
    let image_path = spaced.join("sample.webp");
    fs::write(&image_path, b"RIFFxxxxWEBPpayload").expect("image should be written");
    let file_url = format!(
        "file://{}",
        image_path.display().to_string().replace(' ', "%20")
    );

    let content = local_image_path_prompt_content(&file_url)
        .expect("image prompt should parse")
        .expect("image prompt should produce content");

    assert_eq!(
        content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::Image(ImageBlock {
            media_type: "image/webp".into(),
            data: STANDARD.encode(b"RIFFxxxxWEBPpayload"),
        })])
    );

    let _ = fs::remove_dir_all(dir);
}

#[test]
fn non_image_prompt_does_not_convert_to_image_content() {
    assert_eq!(
        local_image_path_prompt_content("describe this infrastructure")
            .expect("plain text prompt should not error"),
        None
    );
}

#[test]
fn image_prompt_support_gate_uses_multimodal_registry() {
    let dir = unique_temp_dir("iac-code-rs-image-gate");
    fs::create_dir_all(&dir).expect("temp dir should be created");
    let paths = paths_for(&dir);
    let content = AgentMessageContent::Blocks(vec![AgentContentBlock::Image(ImageBlock {
        media_type: "image/png".into(),
        data: "base64-image".into(),
    })]);
    let supported = iac_code_providers::ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-opus-4-7".into(),
        api_key: None,
        base_url: None,
        effort: None,
        supports_stream_options: false,
    };
    let unsupported = iac_code_providers::ProviderConfig {
        provider_key: "deepseek".into(),
        model: "deepseek-v4-pro".into(),
        api_key: None,
        base_url: None,
        effort: None,
        supports_stream_options: true,
    };

    assert!(
        ensure_prompt_content_supported(&content, &paths, &supported, "claude-opus-4-7").is_ok()
    );
    let error = ensure_prompt_content_supported(&content, &paths, &unsupported, "deepseek-v4-pro")
        .expect_err("non-vision model should reject images");
    assert!(error.contains("does not support image input"));

    let _ = fs::remove_dir_all(dir);
}

#[test]
fn interactive_slash_command_history_modes_match_python_defaults() {
    assert_eq!(
        interactive_slash_command_history_mode("/help"),
        Some(InteractiveInputHistoryMode::Session)
    );
    assert_eq!(
        interactive_slash_command_history_mode("/?"),
        Some(InteractiveInputHistoryMode::Session)
    );
    assert_eq!(
        interactive_slash_command_history_mode("/login"),
        Some(InteractiveInputHistoryMode::Session)
    );
    assert_eq!(
        interactive_slash_command_history_mode("/memory-folder search foo"),
        Some(InteractiveInputHistoryMode::Session)
    );
    assert_eq!(
        interactive_slash_command_history_mode("/model qwen3-coder-plus"),
        Some(InteractiveInputHistoryMode::Session)
    );
    assert_eq!(
        interactive_slash_command_history_mode("/exit"),
        Some(InteractiveInputHistoryMode::None)
    );
    assert_eq!(
        interactive_slash_command_history_mode("/quit"),
        Some(InteractiveInputHistoryMode::None)
    );
    assert_eq!(
        interactive_slash_command_history_mode("/q"),
        Some(InteractiveInputHistoryMode::None)
    );
    assert_eq!(interactive_slash_command_history_mode("/unknown"), None);
}
