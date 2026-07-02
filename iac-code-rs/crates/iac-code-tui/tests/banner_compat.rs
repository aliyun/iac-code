use std::path::PathBuf;

use iac_code_tui::{
    format_update_command, render_update_notice_lines, render_update_prompt_header_lines,
    render_welcome_banner_ansi_lines, render_welcome_banner_lines, terminal_display_width,
    BannerUpdate, WelcomeBannerLabels, WelcomeBannerState, ACCENT, LOGO_LINES,
};

#[test]
fn welcome_banner_lines_include_python_banner_core_fields() {
    let banner = WelcomeBannerState::new("qwen3.6-plus", "/tmp/work", "0.4.1")
        .with_username("alice")
        .with_provider_display("Alibaba Cloud Bailian")
        .with_session("abc123", Some("deploy-prod"));

    let lines = render_welcome_banner_lines(&banner);
    let text = lines.join("\n");

    assert_eq!(ACCENT, "bright_cyan");
    assert!(text.contains("Welcome back Alice!"));
    assert!(text.contains("Your AI-powered Infrastructure as Code assistant"));
    assert!(text.contains("iac-code v0.4.1"));
    assert!(text.contains("Alibaba Cloud Bailian / qwen3.6-plus"));
    assert!(text.contains("/tmp/work"));
    assert!(text.contains("Session: deploy-prod (abc123)"));
    assert!(LOGO_LINES.iter().any(|line| text.contains(line.trim())));
}

#[test]
fn welcome_banner_formats_cwd_under_home_like_python() {
    let banner = WelcomeBannerState::new("model", "/Users/alice/projects/my-app", "0.4.0")
        .with_username("alice")
        .with_home_dir("/Users/alice");

    assert_eq!(banner.cwd_display(), "~/projects/my-app");
    assert!(render_welcome_banner_lines(&banner)
        .join("\n")
        .contains("~/projects/my-app"));
}

#[test]
fn welcome_banner_handles_empty_provider_model_session_and_debug_lines() {
    let no_model = WelcomeBannerState::new("", "/tmp/work", "0.4.0").with_username("User");
    let text = render_welcome_banner_lines(&no_model).join("\n");
    assert!(!text.contains(" / "));
    assert!(!text.contains("Session:"));

    let debug = WelcomeBannerState::new("model", "/tmp/work", "0.4.0")
        .with_username("User")
        .with_debug_log_path(PathBuf::from("/tmp/iac-code/latest.log"));
    let debug_text = render_welcome_banner_lines(&debug).join("\n");
    assert!(debug_text.contains("Debug mode"));
    assert!(debug_text.contains("Log file: /tmp/iac-code/latest.log"));
}

#[test]
fn welcome_banner_ansi_panel_matches_python_rich_shape_and_styles() {
    let banner = WelcomeBannerState::new("qwen3.7-plus", "/Users/alice/projects/iac-code", "0.4.1")
        .with_username("alice")
        .with_home_dir("/Users/alice")
        .with_provider_display("Alibaba Cloud Bailian")
        .with_session("session-123", None)
        .with_debug_log_path(PathBuf::from("/Users/alice/.iac-code/logs/latest.log"));

    let lines = render_welcome_banner_ansi_lines(&banner, 160);
    let text = lines.join("\n");
    let visible = strip_ansi(&text);

    assert!(text.contains("\x1b[96m╭"), "{text:?}");
    assert!(text.contains("\x1b[96m│"), "{text:?}");
    assert!(text.contains("\x1b[96m         ▄▄███▄▄"), "{text:?}");
    assert!(
        text.contains("\x1b[1m  Welcome back Alice!\x1b[0m"),
        "{text:?}"
    );
    assert!(
        text.contains("\x1b[3;37mYour AI-powered Infrastructure as Code assistant\x1b[0m"),
        "{text:?}"
    );
    assert!(text.contains("\x1b[2m  iac-code v0.4.1\x1b[0m"), "{text:?}");
    assert!(
        text.contains("\x1b[2m  Alibaba Cloud Bailian / qwen3.7-plus\x1b[0m"),
        "{text:?}"
    );
    assert!(text.contains("\x1b[1;33m  Debug mode\x1b[0m"), "{text:?}");
    assert!(
        text.contains("\x1b[2;33m  Log file: /Users/alice/.iac-code/logs/latest.log\x1b[0m"),
        "{text:?}"
    );
    assert!(visible.contains("Your AI-powered Infrastructure as Code assistant"));
    let description_line = visible
        .lines()
        .find(|line| line.contains("Your AI-powered Infrastructure as Code assistant"))
        .expect("description line should render");
    let description_byte_start = description_line
        .find("Your AI-powered Infrastructure as Code assistant")
        .expect("description position");
    let description_start = terminal_display_width(&description_line[..description_byte_start]);
    assert!(
        description_start >= 90,
        "description should sit in the right half of the banner: {description_line:?}"
    );

    for line in visible.lines() {
        assert_eq!(
            terminal_display_width(line),
            160,
            "panel rows should fill the terminal width: {line:?}"
        );
    }
}

#[test]
fn welcome_banner_ansi_panel_accepts_localized_python_labels() {
    let labels = WelcomeBannerLabels {
        welcome_back: "欢迎回来".to_owned(),
        description: "您的 AI 驱动的基础设施即代码助手".to_owned(),
        session: "会话".to_owned(),
        debug_mode: "调试模式".to_owned(),
        log_file: "日志文件".to_owned(),
    };
    let banner = WelcomeBannerState::new("qwen3.7-plus", "/tmp/iac-code", "0.4.1")
        .with_username("prodesire")
        .with_provider_display("阿里云百炼")
        .with_session("session-123", None)
        .with_debug_log_path(PathBuf::from("/tmp/latest.log"))
        .with_labels(labels);

    let text = render_welcome_banner_ansi_lines(&banner, 100).join("\n");

    assert!(text.contains("欢迎回来 Prodesire!"), "{text:?}");
    assert!(
        text.contains("您的 AI 驱动的基础设施即代码助手"),
        "{text:?}"
    );
    assert!(text.contains("阿里云百炼 / qwen3.7-plus"), "{text:?}");
    assert!(text.contains("会话: session-123"), "{text:?}");
    assert!(text.contains("调试模式"), "{text:?}");
    assert!(text.contains("日志文件: /tmp/latest.log"), "{text:?}");
}

#[test]
fn update_banner_lines_match_python_versions_command_and_release_notes() {
    let update = BannerUpdate::new(
        "0.3.0",
        "0.4.0",
        vec![
            "/path with spaces/python".to_owned(),
            "-m".to_owned(),
            "pip".to_owned(),
            "install".to_owned(),
            "--upgrade".to_owned(),
            "iac-code".to_owned(),
        ],
    )
    .with_release_notes_url("https://github.com/aliyun/iac-code/releases/latest");

    let command = format_update_command(&update.update_command);
    let prompt = render_update_prompt_header_lines(&update).join("\n");
    let notice = render_update_notice_lines(&update).join("\n");

    assert_eq!(
        command,
        "'/path with spaces/python' -m pip install --upgrade iac-code"
    );
    for text in [prompt, notice] {
        assert!(text.contains("Update available! 0.3.0 -> 0.4.0"));
        assert!(text.contains(&command));
        assert!(text.contains("Release notes: https://github.com/aliyun/iac-code/releases/latest"));
    }
}

fn strip_ansi(input: &str) -> String {
    let mut output = String::new();
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for code in chars.by_ref() {
                if code.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            output.push(ch);
        }
    }
    output
}
