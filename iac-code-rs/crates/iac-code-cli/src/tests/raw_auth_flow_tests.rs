use std::fs;
#[cfg(unix)]
use std::os::fd::RawFd;
#[cfg(unix)]
use std::path::PathBuf;
#[cfg(unix)]
use std::thread::{self, JoinHandle};

#[cfg(unix)]
use iac_code_config::paths::ConfigPaths;
#[cfg(unix)]
use iac_code_config::settings::save_active_provider_config;
#[cfg(unix)]
use iac_code_exec::EXIT_OK;
#[cfg(unix)]
use iac_code_tools::TaskManager;

use crate::interactive_session::{raw_prompt_action_context, InteractiveSessionState};
use crate::raw_auth::{
    raw_auth_configuration_choice, raw_auth_configured_provider_model_message,
    raw_auth_index_picker_render_output, RawAuthConfigurationChoice,
};
use crate::raw_auth_input::raw_auth_masked_input_render_output;
#[cfg(unix)]
use crate::raw_auth_llm::{raw_auth_llm_group_choice, RawAuthLlmGroupChoice};
use crate::raw_prompt_input::{
    read_raw_interactive_prompt_input_with_context_and_image_source, RawInteractivePromptInput,
};
use crate::test_support::{
    assert_bytes_contains, english_locale_guard, paths_for, raw_ansi_screen_after_writes,
    raw_prompt_render, read_fd_exact, read_fd_until_contains, terminal_mode_bytes, unique_temp_dir,
    write_fd, EnvVarGuard, PseudoTerminal, StaticRawPromptImageSource,
};

#[cfg(unix)]
fn spawn_raw_auth_prompt_reader(
    slave: RawFd,
    root: PathBuf,
    paths: ConfigPaths,
) -> JoinHandle<Option<RawInteractivePromptInput>> {
    thread::spawn(move || {
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
    })
}

#[cfg(unix)]
#[test]
fn raw_auth_index_picker_renders_static_menu_like_python_select() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let options = vec!["配置 LLM 提供商".to_owned(), "配置 IaC 云服务".to_owned()];

    let (output, line_count) =
        raw_auth_index_picker_render_output(0, "选择配置类型", &options, 0, 80);

    assert_eq!(line_count, 7);
    assert!(!output.contains("auth>"), "{output:?}");
    assert!(output.contains("\r\n  \x1b[1m选择配置类型\x1b[0m\r\n\r\n"));
    assert!(output.contains("  \x1b[96m> 配置 LLM 提供商\x1b[0m\r\n"));
    assert!(output.contains("    \x1b[38;2;128;128;128m配置 IaC 云服务\x1b[0m\r\n"));
    assert!(output.contains("  \x1b[38;2;128;128;128m↑↓ 导航  Enter 确认  Esc 返回\x1b[0m"));
}

#[cfg(unix)]
#[test]
fn raw_auth_index_picker_keeps_lines_left_aligned_in_raw_mode() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let options = vec!["配置 LLM 提供商".to_owned(), "配置 IaC 云服务".to_owned()];

    let (output, _line_count) =
        raw_auth_index_picker_render_output(0, "选择配置类型", &options, 0, 80);
    let screen = raw_ansi_screen_after_writes(80, 8, &[output.as_bytes()]);

    assert_eq!(screen.lines[1], "  选择配置类型");
    assert_eq!(screen.lines[3], "  > 配置 LLM 提供商");
    assert_eq!(screen.lines[4], "    配置 IaC 云服务");
    assert_eq!(screen.lines[6], "  ↑↓ 导航  Enter 确认  Esc 返回");
}

#[cfg(unix)]
#[test]
fn raw_auth_configured_provider_model_message_localizes_like_python() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);

    assert_eq!(
        raw_auth_configured_provider_model_message("阿里云百炼", "qwen3.7-plus"),
        "已配置：阿里云百炼 / qwen3.7-plus"
    );
}

#[cfg(unix)]
#[test]
fn raw_auth_configuration_choice_rejects_unknown_indexes() {
    assert_eq!(
        raw_auth_configuration_choice(0),
        Some(RawAuthConfigurationChoice::LlmProvider)
    );
    assert_eq!(
        raw_auth_configuration_choice(1),
        Some(RawAuthConfigurationChoice::IacCloudService)
    );
    assert_eq!(raw_auth_configuration_choice(2), None);
}

#[cfg(unix)]
#[test]
fn raw_auth_llm_group_choice_handles_third_party_offset() {
    assert_eq!(
        raw_auth_llm_group_choice(true, 0, 3),
        Some(RawAuthLlmGroupChoice::ThirdParty)
    );
    assert_eq!(
        raw_auth_llm_group_choice(true, 1, 3),
        Some(RawAuthLlmGroupChoice::ProviderGroup(0))
    );
    assert_eq!(
        raw_auth_llm_group_choice(false, 2, 3),
        Some(RawAuthLlmGroupChoice::ProviderGroup(2))
    );
    assert_eq!(raw_auth_llm_group_choice(true, 4, 3), None);
    assert_eq!(raw_auth_llm_group_choice(false, 3, 3), None);
}

#[cfg(unix)]
#[test]
fn raw_auth_masked_input_prefills_existing_key_like_python() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);

    let (output, line_count) = raw_auth_masked_input_render_output(
        0,
        "为 阿里云百炼 输入 API 密钥",
        "API key: ",
        "existing-secret",
        true,
        100,
    );

    assert_eq!(line_count, 6);
    assert!(!output.contains("auth>"), "{output:?}");
    assert!(output.contains("\r\n  \x1b[1m为 阿里云百炼 输入 API 密钥\x1b[0m\r\n\r\n"));
    assert!(output.contains("  API key: ***************"));
    assert!(
        output.contains("  \x1b[38;2;128;128;128mEnter 保留  Backspace 重新输入  Esc 返回\x1b[0m")
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_configures_default_llm_provider_without_echoing_key() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    let fake_key = "fake-dashscope-key";

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    assert_bytes_contains(&output, b"\x1b[?1049h");
    assert_bytes_contains(&output, b"> Configure LLM Provider");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    if output
        .windows(b"> Third-party".len())
        .any(|window| window == b"> Third-party")
    {
        write_fd(pty.master, b"\x1b[B");
        output.extend(read_fd_until_contains(pty.master, b"> Alibaba Cloud"));
    } else {
        assert_bytes_contains(&output, b"> Alibaba Cloud");
    }
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    assert_bytes_contains(&output, b"> Alibaba Cloud Bailian");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"API key: "));
    write_fd(pty.master, fake_key.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"******************"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select model for"));
    assert_bytes_contains(&output, b"> qwen3.7-max");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, b"\x1b[?1049l");
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));
    assert_bytes_contains(
        &output,
        b"  \xE2\x94\x94 Configured: Alibaba Cloud Bailian / qwen3.7-max",
    );
    assert_bytes_contains(&output, &expected_exit);
    assert!(
        !String::from_utf8_lossy(&output).contains(fake_key),
        "raw auth output must not echo API key"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);
    assert!(input.prehandled);
    assert_eq!(
        input.transcript_lines,
        vec![
            "❯ /auth".to_owned(),
            "  └ Configured: Alibaba Cloud Bailian / qwen3.7-max".to_owned(),
        ]
    );

    let credentials = fs::read_to_string(config_dir.join(".credentials.yml"))
        .expect("credentials should be written");
    let settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be written");
    assert!(credentials.contains("dashscope: fake-dashscope-key"));
    assert!(settings.contains("activeProvider: dashscope"));
    assert!(settings.contains("model: qwen3.7-max"));
    assert!(!settings.contains("apiBase"), "{settings}");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_uses_detected_chinese_locale() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-zh-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-zh-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, "选择配置类型".as_bytes());
    assert_bytes_contains(&output, "配置 LLM 提供商".as_bytes());
    assert_bytes_contains(&output, "配置 IaC 云服务".as_bytes());
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        "配置 IaC 云服务".as_bytes(),
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        "选择云服务商".as_bytes(),
    ));
    assert_bytes_contains(&output, "阿里云".as_bytes());
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, "配置阿里云".as_bytes()));
    assert_bytes_contains(&output, "凭证".as_bytes());
    assert_bytes_contains(&output, "地域".as_bytes());
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        "选择凭证类型".as_bytes(),
    ));
    assert_bytes_contains(&output, "AccessKey".as_bytes());
    assert_bytes_contains(&output, "STS 令牌".as_bytes());
    assert_bytes_contains(&output, "RAM 角色".as_bytes());
    assert_bytes_contains(&output, "OAuth 登录（浏览器）".as_bytes());
    write_fd(pty.master, b"\x1b");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, b"\x1b[?1049l");
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));
    assert_bytes_contains(&output, "  └ 认证已取消".as_bytes());
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);
    assert!(input.prehandled);
    assert_eq!(
        input.transcript_lines,
        vec!["❯ /auth".to_owned(), "  └ 认证已取消".to_owned()]
    );
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_compatible_provider_saves_api_base() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-compatible-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-compatible-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    save_active_provider_config(&paths, "openapi_compatible", "custom-model", None)
        .expect("provider config should be saved");
    let fake_key = "fake-compatible-key";
    let api_base = "https://compatible.example/v1";
    let api_base_suffix = "compatible.example/v1";

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    assert_bytes_contains(&output, b"> Compatible");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    assert_bytes_contains(&output, b"> OpenAPI Compatible");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"API Base URL: "));
    assert_bytes_contains(&output, b"API Base URL: https://");
    write_fd(pty.master, api_base_suffix.as_bytes());
    output.extend(read_fd_until_contains(pty.master, api_base.as_bytes()));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"API key: "));
    write_fd(pty.master, fake_key.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"*******************"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select model for"));
    assert_bytes_contains(&output, b"> custom-model");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert!(
        !String::from_utf8_lossy(&output).contains(fake_key),
        "raw auth output must not echo API key"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let credentials = fs::read_to_string(config_dir.join(".credentials.yml"))
        .expect("credentials should be written");
    let settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be written");
    assert!(credentials.contains("openapi_compatible: fake-compatible-key"));
    assert!(settings.contains("activeProvider: openapi_compatible"));
    assert!(settings.contains("model: custom-model"));
    assert!(settings.contains("apiBase: \"https://compatible.example/v1\""));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_local_provider_prompts_for_custom_model_without_api_key() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-local-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-local-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(
        config_dir.join("settings.yml"),
        "activeProvider: ollama\nproviders:\n  ollama:\n    name: Ollama\n",
    )
    .expect("settings should be written");
    let paths = paths_for(&config_dir);

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    assert_bytes_contains(&output, b"> Local");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    assert_bytes_contains(&output, b"> Ollama (Local)");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Enter custom model name: ",
    ));
    write_fd(pty.master, b"llama3.1");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Enter custom model name: llama3.1",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert!(
        !String::from_utf8_lossy(&output).contains("API key"),
        "local auth should not ask for an API key"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be written");
    assert!(settings.contains("activeProvider: ollama"));
    assert!(settings.contains("model: llama3.1"));
    assert!(!config_dir.join(".credentials.yml").exists());
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_selects_available_qwenpaw_partner_source() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-partner-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-partner-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(config_dir.join("settings.yml"), "llm_source: qwenpaw\n")
        .expect("settings should be written");
    let paths = paths_for(&config_dir);

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    assert_bytes_contains(&output, b"> Third-party");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    assert_bytes_contains(&output, b"> QwenPaw");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be written");
    assert!(settings.contains("llm_source: qwenpaw"));
    assert!(!settings.contains("activeProvider"), "{settings}");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_non_empty_provider_accepts_custom_model() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-custom-model-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-custom-model-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    save_active_provider_config(&paths, "deepseek", "deepseek-v4-pro", None)
        .expect("provider config should be saved");
    fs::write(
        config_dir.join(".credentials.yml"),
        "deepseek: fake-deepseek-key\n",
    )
    .expect("credentials should be written");

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select provider"));
    assert_bytes_contains(&output, b"> DeepSeek");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"API key: *****************",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select model for"));
    assert_bytes_contains(&output, b"> deepseek-v4-pro");
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> deepseek-v4-flash"));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> Custom model..."));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Enter custom model name: ",
    ));
    write_fd(pty.master, b"deepseek-custom");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Enter custom model name: deepseek-custom",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));
    assert!(
        !String::from_utf8_lossy(&output).contains("fake-deepseek-key"),
        "raw auth output must not echo existing API key"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be written");
    assert!(settings.contains("activeProvider: deepseek"));
    assert!(settings.contains("model: deepseek-custom"));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}
